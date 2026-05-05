import discord
import os
import asyncio
import aiosqlite
import sqlite3
import json
from discord.ext import commands, tasks
from discord import app_commands, Interaction
from datetime import datetime, timedelta
from dotenv import load_dotenv
from lateness_model import LatenessPipeline, setup_tables
from discord import ButtonStyle, ui
import numpy as np

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
    await db.execute("""CREATE TABLE IF NOT EXISTS events
                         (guild_id TEXT, user_id TEXT, username TEXT,
                          name TEXT, time TEXT, lateness INTEGER, started INTEGER)""")
    await db.execute("""CREATE TABLE IF NOT EXISTS schedules
                         (guild_id TEXT, user_id TEXT, username TEXT,
                          name TEXT, day_of_week INTEGER, time_24h TEXT)""")
    
    # Check for missing columns (Migrations)
    cursor = await db.execute("PRAGMA table_info(events)")
    cols = [row[1] for row in await cursor.fetchall()]
    if "guild_id" not in cols:
        await db.execute("ALTER TABLE events ADD COLUMN guild_id TEXT")
    
    cursor = await db.execute("PRAGMA table_info(schedules)")
    cols = [row[1] for row in await cursor.fetchall()]
    if "guild_id" not in cols:
        await db.execute("ALTER TABLE schedules ADD COLUMN guild_id TEXT")

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

# autocomplete/options

async def event_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    guild_id = str(interaction.guild.id)
    uid = str(interaction.user.id)
    cmd_name = interaction.command.name

    is_admin_view = interaction.command.parent and interaction.command.parent.name == "admin"

    if "schedule" in cmd_name:
        if is_admin_view:
            query = "SELECT rowid, name, day_of_week, time_24h, username FROM schedules WHERE guild_id = ? AND name LIKE ? LIMIT 25"
            params = (guild_id, f"%{current}%")
        else:
            query = "SELECT rowid, name, day_of_week, time_24h, username FROM schedules WHERE guild_id = ? AND user_id = ? AND name LIKE ? LIMIT 25"
            params = (guild_id, uid, f"%{current}%")

        rows = await query_db(query, params)
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        
        choices = []
        for r in rows:
            label = f"{r[4]}: {r[1]} ({days[r[2]]} @ {r[3]})" if is_admin_view else f"{r[1]} ({days[r[2]]} @ {r[3]})"
            choices.append(app_commands.Choice(name=label[:100], value=str(r[0])))
        return choices
    elif cmd_name == "predict":

        rows = await query_db(
            "SELECT rowid, name, time, username FROM events "
            "WHERE guild_id = ? AND lateness IS NULL AND name LIKE ? "
            "ORDER BY time ASC LIMIT 25",
            (guild_id, f"%{current}%")
        )
        return [
            app_commands.Choice(name=f"{r[3]}: {r[1]} [{r[2]}]"[:100], value=str(r[0]))
            for r in rows
        ]
    else:
        filter_sql = ""
        if cmd_name == "stop":
            filter_sql = " AND lateness IS NULL"
            
        sort_order = "ASC" if cmd_name == "stop" else "DESC"

        if is_admin_view:
            query = f"SELECT rowid, name, time, username FROM events WHERE guild_id = ?{filter_sql} AND name LIKE ? ORDER BY time {sort_order} LIMIT 25"
            params = (guild_id, f"%{current}%")
        else:
            query = f"SELECT rowid, name, time, username FROM events WHERE guild_id = ? AND user_id = ?{filter_sql} AND name LIKE ? ORDER BY time {sort_order} LIMIT 25"
            params = (guild_id, uid, f"%{current}%")

        rows = await query_db(query, params)
        
        choices = []
        for r in rows:
            label = f"{r[3]}: {r[1]} [{r[2]}]" if is_admin_view else f"{r[1]} [{r[2]}]"
            choices.append(app_commands.Choice(name=label[:100], value=str(r[0])))
        return choices


# stop logic

async def execute_stop_logic(interaction, event_id_str, members_list, role):
    name_lookup = await query_db(
        "SELECT name FROM events WHERE rowid = ?", (int(event_id_str),), one=True
    )
    if not name_lookup:
        return await interaction.response.send_message("❌ Event not found.", ephemeral=True)

    actual_event_name = name_lookup[0]
    targets = {m for m in members_list if m}
    if role:
        targets.update(m for m in role.members if not m.bot)
    if not targets:
        targets.add(interaction.user)

    now           = datetime.now()
    guild_id      = str(interaction.guild.id)
    success_count = 0
    last_diff     = 0

    for member in targets:
        uid = str(member.id)
        row = await query_db(
            "SELECT rowid, time FROM events "
            "WHERE user_id = ? AND guild_id = ? AND name = ? AND lateness IS NULL "
            "ORDER BY rowid DESC LIMIT 1",
            (uid, guild_id, actual_event_name), one=True
        )
        if row:
            rid, time_str = row[0], row[1]
            try:
                fmt       = "%Y-%m-%d %H:%M" if len(time_str) > 5 else "%H:%M"
                target_dt = datetime.strptime(time_str, fmt)
                if fmt == "%H:%M":
                    target_dt = target_dt.replace(year=now.year, month=now.month, day=now.day)
                diff      = int((now - target_dt).total_seconds())
                last_diff = diff
                await query_db(
                    "UPDATE events SET lateness = ?, started = 1 WHERE rowid = ?", (diff, rid)
                )
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

#for delete
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

# events

@event_menu.command(name="create", description="Manual: Set a specific date/time for members")
async def create(interaction: Interaction,
                 name: str, year: int, month: int, day: int, time_24h: str,
                 member1: discord.Member = None, member2: discord.Member = None,
                 member3: discord.Member = None, member4: discord.Member = None,
                 member5: discord.Member = None, role: discord.Role = None):

    targets = {m for m in [member1, member2, member3, member4, member5] if m}
    if role:
        targets.update(m for m in role.members if not m.bot)
    if not targets:
        targets.add(interaction.user)

    try:
        dt_str   = f"{year}-{month:02d}-{day:02d} {time_24h}"
        guild_id = str(interaction.guild.id)
        names_list = []
        for member in targets:
            await query_db("INSERT INTO events (guild_id, user_id, username, name, time, lateness, started) " "VALUES (?, ?, ?, ?, ?, NULL, 0)", (guild_id, str(member.id), member.name, name, dt_str))
            names_list.append(member.mention)
        members_name_str = " ".join(names_list)
        unit = "member" if len(targets) == 1 else "members"
        await interaction.response.send_message(
            f"📅 Scheduled **{name}** on {dt_str} for {len(targets)} {unit}:\n {members_name_str}"
        )
    except Exception:
        await interaction.response.send_message(
            "❌ Format error. Ensure time is HH:MM (24h).", ephemeral=True
        )

@event_menu.command(name="create_quick", description="Create a quick event N minutes from now")
async def quick(interaction: Interaction,
                name: str, minutes: int,
                member1: discord.Member = None, member2: discord.Member = None,
                member3: discord.Member = None, member4: discord.Member = None,
                member5: discord.Member = None, role: discord.Role = None):

    targets = {m for m in [member1, member2, member3, member4, member5] if m}
    if role:
        targets.update(m for m in role.members if not m.bot)
    if not targets:
        targets.add(interaction.user)

    dt_str   = (datetime.now() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M")
    names_list = []
    guild_id = str(interaction.guild.id)
    for member in targets:
        await query_db(
            "INSERT INTO events (guild_id, user_id, username, name, time, lateness, started) "
            "VALUES (?, ?, ?, ?, ?, NULL, 0)",
            (guild_id, str(member.id), member.name, name, dt_str)
        )
        names_list.append(member.mention)
    members_name_str = " ".join(names_list)
    unit = "member" if len(targets) == 1 else "members"
    # Calculate clean hour/minute values
    total_hours = minutes // 60
    remaining_mins = minutes % 60
    
    duration_str = ""
    if total_hours > 0:
        duration_str += f"{total_hours}h "
    duration_str += f"{remaining_mins}m"

    await interaction.response.send_message(f"✅ Quick event '**{name}**' set for **{dt_str}** "f"({duration_str} from now) for {len(targets)} {unit}:\n"f"{members_name_str}")


@event_menu.command(name="list", description="List your events")
async def list_events(interaction: Interaction, member: discord.Member = None):
    target = member or interaction.user
    rows   = await query_db(
        "SELECT name, time, lateness, started FROM events WHERE user_id = ? AND guild_id = ?",
        (str(target.id), str(interaction.guild.id))
    )
    if not rows:
        return await interaction.response.send_message(
            f"📅 No events found for {target.display_name}", ephemeral=True
        )
    msg = f"📅 **{target.display_name}'s Events:**\n"
    for i, (name, timestamp, late, started) in enumerate(rows, 1):
        if late is not None:
            m, s = abs(late) // 60, abs(late) % 60
            status = f"{timestamp} ✅ Early: {m}m {s}s" if late < 0 else (f"{timestamp} ⚠️ Late: {m}m {s}s" if late > 0 else "⏱️ On Time")
        else:
            status = f"{timestamp} ⏳ Ongoing" if started else f"🕒 {timestamp}"
        msg += f"{i}. **{name}** — {status}\n"
    await interaction.response.send_message(msg, ephemeral=True)

@event_menu.command(name="list_all", description="View everyone's events in this server")
async def list_all(interaction: Interaction):
    rows = await query_db("SELECT user_id, username, name, time, lateness, started FROM events WHERE guild_id = ? ORDER BY user_id ASC", (str(interaction.guild.id),))  
    if not rows: 
        return await interaction.response.send_message("📅 No events found in this server.", ephemeral=True)
    
    msg = f" **{interaction.guild.name} Event Board**\n"
    curr = None
    
    for uid, uname, name, timestamp, late, started in rows:
        if uid != curr:
            curr = uid
            msg += f"\n👤 **{uname or f'<@{uid}>'}**\n"
        
        if late is not None:
            abs_late = abs(late)
            m, s = abs_late // 60, abs_late % 60
            time_str = f"{m}m {s}s"
           
            if late < 0:
                emoji = "✅ Early:"
            elif late == 0:
                emoji = " On Time:"
            else:
                emoji = "✅ Late:"        
            status = f"{timestamp} {emoji} {time_str}"
        else:
            status = f"{timestamp} ⏳ Ongoing" if started else f"🕒 {timestamp}"
            
        msg += f" └ **{name}** — {status}\n"  
    await interaction.response.send_message(msg)

@event_menu.command(name="stop", description="Stop an active event")
@app_commands.autocomplete(event_name=event_autocomplete)
async def stop(interaction: discord.Interaction, event_name: str,
               member1: discord.Member = None, member2: discord.Member = None,
               member3: discord.Member = None, member4: discord.Member = None,
               member5: discord.Member = None, role: discord.Role = None):
    has_targets = any([member1, member2, member3, member4, member5, role])
    if has_targets and not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ No permission.", ephemeral=True)
    await execute_stop_logic(interaction, event_name, [member1, member2, member3, member4, member5], role)



@event_menu.command(name="delete", description="Delete one of your event records")
@app_commands.autocomplete(event_name=event_autocomplete)
async def delete_event(interaction: discord.Interaction, event_name: str):
    if not event_name.isdigit():
        return await interaction.response.send_message("❌ Invalid selection.", ephemeral=True)
    
    uid = str(interaction.user.id)
    row = await query_db("SELECT name, time FROM events WHERE rowid = ? AND user_id = ?", (int(event_name), uid), one=True) 
    if not row:
        return await interaction.response.send_message("❌ Record not found.", ephemeral=True)

    event_label = f"**{row[0]}** ({row[1]})"
    view = DeleteConfirm()
    await interaction.response.send_message(f"⚠️ Are you sure you want to delete the record for {event_label}?",view=view,ephemeral=True)
    await view.wait()
    if view.value is None:
        await interaction.edit_original_response(content=" Request timed out.", view=None)
    elif view.value:
        await query_db("DELETE FROM events WHERE rowid = ?", (int(event_name),))
        await interaction.edit_original_response(content=f" Deleted {event_label}.", view=None)
    else:
        await interaction.edit_original_response(content="❌ Deletion cancelled.", view=None)

@event_menu.command(name="clear_all", description="Clear all your events in this server")
async def clear(interaction: Interaction):
    view = ClearConfirm()
    
    await interaction.response.send_message("⚠️ **Are you sure?** This will permanently delete all your event history in this server.",view=view,ephemeral=True)
    await view.wait()

    if view.value is None:
        await interaction.edit_original_response(content=" Request timed out.", view=None)
    elif view.value:
        await query_db(
            "DELETE FROM events WHERE user_id = ? AND guild_id = ?", 
            (str(interaction.user.id), str(interaction.guild.id))
        )
        await interaction.edit_original_response(content=" All your events in this server have been cleared.", view=None)
    else:
        await interaction.edit_original_response(content="❌ Clear cancelled.", view=None)


@event_menu.command(name="add_schedule", description="Set a recurring weekly event")
async def add_schedule(interaction: Interaction, name: str, day: str, time_24h: str):
    day_map = {
        "mon": 0, "monday": 0,
        "tue": 1, "tuesday": 1,
        "wed": 2, "wednesday": 2,
        "thu": 3, "thursday": 3,
        "fri": 4, "friday": 4,
        "sat": 5, "saturday": 5,
        "sun": 6, "sunday": 6
    }
    day_clean = day.lower().strip()
    day_index = day_map.get(day_clean)
    if day_index is None:
        day_index = day_map.get(day_clean[:3])

    if day_index is None:
        return await interaction.response.send_message("❌ Invalid day. Please use 'Monday', 'Mon', etc.", ephemeral=True )
    try:
        datetime.strptime(time_24h, "%H:%M")
    except ValueError:
        return await interaction.response.send_message("❌ Invalid time format. Use HH:MM (e.g., 14:30 or 09:00).", ephemeral=True )

    await query_db("INSERT INTO schedules (guild_id, user_id, username, name, day_of_week, time_24h) VALUES (?, ?, ?, ?, ?, ?)",
                   (str(interaction.guild.id), str(interaction.user.id), interaction.user.display_name, name, day_index, time_24h)
    )
    
    days_list = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    await interaction.response.send_message(f"🗓️ Recurring event **{name}** set for every **{days_list[day_index]}** at **{time_24h}**.")

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
    
    # 1. Fetch the event record
    row = await query_db(
        "SELECT name, time, user_id, username FROM events WHERE rowid = ? AND guild_id = ?", 
        (event_name, gid),
        one=True
    )
    
    if not row:
        return await interaction.response.send_message(
            "❌ Could not find that specific ongoing event.", ephemeral=True
        )

    actual_name, event_time, event_owner_id, event_owner_name = row
    
    # Target is the tagged member, or the owner of the event record
    target_id = str(member.id) if member else event_owner_id
    target_name = member.display_name if member else event_owner_name

    # 2. Run the AI Pipeline
    # pred_res: The main prediction
    # lower_res: The "earliest" likely arrival
    # upper_res: The "latest" likely arrival
    pred_res, lower_res, upper_res = ai_pipeline.predict_with_confidence(
        user_id=target_id, 
        event_name=actual_name, 
        event_time=event_time
    )

    if pred_res is None:
        return await interaction.response.send_message(
            f"❌ Not enough historical data to predict for **{target_name}**.", 
            ephemeral=True
        )

    # 3. Formatting Helper
    def fmt(mins):
        val = float(mins[0]) if isinstance(mins, (list, np.ndarray)) else float(mins)
        total_seconds = abs(int(val * 60))
        m, s = divmod(total_seconds, 60)
        status = "Early" if val < 0 else "Late"
        return f"{m}m {s}s {status}"

    # 4. Final Response with Range
    prediction_text = (
        f"🔮 **AI Prediction** for '**{actual_name}**'\n"
        f"👤 Target: **{target_name}**\n"
        f"⏱️ **Expected**: {fmt(pred_res)}\n"
        f"📉 **Range**: {fmt(lower_res)} — {fmt(upper_res)}"
    )

    await interaction.response.send_message(prediction_text)

#admin stuff
@admin_menu.command(name="delete", description="Admin: Delete event records for members/role")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.autocomplete(event_name=event_autocomplete)
async def admin_delete(interaction: discord.Interaction, event_name: str,
               member1: discord.Member = None, member2: discord.Member = None,
               member3: discord.Member = None, member4: discord.Member = None,
               member5: discord.Member = None, role: discord.Role = None):
    
    actual_name = event_name
    if event_name.isdigit():
        res = await query_db("SELECT name FROM events WHERE rowid = ?", (int(event_name),), one=True)
        if res: actual_name = res[0]

    targets = {m for m in [member1] if m}
    if role:
        targets.update(m for m in role.members if not m.bot)
    target_desc = f"**{len(targets)} members**" if targets else f"**Record ID #{event_name}**"
    
    view = DeleteConfirm() 
    await interaction.response.send_message( f" **ADMIN ACTION**: Are you sure you want to delete entries of '**{actual_name}**' for {target_desc}?",view=view,ephemeral=True)

    await view.wait()
    if not view.value:
        return await interaction.edit_original_response(content="❌ Admin delete cancelled.", view=None)

    if not targets and event_name.isdigit():
        await query_db("DELETE FROM events WHERE rowid = ?", (int(event_name),))
        return await interaction.edit_original_response(content=f"✅ Deleted specific record ID #{event_name}. ", view=None)

    guild_id = str(interaction.guild.id)
    deleted_count = 0
    for member in targets:
        await query_db("DELETE FROM events WHERE user_id = ? AND guild_id = ? AND name = ?", 
                       (str(member.id), guild_id, actual_name))
        deleted_count += 1

    await interaction.edit_original_response(content=f" [ADMIN] Deleted all entries of '**{actual_name}**' for **{deleted_count}** members.",view=None )

@admin_menu.command(name="clear", description="Admin: Clear ALL user data for members/role")
@app_commands.checks.has_permissions(manage_guild=True)
async def admin_clear(interaction: discord.Interaction, event_name: str,
               member1: discord.Member = None, member2: discord.Member = None,
               member3: discord.Member = None, member4: discord.Member = None,
               member5: discord.Member = None, role: discord.Role = None):
    
    target_members = {m for m in [member1, member2, member3, member4, member5] if m}
    if role:
        target_members.update(m for m in role.members if not m.bot)

    if not target_members:
        return await interaction.response.send_message("❌ Specify who to clear! (Tag someone or a role)", ephemeral=True)

    view = DeleteConfirm()
    count = len(target_members)
    await interaction.response.send_message(
        f"❗ **DANGER**: You are about to wipe the **ENTIRE HISTORY** for **{count}** members. This cannot be undone. Proceed?",
        view=view,
        ephemeral=True
    )

    await view.wait()
    if not view.value:
        return await interaction.edit_original_response(content="❌ Admin wipe cancelled.", view=None)

    guild_id = str(interaction.guild.id)
    for member in target_members:
        await query_db("DELETE FROM events WHERE user_id = ? AND guild_id = ?", (str(member.id), guild_id))
        await query_db("DELETE FROM schedules WHERE user_id = ? AND guild_id = ?", (str(member.id), guild_id))

    unit = "member" if count == 1 else "members"
    await interaction.edit_original_response(content=f"[ADMIN] Full history and schedules wiped for **{count}** {unit}!", view=None)

@admin_menu.command(name="stop", description="Admin: Stop an event for anyone")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.autocomplete(event_name=event_autocomplete)
async def admin_stop(interaction: discord.Interaction, event_name: str,
               member1: discord.Member = None, member2: discord.Member = None,
               member3: discord.Member = None, member4: discord.Member = None,
               member5: discord.Member = None, role: discord.Role = None):
    
    await execute_stop_logic(interaction, event_name, [member1, member2, member3, member4, member5], role)

@admin_menu.command(name="add_record", description="Admin: Add a finished event record for members/role")
@app_commands.checks.has_permissions(manage_guild=True)
async def admin_add_record(interaction: discord.Interaction, event_name: str, lateness_minutes: int, date_str: str = None,
                           member1: discord.Member = None, member2: discord.Member = None,
                            member3: discord.Member = None, member4: discord.Member = None,
                            member5: discord.Member = None, role: discord.Role = None):
    
    targets = {m for m in [member1, member2, member3, member4, member5] if m}
    if role:
        targets.update(m for m in role.members if not m.bot)
        
    if not targets:
        return await interaction.response.send_message("❌ You must specify at least one member or a role.", ephemeral=True)

    if not date_str: 
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    guild_id = str(interaction.guild.id)
    lateness_seconds = lateness_minutes * 60
    mentions_list = []

    for member in targets:
        await query_db( "INSERT INTO events (guild_id, user_id, username, name, time, lateness, started) VALUES (?, ?, ?, ?, ?, ?, 0)",  (guild_id, str(member.id), member.name, event_name, date_str, lateness_seconds))
        mentions_list.append(member.mention)

    unit = "member" if len(targets) == 1 else "members"
    mentions_str = ", ".join(mentions_list)
    
    await interaction.response.send_message( f"✅ Added record for **{len(targets)}** {unit} under event '**{event_name}**' ({lateness_minutes}m late).\n" f"**Targets:** {mentions_str}")

@admin_menu.command(name="add_user_schedule", description="Admin: Add schedule for members/role")
@app_commands.checks.has_permissions(manage_guild=True)
async def admin_add_schedule(interaction: Interaction, 
                             name: str, day: str, time_24h: str,
                             member1: discord.Member = None, member2: discord.Member = None,
                             member3: discord.Member = None, member4: discord.Member = None,
                             member5: discord.Member = None, role: discord.Role = None):
    
    day_map = {
        "monday": 0, "mon": 0,
        "tuesday": 1, "tue": 1, "tues": 1,
        "wednesday": 2, "wed": 2,
        "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
        "friday": 4, "fri": 4,
        "saturday": 5, "sat": 5,
        "sunday": 6, "sun": 6
    }
    day_input = day.lower().strip()
    if day_input not in day_map: 
        return await interaction.response.send_message("❌ Invalid day.", ephemeral=True)
    
    day_index = day_map.index(day_input)
    gid = str(interaction.guild.id)

    targets = {m for m in [member1, member2, member3, member4, member5] if m}
    if role:
        targets.update(m for m in role.members if not m.bot)
        
    if not targets:
        return await interaction.response.send_message("❌ Specify who to add the schedule for!", ephemeral=True)

    mentions = []
    for member in targets:
        await query_db(
            "INSERT INTO schedules (guild_id, user_id, username, name, day_of_week, time_24h) VALUES (?, ?, ?, ?, ?, ?)", 
            (gid, str(member.id), member.name, name, day_index, time_24h)
        )
        mentions.append(member.mention)

    await interaction.response.send_message(
        f"🗓️ Admin set schedule '**{name}**' ({day.capitalize()} @ {time_24h}) for:\n{', '.join(mentions)}"
    )

@admin_menu.command(name="delete_user_schedule", description="Admin: Delete schedules for members/role")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.autocomplete(name=event_autocomplete)
async def admin_delete_user_schedule(interaction: Interaction, 
                                     name: str, 
                                     member1: discord.Member = None, member2: discord.Member = None,
                                     member3: discord.Member = None, member4: discord.Member = None,
                                     member5: discord.Member = None, role: discord.Role = None):
    gid = str(interaction.guild.id)

    if name.isdigit():
        row = await query_db("SELECT name, username, day_of_week, time_24h FROM schedules WHERE rowid = ? AND guild_id = ?", (int(name), gid), one=True)
        if not row:
            return await interaction.response.send_message("❌ Schedule not found.", ephemeral=True)

        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        info = f"**{row[0]}** for **{row[1]}** ({days[row[2]]} @ {row[3]})"
        
        view = DeleteConfirm()
        await interaction.response.send_message(f" **ADMIN**: Delete this schedule?\n> {info}", view=view, ephemeral=True)
        await view.wait()
        
        if view.value:
            await query_db("DELETE FROM schedules WHERE rowid = ?", (int(name),))
            await interaction.edit_original_response(content=f"✅ Deleted: {info}", view=None)
        return
    
    targets = {m for m in [member1, member2, member3, member4, member5] if m}
    if role:
        targets.update(m for m in role.members if not m.bot)
    
    if not targets:
        return await interaction.response.send_message("❌ Select a schedule from the list OR specify members/role.", ephemeral=True)

    target_ids = [str(m.id) for m in targets]
    rows = await query_db(f"SELECT name FROM schedules WHERE name = ? AND guild_id = ? AND user_id IN ({','.join(['?']*len(target_ids))})", (name, gid, *target_ids))

    if not rows:
        return await interaction.response.send_message(f"❌ No schedules named '{name}' found for those targets.", ephemeral=True)

    view = DeleteConfirm()
    await interaction.response.send_message(f" **ADMIN**: Delete **{len(rows)}** schedules named '**{rows[0][0]}**' for the selected group?", view=view, ephemeral=True)

    await view.wait()
    if view.value:
        for uid in target_ids:
            await query_db("DELETE FROM schedules WHERE user_id = ? AND guild_id = ? AND name = ?", (uid, gid, name))
        await interaction.edit_original_response(content=f"✅ Admin deleted {len(rows)} records.", view=None)

#import & export
@admin_menu.command(name="export", description="Export server data to JSON")
@app_commands.checks.has_permissions(manage_guild=True)
async def admin_export(interaction: Interaction):
    rows = await query_db("SELECT * FROM events WHERE guild_id = ?", (str(interaction.guild.id),))
    data = [{"gid": r[0], "uid": r[1], "user": r[2], "name": r[3], "time": r[4], "late": r[5], "start": r[6]} for r in rows]
    with open(f"export_{interaction.guild.id}.json", "w") as f: json.dump(data, f, indent=4)
    await interaction.response.send_message("✅ Exported!", file=discord.File(f"export_{interaction.guild.id}.json"), ephemeral=True)

@admin_menu.command(name="import", description="Import from JSON string")
@app_commands.checks.has_permissions(manage_guild=True)
async def admin_import(interaction: Interaction, json_data: str):
    try:
        data = json.loads(json_data)
        for e in data:
            await query_db("INSERT INTO events (guild_id, user_id, username, name, time, lateness, started) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                     (e.get('gid', str(interaction.guild.id)), e['uid'], e.get('user', 'Unknown'), e['name'], e['time'], e['late'], e['start']))
        await interaction.response.send_message("✅ Imported successfully!", ephemeral=True)
    except Exception as ex: await interaction.response.send_message(f"❌ Error: {ex}", ephemeral=True)



# auto start
@tasks.loop(seconds=30)
async def auto_check():
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M")
    day_idx = now.weekday()
    time_str = now.strftime("%H:%M")
    recurring = await query_db(
        "SELECT guild_id, user_id, username, name FROM schedules WHERE day_of_week = ? AND time_24h = ?", 
        (day_idx, time_str)
    )
    for gid, uid, uname, name in recurring:
        exists = await query_db("SELECT 1 FROM events WHERE user_id = ? AND name = ? AND time = ?", (uid, name, now_str), one=True)
        if not exists:
            await query_db(
                "INSERT INTO events (guild_id, user_id, username, name, time, lateness, started) VALUES (?, ?, ?, ?, ?, NULL, 1)", 
                (gid, uid, uname, name, now_str)
            )
    to_notify = await query_db(
        "SELECT user_id, name, guild_id FROM events WHERE time <= ? AND started = 0 AND lateness IS NULL", 
        (now_str,)
    )
    for uid, name, gid in to_notify:
        user = bot.get_user(int(uid))
        if not user:
            try:
                user = await bot.fetch_user(int(uid))
            except:
                continue

        if user:
            embed = discord.Embed( title="⌛ THE CLOCK IS TICKING",description=(f"Your event **{name}** has officially started!\n\n""### Action Required\n""Join the voice channel now to stop the lateness timer."),color=0xFFD700) # gold/yellow
            try:
                await user.send(embed=embed)
            except discord.Forbidden:
                print(f"Could not DM {uid}")


    await query_db("UPDATE events SET started = 1 WHERE time <= ? AND started = 0 AND lateness IS NULL", (now_str,))



#vc
@bot.event
async def on_voice_state_update(member, before, after):

    if before.channel is None and after.channel is not None:
        gid, uid = str(member.guild.id), str(member.id)

        active = await query_db( "SELECT name, time, rowid FROM events ""WHERE user_id = ? AND guild_id = ? AND lateness IS NULL " "ORDER BY time ASC LIMIT 1",  (uid, gid) )
        
        if not active:
            return

        name, timestamp, rid = active[0]
        try:
            date_format = "%Y-%m-%d %H:%M" if len(timestamp) > 5 else "%H:%M"
            now = datetime.now()
            event_dt = datetime.strptime(timestamp, date_format)
            
            if date_format == "%H:%M":
                event_dt = event_dt.replace(year=now.year, month=now.month, day=now.day)
            
            diff = int((now - event_dt).total_seconds())

            # diff -7200 = 2 hours early
            # diff 21600 = 6 hours late
            if diff < -7200 or diff > 21600:
                return

            await query_db("UPDATE events SET lateness = ? WHERE rowid = ?", (diff, rid) )
            
            chan = discord.utils.get(member.guild.text_channels, name="general")
            if chan:
                m, s = abs(diff) // 60, abs(diff) % 60
                if diff < 0:
                    await chan.send(f"🏃 **{member.mention}** is early! Saved **{m}m {s}s** for '**{name}**'.")
                else:
                    await chan.send(f"✅ **{member.mention}** arrived! Late for '**{name}**': **{m}m {s}s**.")
            
        except Exception as e:
            print(f"Error in on_voice_state_update: {e}")

@bot.event
async def on_ready():
    await init_db()
    bot.tree.add_command(event_menu)
    bot.tree.add_command(admin_menu)
    await bot.tree.sync()
    
    try:
        await asyncio.to_thread(ai_pipeline.train)
    except:
        pass

    if not auto_check.is_running():
        auto_check.start()
    print(f"Logged in as {bot.user}")

# execution

ai_pipeline = LatenessPipeline(use_mock=False)
bot.run(TOKEN)