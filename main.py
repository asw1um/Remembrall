import discord
import os
import asyncio
import aiosqlite
import sqlite3
import shutil
import json
from typing import Optional
from discord.ext import commands, tasks
from discord import app_commands, Interaction
from datetime import datetime, timedelta
from dotenv import load_dotenv
from lateness_model import LatenessPipeline, setup_tables
from discord import ButtonStyle, ui
import numpy as np
import re

# get token and file
load_dotenv()
TOKEN   = os.getenv("DISCORD_TOKEN")
DB_FILE = "events.db"

# database
db_conn = None

async def get_db():
    """Returns a persistent async connection. One connection prevents locking."""
    global db_conn
    if db_conn is None:
        db_conn = await aiosqlite.connect(DB_FILE, timeout=60)
        await db_conn.execute("PRAGMA journal_mode=WAL;")
        await db_conn.execute("PRAGMA synchronous=NORMAL;")
    return db_conn



async def init_db():
    """Initializes tables using the async connection logic."""
    db = await get_db()
    await db.execute("""CREATE TABLE IF NOT EXISTS events (guild_id TEXT, user_id TEXT, username TEXT,  name TEXT, time TEXT, lateness INTEGER)""")
    await db.execute("""CREATE TABLE IF NOT EXISTS schedules(guild_id TEXT, user_id TEXT, username TEXT,  name TEXT, day_of_week INTEGER, time_24h TEXT)""")
    await db.execute("""CREATE TABLE IF NOT EXISTS guild_config(guild_id TEXT PRIMARY KEY, log_channel_id TEXT)""")
    

    cursor = await db.execute("PRAGMA table_info(events)")
    cols = [row[1] for row in await cursor.fetchall()]
    if "guild_id" not in cols:
        await db.execute("ALTER TABLE events ADD COLUMN guild_id TEXT")

    if "dm_sent" not in cols:
        await db.execute("ALTER TABLE events ADD COLUMN dm_sent INTEGER DEFAULT 0")
 
    cursor = await db.execute("PRAGMA table_info(schedules)")
    cols = [row[1] for row in await cursor.fetchall()]
    if "guild_id" not in cols:
        await db.execute("ALTER TABLE schedules ADD COLUMN guild_id TEXT")

    if "end_time_24h" not in cols:
        await db.execute("ALTER TABLE schedules ADD COLUMN end_time_24h TEXT")
 
    await asyncio.to_thread(setup_tables)
    await db.commit()

async def query_db(query: str, args: tuple = (), one: bool = False):
    """Refreshed async query handler. No manual locking needed with aiosqlite."""
    db = await get_db()
    try:
        async with db.execute(query, args) as cursor:
            if query.strip().upper().startswith("SELECT"):
                rows = await cursor.fetchall()
                if one:
                    return rows[0] if rows else None
                return rows
            else:
                await db.commit()
                return []
    except sqlite3.OperationalError as e:
        if "locked" in str(e):
            await asyncio.sleep(0.5)
            return await query_db(query, args, one)
        raise e

#SQL prevention helpers

def validate_rowid(raw: str) -> int:
    if not raw.strip().isdigit():
        raise ValueError
    val = int(raw.strip())
    if val <= 0:
        raise ValueError
    return val

def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

def sanitize_text(value: str | None, max_len: int = 100) -> str | None:
    if value is None:
        return None
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", value)
    return value.strip()[:max_len]

#cmd helpers
async def time_suggester(interaction: discord.Interaction, current: str):
    suggestions = []
    # Clean the input for processing
    raw_input = current.replace(":", "") 
    minutes = ["00", "15", "30", "45"]


    if len(raw_input) >= 3:
        h_str = raw_input[:2]
        m_str = raw_input[2:]
        if h_str.isdigit() and m_str.isdigit():
            h, m = int(h_str), int(m_str)
            if 0 <= h < 24 and 0 <= m < 60:
                # Add their EXACT input as the first suggestion
                exact_time = f"{h:02d}:{m:02d}"
                suggestions.append(app_commands.Choice(name=f" Use: {exact_time}", value=exact_time))

    if not current:
        now_h = datetime.now().hour
        for i in range(5):
            h = (now_h + i) % 24
            for m in minutes:
                suggestions.append(app_commands.Choice(name=f"{h:02d}:{m}", value=f"{h:02d}:{m}"))

    elif len(raw_input) <= 2:
        try:
            h = int(raw_input)
            if 0 <= h < 24:
                for m in minutes:
                    suggestions.append(app_commands.Choice(name=f"{h:02d}:{m}", value=f"{h:02d}:{m}"))
        except: pass

    else:
        try:
            raw = current.replace(":","").replace(".","")
            if len(raw) == 3:
                raw= "0"+raw
            h_str = raw_input[:2]
            m_part = raw_input[2:]
            # Filter your 15-min presets
            for m in minutes:
                if m.startswith(m_part):
                    suggestions.append(app_commands.Choice(name=f"{h_str}:{m}", value=f"{h_str}:{m}"))
        except: pass

    return suggestions[:25]

#get channel
async def get_log_channel(guild: discord.Guild) -> discord.TextChannel | None:
    row = await query_db(
        "SELECT log_channel_id FROM guild_config WHERE guild_id = ?",
        (str(guild.id),), one=True
    )
    if row and row[0]:
        return guild.get_channel(int(row[0]))
    return discord.utils.get(guild.text_channels, name="general")

# autocomplete/options

async def event_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    guild_id = str(interaction.guild.id)
    uid = str(interaction.user.id)
    cmd_name = interaction.command.name
    is_admin_view = interaction.command.parent and interaction.command.parent.name == "admin"

    search_term = f"%{current}%"    # Clear search

    if "schedule" in cmd_name:
        if is_admin_view:
            query = """SELECT rowid, name, day_of_week, time_24h, username FROM schedules WHERE guild_id = ? AND (name LIKE ? OR username LIKE ?) LIMIT 25"""
            params = (guild_id, search_term, search_term)
        else:
            query = """SELECT rowid, name, day_of_week, time_24h, username FROM schedules WHERE guild_id = ? AND user_id = ? AND name LIKE ? LIMIT 25"""
            params = (guild_id, uid, search_term)

        rows = await query_db(query, params)
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return [ app_commands.Choice(name=f"{r[4]}: {r[1]} ({days[r[2]]} @ {r[3]})"[:100] if is_admin_view else f"{r[1]} ({days[r[2]]} @ {r[3]})"[:100],value=str(r[0])) for r in rows]

    filter_sql = ""
    if cmd_name in ["stop", "predict"]:
        filter_sql = " AND lateness IS NULL"
    
    sort_order = "ASC" if cmd_name in ["stop", "predict"] else "DESC"

    is_global_command = cmd_name in ["stop", "predict"]

    if is_admin_view or is_global_command:
        query = f"""SELECT rowid, name, time, username FROM events WHERE guild_id = ?{filter_sql} AND (name LIKE ? OR username LIKE ?) ORDER BY time {sort_order} LIMIT 25"""
        params = (guild_id, search_term, search_term)
    else:
        # Standard user commands (like /event delete) stay locked down strictly to their own rows
        query = f"""SELECT rowid, name, time, username FROM events WHERE guild_id = ? AND user_id = ?{filter_sql} AND name LIKE ? ORDER BY time {sort_order} LIMIT 25"""
        params = (guild_id, uid, search_term)

    rows = await query_db(query, params)
    
    choices = []
    for r in rows:
        show_username = is_admin_view or is_global_command
        label = f"{r[3]}: {r[1]} [{r[2]}]" if show_username else f"{r[1]} [{r[2]}]"
        choices.append(app_commands.Choice(name=label[:100], value=str(r[0])))
        
    return choices


# stop logic

async def execute_stop_logic(interaction, event_id_str, members_list, role):
    name_lookup = await query_db( "SELECT name FROM events WHERE rowid = ?", (int(event_id_str),), one=True )
    if not name_lookup:
        return await interaction.response.send_message("❌ Event not found.", ephemeral=True)

    actual_event_name = name_lookup[0]
    targets = {m for m in members_list if m}
    if role:
        targets.update(m for m in role.members if not m.bot)
    if not targets:
        targets.add(interaction.user)

    now = datetime.now()
    guild_id = str(interaction.guild.id)
    success_count = 0
    last_diff = 0

    for member in targets:
        uid = str(member.id)
        row = await query_db( "SELECT rowid, time FROM events " "WHERE user_id = ? AND guild_id = ? AND name = ? AND lateness IS NULL " "ORDER BY rowid DESC LIMIT 1", (uid, guild_id, actual_event_name), one=True )
        if row:
            rid, time_str = row[0], row[1]
            try:
                fmt = "%Y-%m-%d %H:%M" if len(time_str) > 5 else "%H:%M"
                target_dt = datetime.strptime(time_str, fmt)
                if fmt == "%H:%M":
                    target_dt = target_dt.replace(year=now.year, month=now.month, day=now.day)
                diff = int((now - target_dt).total_seconds())
                last_diff = diff
                await query_db("UPDATE events SET lateness = ? WHERE rowid = ?", (diff, rid))

                # DM cleanup
                id_row = await query_db("SELECT last_dm_message_id FROM events WHERE rowid = ?", (rid,), one=True)
                if id_row and id_row[0]:
                    dm_channel = await member.create_dm()
                    for mid in id_row[0].split(","):
                        try:
                            old_msg = await dm_channel.fetch_message(int(mid.strip()))
                            await old_msg.delete()
                            await asyncio.sleep(0.1)
                        except (discord.NotFound, discord.HTTPException):
                            pass

                # Summary DM
                m, s = abs(diff) // 60, abs(diff) % 60
                status = "early" if diff < 0 else "late"
                summary_content = (
                    f"🛑 **Event Manually Stopped**\n"
                    f"📝 **Event Name:** '{actual_event_name}'\n"
                    f"📅 **Scheduled Date:** {target_dt.strftime('%A, %B %d, %Y')}\n"
                    f"⏰ **Scheduled Time:** {target_dt.strftime('%H:%M')}\n"
                    f"───────────────────\n"
                    f"✅ **Status:** Checked in successfully!\n"
                    f"⏱️ **Metrics:** Marked as **{m}m {s}s {status}**."
                )
                try:
                    await member.send(summary_content)
                except discord.Forbidden:
                    pass

                success_count += 1
            except Exception as e:
                print(f"[Stop] Failed for {member.name}: {e}")

    if success_count == 0:
        return await interaction.response.send_message(
            f"❌ No active entries for '**{actual_event_name}**' found.", ephemeral=True
        )

    m, s   = abs(last_diff) // 60, abs(last_diff) % 60
    status = "Early" if last_diff < 0 else "Late"
    await interaction.response.send_message(
        f"Stopped '**{actual_event_name}**' for **{success_count}** member(s). "
        f"Status: **{status}** ({m}m {s}s)."
    )

async def send_tracked_dm(user, eid, embed=None, content=None, view=None):
    """Sends a DM to a user and automatically tracks its message ID in the database."""
    try:
        dm_msg = await user.send(content=content, embed=embed, view=view)

        row = await query_db("SELECT last_dm_message_id FROM events WHERE rowid = ?", (eid,), one=True)
        if row and row[0]:
            new_ids = f"{row[0]},{dm_msg.id}"
        else:
            new_ids = str(dm_msg.id)
            
        await query_db("UPDATE events SET last_dm_message_id = ? WHERE rowid = ?", (new_ids, eid))
        return dm_msg
    except Exception as e:
        print(f"❌ Failed to send or track DM for event {eid}: {e}")
        return None

#buttons
class ClearConfirm(ui.View):
    def __init__(self):
        super().__init__(timeout=30) # expire after 30s
        self.value = None

    @ui.button(label="Confirm Delete", style=ButtonStyle.danger)
    async def confirm(self, interaction: Interaction, button: ui.Button):
        self.value = True
        self.stop()

    @ui.button(label="Cancel", style=ButtonStyle.secondary)
    async def cancel(self, interaction: Interaction, button: ui.Button):
        self.value = False
        self.stop()

class DeleteConfirm(ui.View):
    def __init__(self):
        super().__init__(timeout=20)
        self.value = None

    @ui.button(label="Confirm Delete", style=ButtonStyle.danger)
    async def confirm(self, interaction: Interaction, button: ui.Button):
        self.value = True
        self.stop()

    @ui.button(label="Cancel", style=ButtonStyle.secondary)
    async def cancel(self, interaction: Interaction, button: ui.Button):
        self.value = False
        self.stop()



class CheckInView(discord.ui.View):
    def __init__(self, event_id: int = None, end_time_str: str = None):
        super().__init__(timeout=None)
        self.event_id = event_id
        self.end_time_str = end_time_str

    @discord.ui.button(
        label="Check In Now", 
        style=discord.ButtonStyle.green, 
        emoji="✅", 
        custom_id="universal_remembrall_checkin_button"
    )
    async def check_in_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        eid = self.event_id
        end_time_str = self.end_time_str

        try:
            if eid is None:
                uid = str(interaction.user.id)
                one_day_ago = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
                
                # We search for NULL or -9999 so that if a user clicks mid-lock or right after a crash, it's catchable
                row = await query_db(
                    """SELECT e.rowid, s.end_time_24h 
                       FROM events e
                       LEFT JOIN schedules s ON e.user_id = s.user_id AND e.name = s.name
                       WHERE e.user_id = ? AND (e.lateness IS NULL OR e.lateness = -9999) AND e.time >= ?
                       ORDER BY e.time ASC LIMIT 1""", 
                    (uid, one_day_ago), one=True
                )
                if row:
                    eid = row[0]
                    end_time_str = row[1] if row[1] else None
                else:
                    return await interaction.response.send_message(
                        "❌ Could not resolve an active event within the last 24 hours.", 
                        ephemeral=True
                    )

            await query_db("UPDATE events SET lateness = -9999 WHERE rowid = ? AND lateness IS NULL", (eid,))
            
            row = await query_db(
                "SELECT name, time, lateness, guild_id, grace_minutes FROM events WHERE rowid = ?", 
                (eid,), one=True
            )
     
            if not row:
                return await interaction.response.send_message("❌ Event record not found in database.", ephemeral=True)
            
            if isinstance(row, dict):
                name = row.get("name")
                timestamp = row.get("time")
                current_lateness = row.get("lateness")
                guild_id = row.get("guild_id")
                grace_minutes = row.get("grace_minutes")
            else:
                name, timestamp, current_lateness, guild_id, grace_minutes = row[0], row[1], row[2], row[3], row[4]

            if current_lateness is not None and current_lateness != -9999:
                return await interaction.response.send_message("⚠️ You have already checked in for this event!", ephemeral=True)


            await interaction.response.edit_message(
                content="⏳ **Processing check-in and managing message history...**", 
                embed=None, 
                view=None
            )
     
            now = datetime.now()
            try:
                event_dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M")
            except ValueError:
                return await interaction.edit_original_response(content="❌ Event timestamp data is malformed.")
     
            # Deadline verification check
            if end_time_str and end_time_str not in ["TXT", "NONE", "None"]:
                try:
                    deadline = datetime.strptime(f"{event_dt.strftime('%Y-%m-%d')} {end_time_str}", "%Y-%m-%d %H:%M")
                    if now > deadline:
                        await query_db("UPDATE events SET lateness = NULL WHERE rowid = ?", (eid,))
                        return await interaction.edit_original_response(content="❌ **Interaction Expired**: This event has already concluded.")
                except ValueError:
                    pass  

            diff = int((now - event_dt).total_seconds())
            grace_minutes = grace_minutes or 0
            if 0<=diff<= (grace_minutes * 60):
                diff = 0
            elif diff> (grace_minutes *60):
                diff = diff - grace_minutes*60
            await query_db("UPDATE events SET lateness = ? WHERE rowid = ?", (diff, eid))
     
            m, s = abs(diff) // 60, abs(diff) % 60
            status = "early" if diff < 0 else "late"
            await asyncio.sleep(0.5)
            id_row = await query_db("SELECT last_dm_message_id FROM events WHERE rowid = ?", (eid,), one=True)
            last_msg_val = id_row.get("last_dm_message_id") if isinstance(id_row, dict) else id_row[0] if id_row else None

            if last_msg_val:
                msg_ids = list(set(last_msg_val.split(",")))
                dm_channel = interaction.channel or await interaction.user.create_dm()
                clicked_msg_id = str(interaction.message.id)

                for mid in msg_ids:
                    if mid != clicked_msg_id:
                        try:
                            old_msg = await dm_channel.fetch_message(int(mid))
                            await old_msg.delete()
                            await asyncio.sleep(0.15)
                        except:
                            pass

            is_quick = end_time_str in ["TXT", "NONE", "None"] or end_time_str is None
            prefix = "**Event Check-in**" if is_quick else "🗓️ **Scheduled Event Check-in**"
            
            formatted_date = event_dt.strftime("%A, %B %d, %Y")
            formatted_time = event_dt.strftime("%H:%M")

            summary_content = (
                f"{prefix}\n"
                f"📝 **Event Name:** '{name}'\n"
                f"📅 **Scheduled Date:** {formatted_date}\n"
                f"⏰ **Scheduled Time:** {formatted_time}\n"
                f"───────────────────\n"
                f"✅ **Status:** Checked in successfully!\n"
                f"⏱️ **Metrics:** Marked as **{m}m {s}s {status}**."
            )

            await interaction.edit_original_response(content=summary_content)
     
            guild = bot.get_guild(int(guild_id))
            if guild:
                chan = await get_log_channel(guild)
                if chan:
                    await chan.send(f"🕒 **{interaction.user.display_name}** checked in! ({m}m {s}s {status} for '**{name}**')")

        except Exception as e:
            print(f"💥 CRITICAL ERROR inside check_in_button: {e}")
#bot setup

intents = discord.Intents.default()
intents.voice_states    = True
intents.members         = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

class EventGroup(app_commands.Group, name="event"): pass
class AdminGroup(app_commands.Group, name="admin"):  pass

event_menu = EventGroup()
admin_menu = AdminGroup()

async def reminder_suggester(interaction: discord.Interaction, current: str):
    presets = ["5", "15", "30", "60"]
    
    choices = []

    if current.isdigit():
        choices.append(app_commands.Choice(name=f"{current} minutes", value=int(current)))

    for opt in presets:
        if current not in opt or any(c.name.startswith(opt) for c in choices):
            continue
        choices.append(app_commands.Choice(name=f"{opt} minutes", value=int(opt)))

    return choices[:25]

class AdvancedMemberPicker(discord.ui.View):
    def __init__(self, name, dt_str, notes, checkin_opt, reminder_offset, gid, grace_minutes):
        super().__init__(timeout=120) # 2 minutes for advanced setup
        self.name = name
        self.dt_str = dt_str
        self.notes = notes
        self.checkin_opt = checkin_opt
        self.reminder_offset = reminder_offset
        self.gid = gid
        self.grace_minutes = grace_minutes
        self.targets = set()
        self.message = None # For timeout cleanup

    @discord.ui.select(cls=discord.ui.MentionableSelect, placeholder="Select members or roles to add...", min_values=0, max_values=25)
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.MentionableSelect):
        self.targets = set()
        for entity in select.values:
            if isinstance(entity, discord.Member):
                if not entity.bot: self.targets.add(entity)
            elif isinstance(entity, discord.Role):
                self.targets.update(m for m in entity.members if not m.bot)
        
        await interaction.response.defer()

    @discord.ui.button(label="Create Advanced Event", style=discord.ButtonStyle.blurple)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.targets:
            self.targets.add(interaction.user)

        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(content="⏳ **Scheduling advanced event and sending DMs...**", view=self)

        mentions = []
        clean_notes = self.notes[:100] if self.notes else None

        for member in self.targets:
            await query_db(
                "INSERT INTO events (guild_id, user_id, username, name, time, lateness, dm_sent, checkin_options, notes, reminder_offset, last_reminder_time, grace_minutes) "
                "VALUES (?, ?, ?, ?, ?, NULL, 0, ?, ?, ?, NULL,?)", 
                (self.gid, str(member.id), member.name, self.name, self.dt_str, self.checkin_opt, clean_notes, self.reminder_offset, self.grace_minutes)
            )
            
            last_row = await query_db("SELECT last_insert_rowid()", one=True)
            event_id = last_row[0]

            try:
                view = CheckInView(event_id=event_id)
                dm_text = f"📅 **Advanced Event Scheduled:** '{self.name}'\n⏰ Time: **{self.dt_str}**"
                if self.notes:
                    dm_text += f"\n📝 **Notes:** {self.notes}"
                if self.grace_minutes:
                    dm_text += f"\n⏳ **Grace Period:** {self.grace_minutes} minutes"
                
                dm_text += "\n\nUse the button below to check in when the event starts!"
                
                await send_tracked_dm(member, event_id, content = dm_text, view=view)
            except discord.Forbidden:
                print(f"Could not DM {member.name}")

            mentions.append(member.mention)

        mode_txt = "VC or Button" if self.checkin_opt == 1 else "Button Only"
        
        await interaction.followup.send(
            f"📅 **Advanced Event Created!**\n"
            f"**Name:** {self.name}\n"
            f"**Time:** {self.dt_str}\n"
            f"**Method:** {mode_txt} | **Reminder:** {self.reminder_offset}m\n"
            f"**Grace:** {self.grace_minutes}m\n" if self.grace_minutes else ""
            f"**Participants:** {' '.join(mentions)}",
            ephemeral=False
        )

    async def on_timeout(self):
        """Greys out the menu if they walk away"""
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(content="❌ **Setup Timed Out:** Command expired.", view=self)
            except:
                pass

# events

@event_menu.command(name="create", description="Schedule an event")
@app_commands.describe( time="Select or type time (e.g. 14:30)", month="Optional: Change month (Defaults to current)", day="Optional: Change day (Defaults to today)", notes="Extra details (Optional) (Max: 100 characters)", checkin_opt="How members should check in (Optional)",  reminder_offset="(default = 30 min) Minutes late before a warning (Optional)", grace_minutes = "(default = 0) Grace Period before latness starts (Optional)")
@app_commands.choices(checkin_opt=[app_commands.Choice(name="Button only (Primary)", value=0),  app_commands.Choice(name="Additional VC", value=1)])
@app_commands.autocomplete(time=time_suggester, reminder_offset=reminder_suggester)
async def create_advanced(interaction: Interaction,  name: str,  time: str, month: int = None,  day: int = None, year: int = None,notes: app_commands.Range[str, 0, 100] = None,  checkin_opt: int = 0, reminder_offset: int = 30, grace_minutes: app_commands.Range[int, 0, 59] = 0):
    
    now = datetime.now()
    
    try:
        clean_time = time.replace(":", "").replace(".", "")
        if len(clean_time) == 3: clean_time = "0" + clean_time
        h, m = int(clean_time[:2]), int(clean_time[2:])
        if not (0 <= h < 24 and 0 <= m < 60): raise ValueError
    except:
        return await interaction.response.send_message("❌ **Invalid Time:** Use HH:MM format.", ephemeral=True)

    y = year or now.year
    mon = month or now.month
    d = day or now.day

    try:
        target_dt = datetime(y, mon, d, h, m)
    except ValueError:
        return await interaction.response.send_message("❌ **Invalid Date:** That day doesn't exist in that month.", ephemeral=True)


    if day is None and month is None and target_dt < now:
        target_dt += timedelta(days=1)

    dt_str = target_dt.strftime("%Y-%m-%d %H:%M")
    
    view = AdvancedMemberPicker(
        name=sanitize_text(name), dt_str=dt_str, notes=sanitize_text(notes), 
        checkin_opt=checkin_opt, reminder_offset=reminder_offset, 
        gid=str(interaction.guild.id), grace_minutes = grace_minutes
    )

    await interaction.response.send_message(
        f"📅 **Setting up '{name}'** for **{dt_str}**\n"
        "Who should be added? (Select below or click confirm for just you)",
        view=view, ephemeral=True
    )
    view.message = await interaction.original_response()

class QuickMemberPicker(discord.ui.View):
    def __init__(self, name, dt_str, minutes, checkin_opt, reminder_offset, gid, grace_minutes):
        super().__init__(timeout=60)
        self.name, self.dt_str = name, dt_str
        self.minutes = minutes
        self.checkin_opt = checkin_opt
        self.reminder_offset = reminder_offset
        self.gid = gid
        self.grace_minutes = grace_minutes
        self.targets = set()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        
        if self.message:
            try:
                await self.message.edit(content="❌ **Setup Timed Out:** Please run the command again.", view=self)
            except:
                pass
    
    @discord.ui.select(cls=discord.ui.MentionableSelect, placeholder="Add others (Optional)...", min_values=0, max_values=25)
    async def select_callback(self, interaction: Interaction, select: discord.ui.MentionableSelect):
        self.targets = set()
        for entity in select.values:
            if isinstance(entity, discord.Member) and not entity.bot:
                self.targets.add(entity)
            elif isinstance(entity, discord.Role):
                self.targets.update(m for m in entity.members if not m.bot)
        await interaction.response.defer()

    @discord.ui.button(label="Confirm & Start", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: Interaction, button: discord.ui.Button):
        if not self.targets:
            self.targets.add(interaction.user)

        for item in self.children:
            item.disabled = True
        
        await interaction.response.edit_message(content="✅ Event processed. This menu is now closed.", view=self)

        mentions = []
        for member in self.targets:
            await query_db(
                "INSERT INTO events (guild_id, user_id, username, name, time, lateness, dm_sent, checkin_options, reminder_offset, last_reminder_time, grace_minutes) "
                "VALUES (?, ?, ?, ?, ?, NULL, 0, ?, ?, NULL, ?)",
                (self.gid, str(member.id), member.name, self.name, self.dt_str, self.checkin_opt, self.reminder_offset, self.grace_minutes)
            )

            last_row = await query_db("SELECT last_insert_rowid()", one=True)
            event_id = last_row[0]
            try:
                view = CheckInView(event_id=event_id)
                await send_tracked_dm(member, event_id, content = (
                    f"⚡ **Quick Event:** '{self.name}'\n"
                    f"⏰ Time: **{self.dt_str}**"
                    + (f"\n⏳ **Grace Period:** {self.grace_minutes} minutes" if self.grace_minutes else "")
                    +"\n\nClick the button below to check in!"),
                    view=view
                )
            except discord.Forbidden:
                print(f"Could not DM {member.name}")

            mentions.append(member.mention)

        h, m = self.minutes // 60, self.minutes % 60
        dur = f"{h}h {m}m" if h > 0 else f"{m}m"
        
        await interaction.followup.send(
            f"✅ Quick event '**{self.name}**' set for **{self.dt_str}** ({dur} from now)!\n"
            + (f"**Grace:** {self.grace_minutes}m" if self.grace_minutes else "")
            +f"**Participants:** {' '.join(mentions)}",
            ephemeral=False
        )


@event_menu.command(name="create_quick", description="Quick: 2-step event setup for 'now'")
@app_commands.describe(name="Name of the event",minutes="How many minutes from now it starts",checkin_opt="How members should check in (Optional)", reminder_offset="(default = 30 min) Minutes late before a warning (Optional)", grace_minutes = "(default = 0) Grace Period before latness starts (Optional)")
@app_commands.autocomplete(reminder_offset=reminder_suggester)
@app_commands.choices(checkin_opt=[app_commands.Choice(name="Button only (Primary)", value=0),  app_commands.Choice(name="Additional VC", value=1)])
async def create_quick(interaction: Interaction,   name: str,   minutes: int, checkin_opt: int = 0, reminder_offset: int = 30, grace_minutes: app_commands.Range[int, 0, 59] = 0):

    if minutes < 0:
        return await interaction.response.send_message("❌ Minutes cannot be negative!", ephemeral=True)
    
    if reminder_offset < 1:
        reminder_offset = 30

    now = datetime.now()
    future_time = now + timedelta(minutes=minutes)
    dt_str = future_time.strftime("%Y-%m-%d %H:%M")
    gid = str(interaction.guild.id)

    view = QuickMemberPicker(name=sanitize_text(name), dt_str=dt_str,  minutes=minutes, checkin_opt=checkin_opt, reminder_offset=reminder_offset, gid=gid, grace_minutes = grace_minutes)

    await interaction.response.send_message(f"⚡ Setting up **{name}** for **{dt_str}**.\n"
                                            f"Who should be added? (Select below or click confirm for just you)"
                                            , view=view,ephemeral=True )
    view.message = await interaction.original_response()

@event_menu.command(name="list", description="View server or personal events with advanced filters")
@app_commands.choices(scope=[ app_commands.Choice(name="My Events Only", value="mine"), app_commands.Choice(name="Everyone's Events (Server Board)", value="server")])
@app_commands.choices(timeframe=[ app_commands.Choice(name="All Events", value="all"),app_commands.Choice(name="Today Only", value="today"), app_commands.Choice(name="This Week", value="week")])
async def list_events( interaction: Interaction,  scope: str = "mine",          timeframe: str = "all",     date_search: str = None,    member: discord.Member = None ):
    await interaction.response.defer() # Better safety in case database grows
    
    guild_id = str(interaction.guild.id)
    now_str = datetime.now().strftime("%Y-%m-%d")
    
    query = "SELECT user_id, username, name, time, lateness, notes, rowid, dm_sent FROM events WHERE guild_id = ?"
    params = [guild_id]
    
    if member:
        query += " AND user_id = ?"
        params.append(str(member.id))
    elif scope == "mine":
        query += " AND user_id = ?"
        params.append(str(interaction.user.id))
        
    if date_search:
        query += " AND time LIKE ?"
        params.append(f"{escape_like(date_search.strip())}%")
    elif timeframe == "today":
        query += " AND time LIKE ?"
        params.append(f"{now_str}%")
    elif timeframe == "week":
        start_bound = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d 00:00")
        end_bound = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d 23:59")
        query += " AND time BETWEEN ? AND ?"
        params.extend([start_bound, end_bound])

    query += " ORDER BY user_id ASC, time ASC"
    
    rows = await query_db(query, tuple(params))
    if not rows:
        return await interaction.followup.send("📅 No events found matching your filter criteria.", ephemeral=True)
        
    title_map = {"all": "All", "today": "Today's", "week": "This Week's"}
    if date_search:
        title = f"Filtered ({date_search})"
    else:
        title = title_map[timeframe]
        
    if scope == "server" and not member:
        msg = f" **{interaction.guild.name} {title} Event Board**\n"
    else:
        target_name = member.display_name if member else interaction.user.display_name
        msg = f"📅 **{target_name}'s {title} Events:**\n"
        
    curr_user = None
    msg_lines = [msg]

    
    for uid, uname, name, timestamp, late, notes, _, dm_sent in rows:
        if scope == "server" and not member and uid != curr_user:
            curr_user = uid
            msg_lines.append(f"\n👤 **{uname or f'<@{uid}>'}**")
            
        if late is not None:
            m, s = abs(late) // 60, abs(late) % 60
            time_str = f"{m}m {s}s"
            emoji = "✅ Early:" if late < 0 else ("⏱️ On Time:" if late == 0 else "⚠️ Late:")
            status = f"{timestamp} {emoji} {time_str}"
        else:
            status = f"{timestamp} ⏳ Ongoing" if (late is None and dm_sent and dm_sent >= 1) else f"🕒 {timestamp}"
            
        msg_lines.append(f" └ **{name}** — {status}")
        
        if (scope == "mine" or member) and notes and notes.strip():
            display_note = (notes[:97] + '...') if len(notes) > 100 else notes
            msg_lines.append(f"   └ *Note: {display_note}*")
            
    final_msg = "\n".join(msg_lines)
    if len(final_msg) > 2000:
        final_msg = final_msg[:1990] + "\n..."
        
    await interaction.followup.send(final_msg)

@event_menu.command(name="stop", description="Stop an active event")
@app_commands.autocomplete(event_name=event_autocomplete)
async def stop(interaction: discord.Interaction, event_name: str,member: discord.Member = None, role: discord.Role = None):
    has_targets = any([member, role])
    
    if has_targets and not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ You do not have permission to stop other users' events.", ephemeral=True)
    
    members_list = [member] if member else []
    await execute_stop_logic(interaction, event_name, members_list, role)



@event_menu.command(name="delete", description="Delete one of your event records")
@app_commands.autocomplete(event_name=event_autocomplete)
async def delete_event(interaction: discord.Interaction, event_name: str):
    if not event_name.isdigit():
        return await interaction.response.send_message("❌ Invalid selection.", ephemeral=True)
    
    uid = str(interaction.user.id)
    row = await query_db("SELECT name, time, last_dm_message_id FROM events WHERE rowid = ? AND user_id = ?", (validate_rowid(event_name), uid), one=True) 
    if not row:
        return await interaction.response.send_message("❌ Record not found.", ephemeral=True)
    if isinstance(row, dict):
        name_val = row.get("name")
        time_val = row.get("time")
        msg_id = row.get("last_dm_message_id")
    else:
        name_val, time_val, msg_id = row[0], row[1], row[2]
    event_label = f"**{name_val}** ({time_val})"
    view = DeleteConfirm()
    await interaction.response.send_message(f"⚠️ Are you sure you want to delete the record for {event_label}?",view=view,ephemeral=True)
    await view.wait()
    if view.value is None:
        await interaction.edit_original_response(content=" Request timed out.", view=None)
    elif view.value:
        if msg_id:
            try:
                target_user = await bot.fetch_user(int(uid))
                dm_channel = await target_user.create_dm()
                for mid in msg_id.split(","):
                    try:
                        old_msg = await dm_channel.fetch_message(int(mid.strip()))
                        await old_msg.delete()
                        await asyncio.sleep(0.1)
                    except (discord.NotFound, discord.HTTPException):
                        pass
            except Exception as e:
                print(f"Could not clean up DM for deleted event: {e}")
                    
        await query_db("DELETE FROM events WHERE rowid = ?", (validate_rowid(event_name),))
        await interaction.edit_original_response(content=f" Deleted {event_label}.", view=None)
    else:
        await interaction.edit_original_response(content="❌ Deletion cancelled.", view=None)

# @event_menu.command(name="clear_all", description="Clear all your events in this server")
# async def clear(interaction: Interaction):
#     view = ClearConfirm()
    
#     await interaction.response.send_message("⚠️ **Are you sure?** This will permanently delete all your event history in this server.",view=view,ephemeral=True)
#     await view.wait()

#     if view.value is None:
#         await interaction.edit_original_response(content=" Request timed out.", view=None)
#     elif view.value:
#         await query_db(
#             "DELETE FROM events WHERE user_id = ? AND guild_id = ?", 
#             (str(interaction.user.id), str(interaction.guild.id))
#         )
#         await interaction.edit_original_response(content=" All your events in this server have been cleared.", view=None)
#     else:
#         await interaction.edit_original_response(content="❌ Clear cancelled.", view=None)

@event_menu.command(name="clear_all", description="Clear your events in this server with advanced filters")
@app_commands.choices(timeframe=[app_commands.Choice(name="Wipe EVERYTHING", value="all"), app_commands.Choice(name="Wipe Today's Events Only", value="today"),app_commands.Choice(name="Wipe This Week's Events Only", value="week")])
async def clear_self(interaction: Interaction,  timeframe: str = "all",date_search: str = None ):
    view = ClearConfirm()
    guild_id = str(interaction.guild.id)
    user_id = str(interaction.user.id)
    now_str = datetime.now().strftime("%Y-%m-%d")
    
    if date_search:
        label = f"events matching date '{date_search.strip()}'"
    else:
        labels_map = {"all": "EVERYTHING", "today": "Today's events", "week": "this week's events"}
        label = labels_map[timeframe]
    
    await interaction.response.send_message(
        f"⚠️ **Are you sure?** This will permanently delete **{label}** from your history in this server.",
        view=view,
        ephemeral=True
    )
    
    await view.wait()

    if view.value is None:
        return await interaction.edit_original_response(content="⏳ Request timed out.", view=None)
    if not view.value:
        return await interaction.edit_original_response(content="❌ Clear cancelled.", view=None)

    query = "DELETE FROM events WHERE user_id = ? AND guild_id = ? "
    params = [user_id, guild_id]
    
    if date_search:
        query += " AND time LIKE ?"
        params.append(f"{escape_like(date_search.strip())}%")
    elif timeframe == "today":
        query += " AND time LIKE ?"
        params.append(f"{now_str}%")
    elif timeframe == "week":
        one_week_later = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
        query += " AND time BETWEEN ? AND ?"
        params.extend([f"{now_str} 00:00", one_week_later])

    await query_db(query, tuple(params))
    await interaction.edit_original_response(content=f"✅ Done! Your {label} have been successfully cleared.", view=None )

@event_menu.command(name="add_schedule", description="Set a recurring weekly event")
@app_commands.describe( start_time="HH:MM (e.g. 14:00)",end_time="HH:MM (e.g. 16:00)",checkin_opt="How members should check in",notes="Extra details (Max 100)", reminder_offset="(default = 30 min) Minutes late before a warning (Optional)", grace_minutes = "(default = 0) Grace Period before latness starts (Optional)")
@app_commands.autocomplete(start_time=time_suggester, end_time=time_suggester, reminder_offset=reminder_suggester)
@app_commands.choices(day=[app_commands.Choice(name=d, value=i) for i, d in enumerate(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])])
async def add_schedule(interaction: Interaction,  name: str,  day: int, start_time: str, end_time: str,  checkin_opt: int = 0,  notes: app_commands.Range[str, 0, 100] = None, reminder_offset: int = 30, grace_minutes: int = 0):
    
    def parse_time(t_str):
        t_clean = t_str.replace(":", "").replace(".", "")
        if len(t_clean) == 3: t_clean = "0" + t_clean
        h, m = int(t_clean[:2]), int(t_clean[2:])
        if not (0 <= h < 24 and 0 <= m < 60): raise ValueError
        return f"{h:02d}:{m:02d}"

    try:
        final_start = parse_time(start_time)
        final_end = parse_time(end_time)
    except:
        return await interaction.response.send_message("❌ **Invalid Time Format:** Use HH:MM (e.g. 14:30).", ephemeral=True)

    await query_db(
        "INSERT INTO schedules (guild_id, user_id, username, name, day_of_week, time_24h, end_time_24h, checkin_options, notes, reminder_offset, grace_minutes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,?)",
        (str(interaction.guild.id), str(interaction.user.id), interaction.user.name, sanitize_text(name), day, final_start, final_end, checkin_opt, sanitize_text(notes), reminder_offset, grace_minutes)
    )
    
    days_list = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    
    msg = [
        f"🗓️ Recurring event **{name}** set for every **{days_list[day]}**",
        f"⏰ **Time:** {final_start} - {final_end}",
        f"🔔 **Reminder:** {reminder_offset}m late"
        
    ]
    if notes:
        msg.append(f"📝 **Note:** {notes}")
    if grace_minutes:
        msg.append(f"⏳ **Grace:** {grace_minutes}m")
        
    await interaction.response.send_message("\n".join(msg))

@event_menu.command(name="delete_schedule", description="Delete a recurring schedule")
@app_commands.autocomplete(name=event_autocomplete)
async def delete_schedule(interaction: Interaction, name: str):
    if not name.isdigit():
        return await interaction.response.send_message("❌ Invalid selection.", ephemeral=True)
    
    uid = str(interaction.user.id)

    row = await query_db(
        "SELECT name, day_of_week, time_24h FROM schedules WHERE rowid = ? AND user_id = ?", 
        (int(name), uid), 
        one=True
    )
    
    if not row:
        return await interaction.response.send_message("❌ Schedule record not found.", ephemeral=True)

    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    schedule_info = f"**{row[0]}** (Every {days[row[1]]} at {row[2]})"
    view = DeleteConfirm() 

    await interaction.response.send_message(f"⚠️ Are you sure you want to delete this recurring schedule?\n> {schedule_info}",view=view, ephemeral=True)
    await view.wait()
    if view.value is None:
        await interaction.edit_original_response(content=" Request timed out.", view=None)
    elif view.value:
        await query_db("DELETE FROM schedules WHERE rowid = ?", (int(name),))
        await interaction.edit_original_response(content=f" Deleted schedule: {schedule_info}", view=None)
    else:
        await interaction.edit_original_response(content="❌ Deletion cancelled.", view=None)

@event_menu.command(name="list_schedule", description="View all your recurring schedules in this server")
async def list_schedule(interaction: Interaction):
    gid, uid = str(interaction.guild.id), str(interaction.user.id)

    rows = await query_db("SELECT name, day_of_week, time_24h FROM schedules WHERE user_id = ? AND guild_id = ? ORDER BY day_of_week, time_24h",(uid, gid))
    
    if not rows:
        return await interaction.response.send_message("You don't have any recurring schedules set up yet. Use `/add_schedule` to create one!", ephemeral=True )

    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    schedule_list = "🗓️ **Your Recurring Schedules:**\n"
    for name, day_idx, time_str in rows:
        schedule_list += f"• **{name}**: Every {days[day_idx]} at {time_str}\n"

    await interaction.response.send_message(schedule_list, ephemeral=True)

# @event_menu.command(name="predict", description="AI Predict lateness")
# @app_commands.autocomplete(event_name=event_autocomplete)
# async def predict_lateness(interaction: Interaction, event_name: str, member: discord.Member = None):
#     target = member or interaction.user
    
#     row = await query_db(
#         "SELECT name, time FROM events WHERE rowid = ?", 
#         (event_name,),
#         one=True
#     )
#     if not row:
#         return await interaction.response.send_message(
#             "❌ Could not find that specific event record.", ephemeral=True
#         )

#     actual_name = row[0]
#     event_time  = row[1]

#     pred_res, lower_res, upper_res = ai_pipeline.predict_with_confidence(
#         user_id=str(target.id), 
#         event_name=actual_name, 
#         event_time=event_time
#     )

#     if pred_res is None:
#         return await interaction.response.send_message(
#             f"❌ Not enough historical data for **{target.display_name}** on event '**{actual_name}**'.", 
#             ephemeral=True
#         )

#     def fmt(mins):
#         total = abs(int(mins * 60))
#         m, s = divmod(total, 60)    #min, sec
#         status = "Early" if mins < 0 else "Late"
#         return f"{m}m {s}s {status}"

#     await interaction.response.send_message(
#         f"🔮 Prediction: **{target.display_name}** is going to be **{fmt(pred_res[0])}** for '**{actual_name}**'."
#     )

@event_menu.command(name="predict", description="AI Predict lateness with confidence range")
@app_commands.autocomplete(event_name=event_autocomplete)
async def predict_lateness(interaction: Interaction, event_name: str, member: discord.Member = None):
    gid = str(interaction.guild.id)
    

    row = await query_db("SELECT name, time, user_id, username FROM events WHERE rowid = ? AND guild_id = ?",  (event_name, gid), one=True )
    
    if not row:
        return await interaction.response.send_message( "❌ Could not find that specific ongoing event.", ephemeral=True)

    actual_name, event_time, event_owner_id, event_owner_name = row
    
    target_id = str(member.id) if member else event_owner_id
    target_name = member.display_name if member else event_owner_name


    pred_res, lower_res, upper_res = ai_pipeline.predict_with_confidence(user_id=target_id, event_name=actual_name, event_time=event_time )

    if pred_res is None:
        return await interaction.response.send_message( f"❌ Not enough historical data to predict for **{target_name}**.",   ephemeral=True)

    def fmt(mins):
        val = float(mins[0]) if isinstance(mins, (list, np.ndarray)) else float(mins)
        total_seconds = abs(int(val * 60))
        m, s = divmod(total_seconds, 60)
        status = "Early" if val < 0 else "Late"
        return f"{m}m {s}s {status}"

    prediction_text = (
        f"🔮 **AI Prediction** for '**{actual_name}**'\n"
        f"👤 Target: **{target_name}**\n"
        f"⏱️ **Expected**: {fmt(pred_res)}\n"
        f"📉 **Range**: {fmt(lower_res)} — {fmt(upper_res)}"
    )

    await interaction.response.send_message(prediction_text)

#admin stuff

class AdminActionPicker(discord.ui.View):
    def __init__(self, mode, event_name, actual_name, gid, default_member: discord.Member = None):
        super().__init__(timeout=60)
        self.mode = mode # "delete", "clear", or "stop"
        self.event_name = event_name
        self.actual_name = actual_name
        self.gid = gid
        self.targets = set()
        self.message = None

        if default_member and not default_member.bot:
            self.targets.add(default_member)

    @discord.ui.select(cls=discord.ui.MentionableSelect, placeholder="Select targets (members or roles)...", min_values=1, max_values=25)
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.MentionableSelect):

        self.targets = set()
        for entity in select.values:
            if isinstance(entity, discord.Member) and not entity.bot:
                self.targets.add(entity)
            elif isinstance(entity, discord.Role):
                self.targets.update(m for m in entity.members if not m.bot)
        await interaction.response.defer()

    @discord.ui.button(label="Confirm Action", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):

        if not self.targets:
            return await interaction.response.send_message("❌ No targets selected.", ephemeral=True)

        for item in self.children: item.disabled = True
        await interaction.response.edit_message(content=f"⏳ **Processing {self.mode}...**", view=self)

        count = 0
        for member in self.targets:
            uid = str(member.id)
            if self.mode == "delete":
                if self.event_name.isdigit():
                    row = await query_db("SELECT last_dm_message_id FROM events WHERE rowid = ?", (int(self.event_name),), one=True)
                    msg_ids = [row[0]] if row and row[0] else []
                    await query_db("DELETE FROM events WHERE rowid = ?", (int(self.event_name),))
                else:
                    rows = await query_db(
                        "SELECT last_dm_message_id FROM events WHERE user_id = ? AND guild_id = ? AND name = ?",
                        (uid, self.gid, self.actual_name)
                    )
                    msg_ids = [r[0] for r in rows if r and r[0]]
                    await query_db("DELETE FROM events WHERE user_id = ? AND guild_id = ? AND name = ?", (uid, self.gid, self.actual_name))

                try:
                    target_user = await bot.fetch_user(int(uid))
                    dm_channel = await target_user.create_dm()
                    for msg_id_str in msg_ids:
                        for mid in msg_id_str.split(","):
                            try:
                                old_msg = await dm_channel.fetch_message(int(mid.strip()))
                                await old_msg.delete()
                                await asyncio.sleep(0.1)
                            except (discord.NotFound, discord.HTTPException):
                                pass
                except Exception as e:
                    print(f"Could not clean up DMs for admin delete: {e}")

            elif self.mode == "clear":
                await query_db("DELETE FROM events WHERE user_id = ? AND guild_id = ?", (uid, self.gid))
                await query_db("DELETE FROM schedules WHERE user_id = ? AND guild_id = ?", (uid, self.gid))
            elif self.mode == "stop":
                if self.event_name.isdigit():
                    now = datetime.now()
                    if self.event_name.isdigit():
                        row = await query_db(
                            "SELECT time, last_dm_message_id FROM events WHERE rowid = ?",
                            (int(self.event_name),), one=True
                        )
                        if row:
                            time_str, msg_id_str = row[0], row[1]
                            try:
                                event_dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
                                diff = int((now - event_dt).total_seconds())
                                await query_db("UPDATE events SET lateness = ? WHERE rowid = ?", (diff, int(self.event_name)))
                            except ValueError:
                                diff, event_dt = 0, now

                            if msg_id_str:
                                try:
                                    dm_channel = await member.create_dm()
                                    for mid in msg_id_str.split(","):
                                        try:
                                            old_msg = await dm_channel.fetch_message(int(mid.strip()))
                                            await old_msg.delete()
                                            await asyncio.sleep(0.1)
                                        except (discord.NotFound, discord.HTTPException):
                                            pass
                                except Exception as e:
                                    print(f"Could not clean up DMs for admin stop: {e}")

                            m, s = abs(diff) // 60, abs(diff) % 60
                            status = "early" if diff < 0 else "late"
                            try:
                                await member.send(
                                    f"🛑 **Event Manually Stopped**\n"
                                    f"📝 **Event Name:** '{self.actual_name}'\n"
                                    f"📅 **Scheduled Date:** {event_dt.strftime('%A, %B %d, %Y')}\n"
                                    f"⏰ **Scheduled Time:** {event_dt.strftime('%H:%M')}\n"
                                    f"───────────────────\n"
                                    f"✅ **Status:** Checked in successfully!\n"
                                    f"⏱️ **Metrics:** Marked as **{m}m {s}s {status}**."
                                )
                            except discord.Forbidden:
                                pass
                    else:
                        rows = await query_db(
                            "SELECT rowid, time, last_dm_message_id FROM events WHERE user_id = ? AND guild_id = ? AND name = ? AND lateness IS NULL",
                            (uid, self.gid, self.actual_name)
                        )
                        for rid, time_str, msg_id_str in rows:
                            try:
                                event_dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
                                diff = int((now - event_dt).total_seconds())
                                await query_db("UPDATE events SET lateness = ? WHERE rowid = ?", (diff, rid))
                            except ValueError:
                                diff, event_dt = 0, now

                            if msg_id_str:
                                try:
                                    dm_channel = await member.create_dm()
                                    for mid in msg_id_str.split(","):
                                        try:
                                            old_msg = await dm_channel.fetch_message(int(mid.strip()))
                                            await old_msg.delete()
                                            await asyncio.sleep(0.1)
                                        except (discord.NotFound, discord.HTTPException):
                                            pass
                                except Exception as e:
                                    print(f"Could not clean up DMs for admin stop: {e}")

                            m, s = abs(diff) // 60, abs(diff) % 60
                            status = "early" if diff < 0 else "late"
                            try:
                                await member.send(
                                    f"🛑 **Event Manually Stopped**\n"
                                    f"📝 **Event Name:** '{self.actual_name}'\n"
                                    f"📅 **Scheduled Date:** {event_dt.strftime('%A, %B %d, %Y')}\n"
                                    f"⏰ **Scheduled Time:** {event_dt.strftime('%H:%M')}\n"
                                    f"───────────────────\n"
                                    f"✅ **Status:** Checked in successfully!\n"
                                    f"⏱️ **Metrics:** Marked as **{m}m {s}s {status}**."
                                )
                            except discord.Forbidden:
                                pass
            count += 1

        verb = "Deleted" if self.mode == "delete" else "Wiped" if self.mode == "clear" else "Stopped"
        target_info = f"entries of '{self.actual_name}'" if self.mode != "clear" else "ALL data"

        await interaction.followup.send(f"✅ [ADMIN] {verb} {target_info} for **{count}** members.", ephemeral=True)

    async def on_timeout(self):
        for item in self.children: item.disabled = True
        if self.message:
            try: await self.message.edit(content="❌ **Action Expired.**", view=self)
            except: pass

@admin_menu.command(name="set_channel", description="Admin: Set the channel for bot announcements")
@app_commands.checks.has_permissions(manage_guild=True)
async def set_channel(interaction: Interaction, channel: discord.TextChannel):
    await query_db(
        "INSERT INTO guild_config (guild_id, log_channel_id) VALUES (?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET log_channel_id = excluded.log_channel_id",
        (str(interaction.guild.id), str(channel.id))
    )
    await interaction.response.send_message(
        f"✅ Bot announcements will now go to {channel.mention}.", ephemeral=True
    )

@admin_menu.command(name="delete", description="Admin: Delete event records")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.autocomplete(event_name=event_autocomplete)
async def admin_delete(interaction: discord.Interaction, event_name: str):
    actual_name = event_name
    default_member = None
    
    if event_name.isdigit():
        res = await query_db("SELECT name, user_id FROM events WHERE rowid = ?", (validate_rowid(event_name),), one=True)
        if res:
            actual_name = res[0]
            uid = int(res[1])
            default_member = interaction.guild.get_member(uid) or await interaction.guild.fetch_member(uid)
    else:
        res = await query_db("SELECT user_id FROM events WHERE name = ? AND guild_id = ? ORDER BY time DESC", (event_name, str(interaction.guild.id)), one=True)
        if res:
            uid = int(res[0])
            default_member = interaction.guild.get_member(uid) or await interaction.guild.fetch_member(uid)

    view = AdminActionPicker(mode="delete", event_name=event_name, actual_name=actual_name, gid=str(interaction.guild.id),default_member=default_member)
    
    target_label = f"Default Target: **{default_member.display_name}**" if default_member else "No initial target matches."
    await interaction.response.send_message(
        f"🗑️ **Delete Action**: Who should have entries of '**{actual_name}**' removed?\n"
        f"💡 _{target_label} (Use the select menu below to change or add more targets)_", 
        view=view, 
        ephemeral=True
    )
    view.message = await interaction.original_response()

@admin_menu.command(name="clear", description="Admin: Wipe event or history data")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.choices(scope=[
    app_commands.Choice(name="Wipe Specific Member/Role", value="target"),
    app_commands.Choice(name="Purge Old/Expired Events Only", value="expired"),
    app_commands.Choice(name="NUCLEAR: Wipe ALL Server Data", value="nuclear")
])
async def admin_clear(interaction: discord.Interaction, scope: str, days_old: int = None):
    guild_id = str(interaction.guild.id)

    if scope == "nuclear":
        view = AdminActionPicker(action_type="clear_all", target=None, details="EVERYTHING", gid=guild_id)
        await interaction.response.send_message(
            "⚠️ **CRITICAL WARNING**: You are about to **PERMANENTLY DELETE ALL EVENT DATA** for this entire server.\n"
            "Click below to confirm this absolute wipe.", 
            view=view, ephemeral=True
        )
        view.message = await interaction.original_response()

    elif scope == "expired":
        days = days_old or 30 # Default to 30 days if they don't specify
        view = AdminActionPicker(action_type="clear_expired", target=days, details=f"Events older than {days} days", gid=guild_id)
        await interaction.response.send_message(
            f"🧹 **Clean Up**: You are about to clear completed/late events older than **{days} days**.\n"
            "Confirm below to optimize the database.", 
            view=view, ephemeral=True
        )
        view.message = await interaction.original_response()

    else:
        view = AdminActionPicker(action_type="clear_target", target=None, details="Selected Members/Roles", gid=guild_id)
        await interaction.response.send_message(
            "❗ **Targeted Wipe**: Select the members or roles whose history you want to **COMPLETELY ERASE**.", 
            view=view, ephemeral=True
        )
        view.message = await interaction.original_response()

@admin_menu.command(name="stop", description="Admin: Stop an active event session")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.autocomplete(event_name=event_autocomplete)
@app_commands.choices(scope=[
    app_commands.Choice(name="Target specific members/roles via dropdown", value="target"),
    app_commands.Choice(name="FORCE STOP for everyone in the server", value="global")
])
async def admin_stop(interaction: discord.Interaction, event_name: str, scope: str = "target"):
    actual_name = event_name
    
    if event_name.isdigit():
        res = await query_db("SELECT name FROM events WHERE rowid = ?", (validate_rowid(event_name),), one=True)
        if res: 
            actual_name = res[0]

    if scope == "global":

        view = AdminActionPicker("stop_global", event_name, actual_name, str(interaction.guild.id))
        await interaction.response.send_message(
            f" **Force Stop**: Click below to completely end **all active sessions** of '**{actual_name}**' across the entire server.", 
            view=view, ephemeral=True
        )
    
    else:
        view = AdminActionPicker("stop", event_name, actual_name, str(interaction.guild.id))
        await interaction.response.send_message(
            f"👥 **Stop Action**: Who should have their session of '**{actual_name}**' ended?", 
            view=view, ephemeral=True
        )
        
    view.message = await interaction.original_response()

class RecordMemberPicker(discord.ui.View):
    def __init__(self, event_name, dt_str, lateness_seconds, gid, notes, admin_user: discord.Member):
        super().__init__(timeout=60)
        self.event_name = event_name
        self.dt_str = dt_str
        self.lateness_seconds = lateness_seconds
        self.gid = gid
        self.targets = {admin_user}
        self.message = None
        self.notes = notes

    @discord.ui.select(cls=discord.ui.MentionableSelect, placeholder="Select members/roles for this record...", min_values=1, max_values=25)
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.MentionableSelect):
        self.targets = set()
        for entity in select.values:
            if isinstance(entity, discord.Member) and not entity.bot:
                self.targets.add(entity)
            elif isinstance(entity, discord.Role):
                self.targets.update(m for m in entity.members if not m.bot)
        await interaction.response.defer()

    @discord.ui.button(label="Confirm & Save Record", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.targets:
            return await interaction.followup.send("❌ Select someone first!", ephemeral=True)

        for item in self.children: item.disabled = True
        await interaction.response.edit_message(content="⏳ **Writing to database...**", view=self)

        mentions = []
        m, s = abs(self.lateness_seconds) // 60, abs(self.lateness_seconds) % 60
        time_formatted = f"{m}m {s}s"
        if self.lateness_seconds < 0:
            status_str = f"**{time_formatted} early**"
        elif self.lateness_seconds == 0:
            status_str = "**on time**"
        else:
            status_str = f"**{time_formatted} late**"

        for member in self.targets:
            await query_db(
                "INSERT INTO events (guild_id, user_id, username, name, time, lateness, notes) VALUES (?, ?, ?, ?, ?, ?,?)",
                (self.gid, str(member.id), member.name, self.event_name, self.dt_str, self.lateness_seconds, self.notes)
            )
            mentions.append(member.mention)
            dm_text = (
                f"📋 **New Event Record Logged!**\n"
                f"📝 **Event Name:** '{self.event_name}'\n"
                f"📆 **Scheduled Time:** {self.dt_str}\n"
                f"-----------------------------------------\n"
                f"⏱️ **Metrics:** Marked as {status_str}."
            )
            if self.notes and self.notes.strip():
                dm_text += f"ℹ️**Note:** *{self.notes}*\n"

            try:
                await member.send(dm_text)

            except discord.Forbidden:
                print(f"Could not DM summary to {member.display_name} (DMs locked).")
        await interaction.followup.send(
            f"✅ **Record Logged!**\n"
            f"📝 **Event:** {self.event_name} ({self.dt_str})\n"
            f"⏰ **Lateness:** {time_formatted}m\n"
            f"👥 **Participants:** {', '.join(mentions)}",
            ephemeral=False
        )

    async def on_timeout(self):
        for item in self.children: item.disabled = True
        if self.message:
            try: await self.message.edit(content="❌ **Record setup timed out.**", view=self)
            except: pass

@admin_menu.command(name="add_record", description="Admin: Log a finished event record")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(event_name="Name of the event (e.g. Weekly Meeting)", time="HH:MM (e.g. 14:00)",lateness_minutes="How many minutes late? (Use 0 for on-time)",notes="Context notes for this entry",year="Optional: Custom year (e.g. 2026)",month="Optional: 1-12",day="Optional: 1-31")
@app_commands.autocomplete(time=time_suggester)
async def admin_add_record(interaction: discord.Interaction,  event_name: str,   time: str,   lateness_minutes: int, notes: str=None,  year: int = None, month: int = None,  day: int = None):
    
    now = datetime.now()

    try:
        t_clean = time.replace(":", "").replace(".", "")
        if len(t_clean) == 3: t_clean = "0" + t_clean
        h, m = int(t_clean[:2]), int(t_clean[2:])
        if not (0 <= h < 24 and 0 <= m < 60): raise ValueError
    except:
        return await interaction.response.send_message("❌ **Invalid Time:** Use HH:MM.", ephemeral=True)

    y = year or now.year
    mon = month or now.month
    d = day or now.day
    try:
        valid_dt = datetime(y, mon, d, h, m)
    except ValueError:
        return await interaction.response.send_message("❌ **Invalid Date.**", ephemeral=True)

    dt_str = valid_dt.strftime("%Y-%m-%d %H:%M")
    lateness_seconds = lateness_minutes * 60
    gid = str(interaction.guild.id)

    view = RecordMemberPicker(sanitize_text(event_name), dt_str, lateness_seconds, gid, sanitize_text(notes), admin_user = interaction.user)
    
    await interaction.response.send_message(
        f"📝 **Creating record for '{event_name}'** on **{dt_str}**\n"
        f"Lateness: **{lateness_minutes}m**\n"
        "Who is this record for?",
        view=view,
        ephemeral=True
    )
    view.message = await interaction.original_response()

class ScheduleMemberPicker(discord.ui.View):
    def __init__(self, name, day, start_t, end_t, checkin_opt, notes, reminder_offset, gid, grace_minutes):
        super().__init__(timeout=60)
        self.name = name
        self.day = day
        self.start_t = start_t
        self.end_t = end_t
        self.checkin_opt = checkin_opt
        self.notes = notes
        self.reminder_offset = reminder_offset
        self.grace_minutes= grace_minutes
        self.gid = gid
        self.targets = set()
        self.message = None

    @discord.ui.select(cls=discord.ui.MentionableSelect, placeholder="Select members or roles...", min_values=1, max_values=25)
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.MentionableSelect):
        self.targets = set()
        for entity in select.values:
            if isinstance(entity, discord.Member) and not entity.bot:
                self.targets.add(entity)
            elif isinstance(entity, discord.Role):
                self.targets.update(m for m in entity.members if not m.bot)
        await interaction.response.defer()

    @discord.ui.button(label="Confirm Schedule", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.targets:
            return await interaction.followup.send("❌ You must select at least one member/role.", ephemeral=True)

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="⏳ **Saving schedules...**", view=self)

        mentions = []
        for member in self.targets:
            await query_db(
                "INSERT INTO schedules (guild_id, user_id, username, name, day_of_week, time_24h, end_time_24h, checkin_options, notes, reminder_offset, grace_minutes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,?)",
                (self.gid, str(member.id), member.name, self.name, self.day, self.start_t, self.end_t, self.checkin_opt, self.notes, self.reminder_offset, self.grace_minutes)
            )
            mentions.append(member.mention)

        days_list = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        await interaction.followup.send(
            f"✅ **Schedule Added!**\n"
            f"📅 **Event:** {self.name} every {days_list[self.day]}\n"
            f"⏰ **Time:** {self.start_t} - {self.end_t}\n"
            +(f"⏳ **Grace:** {self.grace_minutes}m\n" if self.grace_minutes else "")
            +f"👥 **Assigned to:** {', '.join(mentions)}",
            ephemeral=False
        )

    async def on_timeout(self):
        for item in self.children: item.disabled = True
        if self.message:
            try: await self.message.edit(content="❌ **Setup Timed Out.**", view=self)
            except: pass

@admin_menu.command(name="add_user_schedule", description="Admin: Add schedule for members/role")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe( start_time="HH:MM (e.g. 09:00)", end_time="HH:MM (e.g. 17:00)", checkin_opt="How members should check in", notes="Extra details (Max 100)",  reminder_offset="(default = 30 min) Minutes late before a warning (Optional)", grace_minutes = "(default = 0) Grace Period before latness starts (Optional)")
@app_commands.autocomplete(start_time=time_suggester,  end_time=time_suggester,  reminder_offset=reminder_suggester)
@app_commands.choices( day=[app_commands.Choice(name=d, value=i) for i, d in enumerate(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])],checkin_opt=[app_commands.Choice(name="Button only", value=0), app_commands.Choice(name="Additional VC", value=1)])
async def admin_add_schedule(interaction: Interaction, name: str,  day: int,  start_time: str,  end_time: str, checkin_opt: int = 0,notes: app_commands.Range[str, 0, 100] = None,reminder_offset: int = 30,grace_minutes: app_commands.Range[int, 0, 59] = 0 ):
    
    def parse_time(t_str):
        t_clean = t_str.replace(":", "").replace(".", "")
        if len(t_clean) == 3: t_clean = "0" + t_clean
        h, m = int(t_clean[:2]), int(t_clean[2:])
        if not (0 <= h < 24 and 0 <= m < 60): raise ValueError
        return f"{h:02d}:{m:02d}"

    try:
        final_start = parse_time(start_time)
        final_end = parse_time(end_time)
    except:
        return await interaction.response.send_message("❌ **Invalid Time Format:** Use HH:MM (e.g. 09:00).", ephemeral=True)

    gid = str(interaction.guild.id)


    view = ScheduleMemberPicker(name=sanitize_text(name),  day=day, start_t=final_start,  end_t=final_end, checkin_opt=checkin_opt, notes=sanitize_text(notes),  reminder_offset=reminder_offset, gid=gid, grace_minutes=grace_minutes )

    await interaction.response.send_message(
        f"🗓️ **Setting up recurring schedule: {name}**\n"
        f"⏰ **Time:** {final_start} - {final_end}\n"
        "Who should receive this schedule?",
        view=view,
        ephemeral=True
    )
    view.message = await interaction.original_response()
class AdminScheduleDeletePicker(discord.ui.View):
    def __init__(self, name: str, gid: str, weekday_filter: Optional[int] = None, date_search: Optional[str] = None):
        super().__init__(timeout=60)
        self.name = name
        self.gid = gid
        self.weekday_filter = weekday_filter 
        self.date_search = date_search       
        self.targets = set()
        self.message = None

    @discord.ui.select(
        cls=discord.ui.MentionableSelect, 
        placeholder="Select members or roles to remove from this schedule...", 
        min_values=1, 
        max_values=25
    )
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.MentionableSelect):
        self.targets = set()
        for entity in select.values:
            if isinstance(entity, discord.Member) and not entity.bot:
                self.targets.add(entity)
            elif isinstance(entity, discord.Role):
                self.targets.update(m for m in entity.members if not m.bot)
        await interaction.response.defer()

    @discord.ui.button(label="Confirm Bulk Deletion", style=discord.ButtonStyle.red)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.targets:
            return await interaction.followup.send("❌ You must select at least one member/role.", ephemeral=True)

        for item in self.children:
            item.disabled = True
        
        await interaction.response.edit_message(content="⏳ **Processing filtered database purge...**", view=self)

        target_ids = [str(member.id) for member in self.targets]
        placeholders = ",".join(["?"] * len(target_ids))

        query_args = [self.name, self.gid, *target_ids]
        filter_conditions = ""

        if self.weekday_filter is not None:
            filter_conditions += " AND day_of_week = ?"
            query_args.append(self.weekday_filter)

        if self.date_search:
            filter_conditions += " AND time_24h LIKE ?"
            query_args.append(f"{self.date_search.strip()}%")

        select_sql = f"SELECT name FROM schedules WHERE name = ? AND guild_id = ? AND user_id IN ({placeholders}){filter_conditions}"
        rows = await query_db(select_sql, tuple(query_args))

        if not rows:
            return await interaction.followup.send(
                f"❌ No matching schedule templates found for the selected targets under these filters.", 
                ephemeral=True
            )

        delete_sql = f"DELETE FROM schedules WHERE name = ? AND guild_id = ? AND user_id IN ({placeholders}){filter_conditions}"
        await query_db(delete_sql, tuple(query_args))

        mentions = [member.mention for member in self.targets]
        await interaction.followup.send(
            f"✅ **Admin Deletion Complete!**\n"
            f"🗑️ **Removed Event:** '{self.name}'\n"
            f"👥 **Stripped From:** {', '.join(mentions)}\n"
            f"📊 **Total Records Dropped:** {len(rows)}",
            ephemeral=False
        )

    async def on_timeout(self):
        for item in self.children: item.disabled = True
        if self.message:
            try: await self.message.edit(content="❌ **Admin Selection Timed Out.**", view=self)
            except: pass

async def schedule_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    try:
        gid = str(interaction.guild_id) if interaction.guild_id else ""
        days_list = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

        rows = await query_db("SELECT name, day_of_week, time_24h, username, user_id FROM schedules WHERE guild_id = ?", (gid,))
        if not rows:
            return []

        choices = []
        seen_combos = set()
        current_lower = current.lower()

        for row in rows:
            if isinstance(row, dict):
                name_val = row.get("name")
                day_idx = row.get("day_of_week")
                time_str = row.get("time_24h")
                uname_val = row.get("username")
                uid_val = row.get("user_id")
            elif hasattr(row, "keys"):
                name_val = row["name"]
                day_idx = row["day_of_week"]
                time_str = row["time_24h"]
                uname_val = row["username"]
                uid_val = row["user_id"]
            else:
                name_val, day_idx, time_str, uname_val, uid_val = row[0], row[1], row[2], row[3], row[4]

            if name_val is None:
                continue

            server_display_name = None
            if interaction.guild and uid_val:
                member = interaction.guild.get_member(int(uid_val))
                if member:
                    server_display_name = member.display_name

            user_tag = server_display_name or uname_val or f"ID: {uid_val}"
            day_name = days_list[int(day_idx)]
            time_display = f" @ {time_str}" if time_str else ""
            
            display_name = f"{name_val} ({day_name}{time_display}) - {user_tag}"[:100]
            packed_value = f"{name_val}|||{day_idx}"[:100]

            search_haystack = f"{name_val} {day_name} {user_tag}".lower()
            if current_lower and current_lower not in search_haystack:
                continue

            search_combo_id = f"{name_val}-{day_idx}-{time_str}-{uid_val}"
            if search_combo_id not in seen_combos:
                seen_combos.add(search_combo_id)
                choices.append(app_commands.Choice(name=display_name, value=packed_value))

            if len(choices) >= 25:
                break
                
        return choices

    except Exception as e:
        print(f"💥 AUTOCOMPLETE CRASH LOG: {e}")
        return []

# ⚙️ THE CLEANED-UP COMMAND
@admin_menu.command(name="delete_user_schedule", description="Admin: Delete schedules via smart search box")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.autocomplete(name=schedule_autocomplete)
async def admin_delete_user_schedule(interaction: Interaction, name: str):
    gid = str(interaction.guild.id)
    day_val = None
    target_name = name

    if "|||" in name:
        parts = name.split("|||")
        target_name = parts[0]
        day_val = int(parts[1])

    picker_view = AdminScheduleDeletePicker(name=target_name, gid=gid, weekday_filter=day_val)
    
    days_list = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    day_string = days_list[day_val] if day_val is not None else "All Days"

    await interaction.response.send_message(f"⚙️ **Admin Control [Filter Locked: {target_name} ({day_string})]**\nSelect target members/roles to clear entries:",view=picker_view, ephemeral=True )
    picker_view.message = await interaction.original_response()


#backup
@admin_menu.command(name="backup", description="Create a manual database backup")
@app_commands.checks.has_permissions(administrator=True)
async def manual_backup(interaction: discord.Interaction):
    if not os.path.exists("backups"):
        os.makedirs("backups")
    
    filename = f"backups/events_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.db"
    shutil.copy2("events.db", filename)
    
    await interaction.response.send_message(f"✅ Backup created: `{filename}`", ephemeral=True)


LEAD_MINUTES = 24*60*7 #7 days

@tasks.loop(seconds=30)
async def auto_check():
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M")
    #today_str = now.strftime("%Y-%m-%d")
    yesterday_str = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")

    #insert for the full week for schedules
    for day_offset in range(8):
        future_date = now + timedelta(days=day_offset)
        future_today_str = future_date.strftime("%Y-%m-%d")
        day_idx = future_date.weekday()

        all_on_day = await query_db("SELECT guild_id, user_id, username, name, end_time_24h, time_24h, grace_minutes FROM schedules WHERE day_of_week = ?", (day_idx,))

        for gid, uid, uname, name, end_t, start_t, grace in all_on_day:
            try:
                start_dt = datetime.strptime(f"{future_today_str} {start_t}", "%Y-%m-%d %H:%M")
            except ValueError:
                continue

            diff_minutes = (start_dt - now).total_seconds() / 60

            if 0 < diff_minutes <= LEAD_MINUTES:
                exists = await query_db("SELECT 1 FROM events WHERE user_id = ? AND name = ? AND time = ?",(uid, name, start_dt.strftime("%Y-%m-%d %H:%M")), one=True)
                if not exists:
                    await query_db("INSERT INTO events (guild_id, user_id, username, name, time, lateness, dm_sent) " "VALUES (?, ?, ?, ?, ?, NULL, 0)",(gid, uid, uname, name, start_dt.strftime("%Y-%m-%d %H:%M"), grace))

    upcoming_events = await query_db("SELECT rowid, user_id, name, time, dm_sent FROM events WHERE lateness IS NULL AND time >= ?",(now_str,) )

    for eid, uid, name, etime, current_milestone in upcoming_events:
        try:
            start_dt = datetime.strptime(etime, "%Y-%m-%d %H:%M")
            hours_until = (start_dt - now).total_seconds() / 3600
            minutes_until = int((start_dt - now).total_seconds() / 60)
            
            target_milestone = None
            time_msg = ""

            # Check intervals using negative tracking values
            if 167 <= hours_until <= 168 and current_milestone > -7:
                target_milestone = -7
                time_msg = "starts in **7 days**"
            elif 23 <= hours_until <= 24 and current_milestone > -1:
                target_milestone = -1
                time_msg = "starts tomorrow (**24 hours**)"
            elif hours_until >= 7 and 119 <= minutes_until <= 120 and current_milestone > -2:
                target_milestone = -2
                time_msg = "starts in **2 hours**"
            elif hours_until >= 3 and 59 <= minutes_until <= 60 and current_milestone > -3:
                target_milestone = -3
                time_msg = "starts in **1 hour**"
            elif 1.5 <= hours_until < 3 and 29 <= minutes_until <= 30 and current_milestone > -4:
                target_milestone = -4
                time_msg = "starts in **30 minutes**"

            if target_milestone is not None:
                user = bot.get_user(int(uid)) or await bot.fetch_user(int(uid))
                if user:
                    sched_data = await query_db(
                        "SELECT end_time_24h FROM schedules WHERE user_id = ? AND name = ?",
                        (str(uid), name), one=True
                    )
                    end_val = sched_data[0] if sched_data else None

                    view = CheckInView(event_id=eid, end_time_str=end_val)
                    embed = discord.Embed(
                        title="⌛ UPCOMING EVENT REMINDER",
                        description=f"Your event **{name}** {time_msg}!\n\nCheck in scheduled for: `{etime}`.",
                        color=0xFFD700
                    )

                    await send_tracked_dm(user, eid, embed=embed, view=view)
                    await query_db("UPDATE events SET dm_sent = ? WHERE rowid = ?", (target_milestone, eid))

        except Exception as e:
            print(f"Error handling early reminder evaluation rules: {e}")


    just_started = await query_db("SELECT rowid, user_id, name, time FROM events WHERE lateness IS NULL AND dm_sent <= 0 AND time <= ?",(now_str,))

    for eid, uid, name, etime in just_started:
        try:
            user = bot.get_user(int(uid)) or await bot.fetch_user(int(uid))
            if user:
                sched_data = await query_db("SELECT end_time_24h FROM schedules WHERE user_id = ? AND name = ?",(str(uid), name), one=True)
                end_val = sched_data[0] if sched_data else None

                view = CheckInView(event_id=eid, end_time_str=end_val)
                embed = discord.Embed(
                    title="⌛ THE CLOCK IS TICKING",
                    description=(
                        f"Your event **{name}** has started!\n\n"
                        f"Check in before **{end_val or 'the deadline'}**."
                    ),
                    color=0xFFD700
                )
                await send_tracked_dm(user, eid, embed=embed, view=view)

        except Exception as e:
            print(f"Error sending start DM for event {eid}: {e}")

        await query_db("UPDATE events SET dm_sent = 1 WHERE rowid = ?", (eid,))

    #reminder dm
    if now.second < 30:
        late_candidates = await query_db( "SELECT rowid, user_id, name, time, reminder_offset, notes, last_reminder_time ""FROM events WHERE lateness IS NULL AND dm_sent = 1" )

        for eid, uid, name, start_str, offset, notes, last_remind in late_candidates:
            try:
                start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M")
                diff_seconds = int((now - start_dt).total_seconds())
                minutes_late = diff_seconds / 60

                if minutes_late >= offset:
                    should_nag = False
                    
                    if not last_remind:
                        should_nag = True 
                    else:
                        # Nag every offset
                        last_remind_dt = datetime.strptime(last_remind, "%Y-%m-%d %H:%M:%S")
                        required_gap_seconds = int(offset)*60
                        if (now - last_remind_dt).total_seconds() >= required_gap_seconds:
                            should_nag = True

                    if should_nag:
                        user = bot.get_user(int(uid)) or await bot.fetch_user(int(uid))
                        if user:
                            sched = await query_db("SELECT end_time_24h FROM schedules WHERE user_id = ? AND name = ?", (str(uid), name), one=True)
                            end_t = sched[0] if sched else None

                            m, s = abs(diff_seconds) // 60, abs(diff_seconds) % 60
                            lateness_str = f"**{m}m {s}s**"
                            view = CheckInView(event_id=eid, end_time_str=end_t)
                            
                            embed = discord.Embed(title="⚠️ Event Reminder", color=discord.Color.red())
                            embed.description = f"You are currently {lateness_str} late for **{name}** (Scheduled: {start_str})"
                            if notes:
                                embed.add_field(name="📝 Note", value=notes)
                            
                            dm_msg = await user.send(embed=embed, view=view)
                            row = await query_db("SELECT last_dm_message_id FROM events WHERE rowid = ?", (eid,), one=True)

                            if row and row[0]:
                                new_ids = f"{row[0]},{dm_msg.id}"
                            else:
                                new_ids = str(dm_msg.id)

                            await query_db("UPDATE events SET last_dm_message_id = ? WHERE rowid = ?", (new_ids, eid))

                        await query_db(
                            "UPDATE events SET last_reminder_time = ? WHERE rowid = ?", 
                            (now.strftime("%Y-%m-%d %H:%M:%S"), eid)
                        )
                        
            except Exception as e:
                print(f"Error in late reminder loop: {e}")
        open_events = await query_db( "SELECT rowid, user_id, name, time FROM events " "WHERE lateness IS NULL AND dm_sent = 1 AND time >= ?", (yesterday_str,) )

        for eid, uid, name, start_str in open_events:
            sched = await query_db(
                "SELECT end_time_24h FROM schedules WHERE user_id = ? AND name = ?",
                (str(uid), name), one=True
            )
            if not sched or not sched[0]:
                continue

            try:
                start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M")
                end_dt = datetime.strptime(f"{start_dt.strftime('%Y-%m-%d')} {sched[0]}", "%Y-%m-%d %H:%M")
            except ValueError:
                continue

            if now < end_dt:
                continue 

            max_lateness = int((end_dt - start_dt).total_seconds())
            
            # Close the event in DB
            await query_db("UPDATE events SET lateness = ? WHERE rowid = ?", (max_lateness, eid))

            try:
                user = bot.get_user(int(uid)) or await bot.fetch_user(int(uid))
                if user:
                    m = max_lateness // 60
                    dm_msg = await user.send(
                        f"❌ **{name}** has ended. You never checked in.\n"
                        f"Logged as **{m}m late** (full duration)."
                    )
            except:
                pass

#vc
@bot.event
async def on_voice_state_update(member, before, after):
    if before.channel is not None and after.channel is None:
        return
    if before.channel is None and after.channel is not None:
        gid, uid = str(member.guild.id), str(member.id)
        old_prompts = await query_db(
            "SELECT last_dm_message_id FROM events WHERE user_id = ? AND guild_id = ? AND lateness IS NULL AND last_dm_message_id IS NOT NULL",
            (uid, gid)
        )
        
        if old_prompts:
            dm_channel = await member.create_dm()

            for row in old_prompts:
                old_msg_id = row.get("last_dm_message_id") if isinstance(row, dict) else row[0]
                if old_msg_id:
                    for mid in old_msg_id.split(","):
                        try:
                            old_msg = await dm_channel.fetch_message(int(old_msg_id))
                            await old_msg.delete()
                            await asyncio.sleep(0.1)
                        except (discord.NotFound, discord.HTTPException):
                            pass # Already deleted

        active = await query_db("SELECT name, time, rowid, last_dm_message_id, grace_minutes FROM events ""WHERE user_id = ? AND guild_id = ? AND lateness IS NULL AND checkin_options = ? ""ORDER BY time ASC LIMIT 1",(uid, gid, 1))
        
        if not active:
            return

        if isinstance(active[0], dict):
            name = active[0].get("name")
            timestamp = active[0].get("time")
            rid = active[0].get("rowid")
            msg_id = active[0].get("last_dm_message_id")
            grace_minutes = active[0].get("grace_minutes")
        else:
            name, timestamp, rid, msg_id, grace_minutes = active[0][0], active[0][1], active[0][2], active[0][3], active[0][4]
            
        try:
            date_format = "%Y-%m-%d %H:%M" if len(timestamp) > 5 else "%H:%M"
            now = datetime.now()
            event_dt = datetime.strptime(timestamp, date_format)
            
            if date_format == "%H:%M":
                event_dt = event_dt.replace(year=now.year, month=now.month, day=now.day)
            
            diff = int((now - event_dt).total_seconds())
            grace_minutes = grace_minutes or 0
            if 0 <= diff <= (grace_minutes * 60):
                diff = 0
            elif diff> (grace_minutes *60):
                diff = diff - grace_minutes*60

            if diff < -7200 or diff > 21600:
                return

            await query_db("UPDATE events SET lateness = ? WHERE rowid = ?", (diff, rid))
            
            m, s = abs(diff) // 60, abs(diff) % 60
            time_formatted = f"{m}m {s}s"
            
            if diff < 0:
                metrics_str = f"Marked as **{time_formatted} early**."
                dm_text = f"⚡ **Voice Check-in Successful!**\n└ You arrived **{time_formatted} early** for '**{name}**'."
            else:
                metrics_str = f"Marked as **{time_formatted} late**."
                dm_text = f"⚠️ **Voice Check-in Successful!**\n└ You checked in **{time_formatted} late** for '**{name}**'."

            if msg_id:
                chan = await get_log_channel(member.guild)
                if chan:
                    try:
                        target_msg = await chan.fetch_message(int(msg_id))

                        updated_content = (
                            f"{target_msg.content}\n"
                            f"-----------------------------\n"
                            f"✅ **Status:** Checked in successfully!\n"
                            f"⏱️ **Metrics:** {metrics_str}"
                        )

                        await target_msg.edit(content=updated_content, view=None)
                        
                    except discord.NotFound:
                        print("Active prompt message was already cleared manually.")
                    except Exception as edit_err:
                        print(f"Failed to edit check-in prompt frame: {edit_err}")

            try:
                await member.send(dm_text)
                chan = await get_log_channel(member.guild)
                if chan:
                    await chan.send(f"**{member.display_name}** checked in via voice! ({m}m {s}s {'early' if diff < 0 else 'late'} for '**{name}**')")
            except discord.Forbidden:
                print(f"Could not send DM update to {member.display_name} (DMs Locked).")
            
        except Exception as e:
            print(f"Error in on_voice_state_update: {e}")
@bot.event
async def on_ready():
    await init_db()
    bot.tree.add_command(event_menu)
    bot.tree.add_command(admin_menu)


    await bot.tree.sync()
    bot.add_view(CheckInView())
    print("✅ Check in buttons are restored successfully!")
    try:
        await asyncio.to_thread(ai_pipeline.train)
    except Exception as e:
        print(f"AI training skipped, failed: {e}")
        pass

    if not auto_check.is_running():
        auto_check.start()

    #send new dm after restart    
    now = datetime.now()
    yesterday_str = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")

    active = await query_db(
        "SELECT rowid, user_id, name, time FROM events "
        "WHERE lateness IS NULL AND dm_sent = 1 AND time >= ?",
        (yesterday_str,)
    )

    for eid, uid, name, start_str in active:
        sched = await query_db(
            "SELECT end_time_24h FROM schedules WHERE user_id = ? AND name = ?",
            (str(uid), name), one=True
        )
        end_val = sched[0] if sched else None

        if end_val:
            try:
                start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M")
                end_dt   = datetime.strptime(
                    f"{start_dt.strftime('%Y-%m-%d')} {end_val}", "%Y-%m-%d %H:%M"
                )
                if now > end_dt:
                    continue 
            except ValueError:
                pass
    print(f"Logged in as {bot.user}")

# execution

ai_pipeline = LatenessPipeline(use_mock=False)
bot.run(TOKEN)