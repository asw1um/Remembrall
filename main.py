import discord
import os
from discord.ext import commands, tasks
from discord import app_commands, Interaction
import sqlite3
import json 
from datetime import datetime, timedelta
from dotenv import load_dotenv
from lateness_model import LatenessPipeline, setup_tables

# setup
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DB_FILE = "events.db"

def init_db():
    conn = sqlite3.connect(DB_FILE, timeout=15)
    c = conn.cursor()
    try:
        c.execute('PRAGMA journal_mode=WAL;')
        c.execute('''CREATE TABLE IF NOT EXISTS events 
                     (guild_id TEXT, user_id TEXT, username TEXT, name TEXT, time TEXT, lateness INTEGER, started INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS schedules 
                     (guild_id TEXT, user_id TEXT, username TEXT, name TEXT, day_of_week INTEGER, time_24h TEXT)''')
        
        # Migration logic for existing DBs
        try:
            c.execute("ALTER TABLE events ADD COLUMN guild_id TEXT")
        except sqlite3.OperationalError: pass
        try:
            c.execute("ALTER TABLE schedules ADD COLUMN guild_id TEXT")
        except sqlite3.OperationalError: pass
        setup_tables()
        conn.commit()
    finally:
        conn.close()

def query_db(query, args=(), one=False):
    # Added isolation_level=None for better performance in WAL mode
    conn = sqlite3.connect(DB_FILE, timeout=20, isolation_level=None) 
    c = conn.cursor()
    try:
        if not query.strip().upper().startswith("SELECT"):
            c.execute("BEGIN IMMEDIATE") # Forces a write lock immediately to prevent mid-operation deadlocks
            c.execute(query, args)
            c.execute("COMMIT")
            rv = []
        else:
            c.execute(query, args)
            rv = c.fetchall()
    except Exception as e:
        if not query.strip().upper().startswith("SELECT"):
            c.execute("ROLLBACK")
        raise e
    finally:
        conn.close()
    return (rv[0] if rv else None) if one else rv

async def event_autocomplete(interaction: discord.Interaction, current: str):
    query = "SELECT DISTINCT name FROM events WHERE name LIKE ? LIMIT 25"
    rows = query_db(query, (f'%{current}%',))
    
    return [
        app_commands.Choice(name=row[0], value=row[0]) 
        for row in rows
    ]

init_db()

ai_pipeline = LatenessPipeline(use_mock=False)

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True
intents.message_content = True 

bot = commands.Bot(command_prefix="!", intents=intents)

class EventGroup(app_commands.Group, name="event"): pass
class AdminGroup(app_commands.Group, name="admin"): pass

event_menu = EventGroup()
admin_menu = AdminGroup()

# --- USER COMMANDS ---

@event_menu.command(name="create", description="Manual: Set a specific date and time for multiple members")
async def create(interaction: Interaction, 
                 name: str, 
                 year: int, 
                 month: int, 
                 day: int, 
                 time_24h: str, 
                 member1: discord.Member = None, 
                 member2: discord.Member = None,
                 member3: discord.Member = None,
                 member4: discord.Member = None,
                 member5: discord.Member = None,
                 role: discord.Role = None):
    
    target_members = set()

    for m in [member1, member2, member3, member4, member5]:
        if m:
            target_members.add(m)

    if role:
        for m in role.members:
            if not m.bot:
                target_members.add(m)
    
    if not target_members:
        target_members.add(interaction.user)

    try:
        dt_str = f"{year}-{month:02d}-{day:02d} {time_24h}"
        guild_id = str(interaction.guild.id)
        
        for member in target_members:
            query_db(
                "INSERT INTO events (guild_id, user_id, username, name, time, lateness, started) VALUES (?, ?, ?, ?, ?, NULL, 0)", 
                (guild_id, str(member.id), member.display_name, name, dt_str)
            )
            
        member_count = len(target_members)
        unit = "member" if member_count == 1 else "members"
        await interaction.response.send_message(f"📅 Scheduled **{name}** for {member_count} {unit} on {dt_str}")
        
    except Exception as e:
        await interaction.response.send_message("❌ Format error. Ensure time is HH:MM (24h format).", ephemeral=True)

@event_menu.command(name="create_quick", description="Create a quick event for up to 5 members and/or a role")
async def quick(interaction: Interaction, 
                name: str, 
                minutes: int, 
                member1: discord.Member = None, 
                member2: discord.Member = None,
                member3: discord.Member = None,
                member4: discord.Member = None,
                member5: discord.Member = None,
                role: discord.Role = None):
    
    target_members = set()
    
    # 1. Calculate the exact Date and Time
    now = datetime.now()
    future_dt = now + timedelta(minutes=minutes)
    # Formats as "2026-05-01 21:45"
    dt_str = future_dt.strftime("%Y-%m-%d %H:%M")
    
    for m in [member1, member2, member3, member4, member5]:
        if m:
            target_members.add(m)
    
    if role:
        for m in role.members:
            if not m.bot: 
                target_members.add(m)
                
    if not target_members:
        # Default to the user if no one else is specified
        target_members.add(interaction.user)

    guild_id = str(interaction.guild.id)
    for member in target_members:
        query_db(
            "INSERT INTO events (user_id, username, name, time, lateness, started, guild_id) VALUES (?, ?, ?, ?, NULL, 0, ?)",
            (str(member.id), member.display_name, name, dt_str, guild_id)
        )
    
    member_count = len(target_members)
    unit = "member" if member_count == 1 else "members"
    await interaction.response.send_message(f"✅ Quick event **{name}** set for **{dt_str}** ({minutes}m from now).")

@event_menu.command(name="list", description="List your events and recorded lateness/earliness")
async def list_events(interaction: Interaction, member: discord.Member = None):
    target = member or interaction.user
    rows = query_db("SELECT name, time, lateness, started FROM events WHERE user_id = ? AND guild_id = ?", 
                    (str(target.id), str(interaction.guild.id)))
    
    if not rows: 
        return await interaction.response.send_message(f"📅 No events found for {target.display_name}", ephemeral=True)
    
    msg = f"📅 **{target.display_name}'s Events:**\n"
    
    for i, (name, timestamp, late, started) in enumerate(rows, 1):
        if late is not None:
            # Calculate absolute minutes and seconds for display
            m, s = abs(late) // 60, abs(late) % 60
            time_str = f"{m}m {s}s"
            
            # Label based on original value
            if late < 0:
                status = f"✅ Early: {time_str}"
            elif late == 0:
                status = " Exactly on time!"
            else:
                status = f"✅ Late: {time_str}"
        else:
            status = f"{timestamp} ⏳ Ongoing" if started else f"🕒 {timestamp}"
            
        msg += f"{i}. **{name}** — {timestamp} {status}\n"
    
    await interaction.response.send_message(msg, ephemeral=True)


@event_menu.command(name="stop", description="Stop a specific named event for members/role")
async def stop(interaction: Interaction, 
               event_name: str, 
               member1: discord.Member = None, 
               member2: discord.Member = None,
               member3: discord.Member = None,
               member4: discord.Member = None,
               member5: discord.Member = None,
               role: discord.Role = None):
    
    target_members = set()
    for m in [member1, member2, member3, member4, member5]:
        if m: target_members.add(m)
    
    if role:
        for m in role.members:
            if not m.bot: target_members.add(m)
                
    if not target_members:
        target_members.add(interaction.user)

    now = datetime.now()
    guild_id = str(interaction.guild.id)
    success_count = 0
    last_diff = 0 # To store the diff for the response

    for member in target_members:
        uid = str(member.id)
        row = query_db(
            "SELECT rowid, time FROM events WHERE user_id = ? AND guild_id = ? AND name = ? AND lateness IS NULL ORDER BY rowid DESC LIMIT 1", 
            (uid, guild_id, event_name)
        )
        
        if row:
            internal_id, target_time_str = row[0]
            try:
                if len(target_time_str) > 5:
                    target_dt = datetime.strptime(target_time_str, "%Y-%m-%d %H:%M")
                else:
                    target_dt = datetime.strptime(target_time_str, "%H:%M").replace(
                        year=now.year, month=now.month, day=now.day
                    )
                
                diff = int((now - target_dt).total_seconds())
                last_diff = diff 
                
                query_db("UPDATE events SET lateness = ?, started = 1 WHERE rowid = ?", (diff, internal_id))
                success_count += 1
            except Exception as e:
                print(f"Error processing {member.display_name}: {e}")

    if success_count == 0:
        return await interaction.response.send_message(f"❌ No active event named '**{event_name}**' found for the specified members.", ephemeral=True)

    # Response Logic
    unit = "member" if success_count == 1 else "members"
    time_str = f"{abs(last_diff)//60}m {abs(last_diff)%60}s"
    
    if last_diff < 0:
        await interaction.response.send_message(f"✅ Early arrival! Recorded **-{time_str}** for '**{event_name}**' ({success_count} {unit}).")
    else:
        await interaction.response.send_message(f"⏹️ Stopped '**{event_name}**' for **{success_count}** {unit}. Lateness: **{time_str}**.")

# @event_menu.command(name="stop", description="Stop the timer (records negative if early)")
# async def stop(interaction: Interaction, name: str):
#     uid, gid = str(interaction.user.id), str(interaction.guild.id)
#     # Removed the 'started = 1' requirement so you can stop it early
#     row = query_db("SELECT time FROM events WHERE user_id = ? AND guild_id = ? AND name = ? AND lateness IS NULL", (uid, gid, name), one=True)
    
#     if not row: 
#         return await interaction.response.send_message("❌ No active or pending event found with that name.", ephemeral=True)
    
#     event_time = datetime.strptime(row[0], "%Y-%m-%d %H:%M")
#     now = datetime.now()
    
#     # Calculate total seconds (Negative = Early, Positive = Late)
#     late_seconds = int((now - event_time).total_seconds())
    
#     query_db("UPDATE events SET lateness = ?, started = 0 WHERE user_id = ? AND guild_id = ? AND name = ?", 
#              (late_seconds, uid, gid, name))
    
#     if late_seconds < 0:
#         abs_early = abs(late_seconds)
#         await interaction.response.send_message(f" Early arrival! Recorded **-{abs_early//60}m {abs_early%60}s** for '{name}'.", ephemeral=True)
#     else:
#         await interaction.response.send_message(f" Stopped '{name}'. Lateness: **{late_seconds//60}m {late_seconds%60}s**.", ephemeral=True)

@event_menu.command(name="delete", description="Delete one of your events")
async def delete(interaction: Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    
    try:
        query_db("DELETE FROM events WHERE user_id = ? AND guild_id = ? AND name = ?", 
                 (str(interaction.user.id), str(interaction.guild.id), name))
        await interaction.followup.send(f" Deleted event: '{name}'")
    except sqlite3.OperationalError:
        await interaction.followup.send("❌ Database is currently busy. Please try again in a few seconds.")


@event_menu.command(name="clear", description="Clear all your events in this server")
async def clear(interaction: Interaction):
    query_db("DELETE FROM events WHERE user_id = ? AND guild_id = ?", (str(interaction.user.id), str(interaction.guild.id)))
    await interaction.response.send_message(" Your events in this server cleared.", ephemeral=True)

@event_menu.command(name="list_all", description="View everyone's events in this server")
async def list_all(interaction: Interaction):
    rows = query_db("SELECT user_id, username, name, time, lateness, started FROM events WHERE guild_id = ? ORDER BY user_id ASC", (str(interaction.guild.id),))
    
    if not rows: 
        return await interaction.response.send_message("📅 No events found in this server.", ephemeral=True)
    
    msg = f" **{interaction.guild.name} Event Board**\n"
    curr = None
    
    for uid, uname, name, timestamp, late, started in rows:
        if uid != curr:
            curr = uid
            msg += f"\n👤 **{uname or f'<@{uid}>'}**\n"
        
        if late is not None:
            # 1. Always use absolute value for the numbers
            abs_late = abs(late)
            m, s = abs_late // 60, abs_late % 60
            time_str = f"{m}m {s}s"
            
            # 2. Use the original 'late' value to pick the label/emoji
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

@event_menu.command(name="add_schedule", description="Set a recurring weekly event")
async def add_schedule(interaction: Interaction, name: str, day: str, time_24h: str):
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    day = day.lower().strip()
    if day not in days: return await interaction.response.send_message("❌ Invalid day.", ephemeral=True)
    query_db("INSERT INTO schedules (guild_id, user_id, username, name, day_of_week, time_24h) VALUES (?, ?, ?, ?, ?, ?)", 
             (str(interaction.guild.id), str(interaction.user.id), str(interaction.user), name, days.index(day), time_24h))
    await interaction.response.send_message(f"🗓️ Recurring: **{name}** every {day.capitalize()} at {time_24h}.")

@event_menu.command(name="delete_schedule", description="Delete a recurring schedule")
async def delete_schedule(interaction: Interaction, name: str):
    query_db("DELETE FROM schedules WHERE user_id = ? AND guild_id = ? AND name = ?", (str(interaction.user.id), str(interaction.guild.id), name))
    await interaction.response.send_message(f" Deleted schedule: {name}", ephemeral=True)

# --- ADMIN COMMANDS ---

@admin_menu.command(name="delete", description="Admin: Delete a specific named event entry")
@app_commands.checks.has_permissions(manage_guild=True)
async def admin_delete(interaction: Interaction, 
                       event_name: str,
                       member1: discord.Member = None, 
                       member2: discord.Member = None,
                       member3: discord.Member = None,
                       member4: discord.Member = None,
                       member5: discord.Member = None,
                       role: discord.Role = None):
    
    target_members = set()
    for m in [member1, member2, member3, member4, member5]:
        if m: target_members.add(m)
    
    if role:
        for m in role.members:
            if not m.bot: target_members.add(m)

    if not target_members:
        target_members.add(interaction.user)

    guild_id = str(interaction.guild.id)
    actually_deleted = 0

    for member in target_members:
        uid = str(member.id)
        
        check = query_db(
            "SELECT rowid FROM events WHERE user_id = ? AND guild_id = ? AND name = ? ORDER BY rowid DESC LIMIT 1",
            (uid, guild_id, event_name)
        )
        
        if check:
            target_rowid = check[0][0]
            query_db("DELETE FROM events WHERE rowid = ?", (target_rowid,))
            actually_deleted += 1
    
    if actually_deleted == 0:
        await interaction.response.send_message(
            f" No entries found for '**{event_name}**' among the specified members.", 
            ephemeral=True
        )
    else:
        unit = "entry" if actually_deleted == 1 else "entries"
        await interaction.response.send_message(
            f" [ADMIN] Successfully deleted **{actually_deleted}** {unit} of '**{event_name}**'!"
        )

@admin_menu.command(name="clear", description="Admin: Clear ALL user data for members/role")
@app_commands.checks.has_permissions(manage_guild=True)
async def admin_clear(interaction: Interaction, 
                      member1: discord.Member = None, 
                      member2: discord.Member = None,
                      member3: discord.Member = None,
                      member4: discord.Member = None,
                      member5: discord.Member = None,
                      role: discord.Role = None):
    
    target_members = set()
    for m in [member1, member2, member3, member4, member5]:
        if m: target_members.add(m)
    
    if role:
        for m in role.members:
            if not m.bot: target_members.add(m)

    # For 'Clear', we force a target to avoid accidental server-wide wipes
    if not target_members:
        return await interaction.response.send_message("❌ Specify who to clear! (Tag someone or a role)", ephemeral=True)

    guild_id = str(interaction.guild.id)
    for member in target_members:
        query_db("DELETE FROM events WHERE user_id = ? AND guild_id = ?", (str(member.id), guild_id))
    
    member_count = len(target_members)
    unit = "member" if member_count == 1 else "members"
    await interaction.response.send_message(f"💥 [ADMIN] Full history wiped for **{member_count}** {unit}!")
# @admin_menu.command(name="delete", description="Admin: Delete event from other user")
# @app_commands.checks.has_permissions(manage_guild=True)
# async def admin_delete(interaction: Interaction, member: discord.Member, event_name: str):
#     query_db("DELETE FROM events WHERE user_id = ? AND guild_id = ? AND name = ?", (str(member.id), str(interaction.guild.id), event_name))
#     await interaction.response.send_message(f" Admin: Deleted '{event_name}' for {member.display_name}")

# @admin_menu.command(name="clear", description="Admin: Clear all user data in this server")
# @app_commands.checks.has_permissions(manage_guild=True)
# async def admin_clear(interaction: Interaction, member: discord.Member):
#     query_db("DELETE FROM events WHERE user_id = ? AND guild_id = ?", (str(member.id), str(interaction.guild.id)))
#     await interaction.response.send_message(f" Admin: Cleared data for {member.display_name}")

@admin_menu.command(name="stop", description="Admin: Force stop a user's timer (allows negative lateness)")
@app_commands.checks.has_permissions(manage_guild=True)
async def admin_stop(interaction: Interaction, member: discord.Member, name: str):
    uid, gid = str(member.id), str(interaction.guild.id)
    
    # Look for any event for this user that hasn't been finished (lateness is NULL)
    row = query_db("SELECT time FROM events WHERE user_id = ? AND guild_id = ? AND name = ? AND lateness IS NULL", (uid, gid, name), one=True)
    
    if not row: 
        return await interaction.response.send_message(f"❌ No active/pending event found for {member.display_name} with that name.", ephemeral=True)
    
    event_time = datetime.strptime(row[0], "%Y-%m-%d %H:%M")
    now = datetime.now()
    
    # Calculate difference (Negative = Early, Positive = Late)
    late_seconds = int((now - event_time).total_seconds())
    
    query_db("UPDATE events SET lateness = ?, started = 0 WHERE user_id = ? AND guild_id = ? AND name = ?", 
             (late_seconds, uid, gid, name))
    
    if late_seconds < 0:
        abs_early = abs(late_seconds)
        await interaction.response.send_message(f" Admin: Force-stopped '{name}' early for {member.mention}. Recorded **-{abs_early//60}m {abs_early%60}s**.")
    else:
        await interaction.response.send_message(f" Admin: Force-stopped '{name}' for {member.mention}. Lateness: **{late_seconds//60}m {late_seconds%60}s**.")

@admin_menu.command(name="add_record", description="Admin: Add a finished event record")
@app_commands.checks.has_permissions(manage_guild=True)
async def admin_add_record(interaction: Interaction, member: discord.Member, name: str, lateness_minutes: int, date_str: str = None):
    if not date_str: date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    query_db("INSERT INTO events (guild_id, user_id, username, name, time, lateness, started) VALUES (?, ?, ?, ?, ?, ?, 0)", 
             (str(interaction.guild.id), str(member.id), str(member), name, date_str, lateness_minutes * 60))
    await interaction.response.send_message(f"✅ Added record for {member.display_name}.")

@admin_menu.command(name="add_schedule", description="Admin: Add schedule for member")
@app_commands.checks.has_permissions(manage_guild=True)
async def admin_add_schedule(interaction: Interaction, member: discord.Member, name: str, day: str, time_24h: str):
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    day = day.lower().strip()
    if day not in days: return await interaction.response.send_message("❌ Invalid day.")
    query_db("INSERT INTO schedules (guild_id, user_id, username, name, day_of_week, time_24h) VALUES (?, ?, ?, ?, ?, ?)", 
             (str(interaction.guild.id), str(member.id), str(member), name, days.index(day), time_24h))
    await interaction.response.send_message(f"🗓️ Admin set schedule for {member.display_name}")

@admin_menu.command(name="delete_user_schedule", description="Admin: Delete user schedule")
@app_commands.checks.has_permissions(manage_guild=True)
async def admin_delete_user_schedule(interaction: Interaction, member: discord.Member, name: str):
    query_db("DELETE FROM schedules WHERE user_id = ? AND guild_id = ? AND name = ?", (str(member.id), str(interaction.guild.id), name))
    await interaction.response.send_message(f" Admin deleted schedule for {member.display_name}")

# --- SYSTEM COMMANDS ---

@admin_menu.command(name="export", description="Export server data to JSON")
@app_commands.checks.has_permissions(manage_guild=True)
async def admin_export(interaction: Interaction):
    rows = query_db("SELECT * FROM events WHERE guild_id = ?", (str(interaction.guild.id),))
    data = [{"gid": r[0], "uid": r[1], "user": r[2], "name": r[3], "time": r[4], "late": r[5], "start": r[6]} for r in rows]
    with open(f"export_{interaction.guild.id}.json", "w") as f: json.dump(data, f, indent=4)
    await interaction.response.send_message("✅ Exported!", file=discord.File(f"export_{interaction.guild.id}.json"), ephemeral=True)

@admin_menu.command(name="import", description="Import from JSON string")
@app_commands.checks.has_permissions(manage_guild=True)
async def admin_import(interaction: Interaction, json_data: str):
    try:
        data = json.loads(json_data)
        for e in data:
            query_db("INSERT INTO events (guild_id, user_id, username, name, time, lateness, started) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                     (e.get('gid', str(interaction.guild.id)), e['uid'], e.get('user', 'Unknown'), e['name'], e['time'], e['late'], e['start']))
        await interaction.response.send_message("✅ Imported successfully!", ephemeral=True)
    except Exception as ex: await interaction.response.send_message(f"❌ Error: {ex}", ephemeral=True)

# --- LOOPS & AUTOMATION ---

@tasks.loop(seconds=20)
async def auto_check():
    now = datetime.now()
    now_str, day_idx, time_str, date_only = now.strftime("%Y-%m-%d %H:%M"), now.weekday(), now.strftime("%H:%M"), now.strftime("%Y-%m-%d")
    
    recurring = query_db("SELECT guild_id, user_id, username, name FROM schedules WHERE day_of_week = ? AND time_24h = ?", (day_idx, time_str))
    if recurring:
        for gid, uid, uname, name in recurring:
            if not query_db("SELECT name FROM events WHERE guild_id = ? AND user_id = ? AND name = ? AND time LIKE ?", (gid, uid, name, f"{date_only}%"), one=True):
                query_db("INSERT INTO events (guild_id, user_id, username, name, time, lateness, started) VALUES (?, ?, ?, ?, ?, NULL, 1)", (gid, uid, uname, name, now_str))
                try: 
                    user = await bot.fetch_user(int(uid))
                    await user.send(f"⏰ **Scheduled Event Started:** {name}")
                except: pass

    pending = query_db("SELECT user_id, name, guild_id FROM events WHERE time <= ? AND started = 0 AND lateness IS NULL", (now_str,))
    if pending:
        for uid, name, gid in pending:
            query_db("UPDATE events SET started = 1 WHERE user_id = ? AND guild_id = ? AND name = ?", (uid, gid, name))
            try: 
                user = await bot.fetch_user(int(uid))
                await user.send(f"⚠️ **Event Starting Now:** {name}")
            except: pass

@bot.event
async def on_voice_state_update(member, before, after):
    # User joins a voice channel
    if before.channel is None and after.channel is not None:
        gid, uid = str(member.guild.id), str(member.id)
        
        # Look for any event for this user that hasn't been finished yet
        active = query_db("SELECT name, time FROM events WHERE user_id = ? AND guild_id = ? AND lateness IS NULL", (uid, gid))
        
        if active:
            for name, timestamp in active:
                event_time = datetime.strptime(timestamp, "%Y-%m-%d %H:%M")
                now = datetime.now()
                late_seconds = int((now - event_time).total_seconds())
                
                # Update the database
                query_db("UPDATE events SET lateness = ?, started = 0 WHERE user_id = ? AND guild_id = ? AND name = ?", 
                         (late_seconds, uid, gid, name))
                
                chan = discord.utils.get(member.guild.text_channels, name="general")
                if chan:
                    if late_seconds < 0:
                        abs_early = abs(late_seconds)
                        await chan.send(f" {member.mention} is early! Saved **-{abs_early//60}m {abs_early%60}s** for **{name}**.")
                    else:
                        await chan.send(f"✅ {member.mention} arrived! Late for **{name}**: {late_seconds//60}m {late_seconds%60}s")

@bot.event
async def on_ready():
    bot.tree.add_command(event_menu)
    bot.tree.add_command(admin_menu)
    await bot.tree.sync()
    print("ML model refresh")
    try:
        ai_pipeline.train()
        print("ML model trained and ready")
    except Exception as e:
        print(f"ML model was not trained: {e}")
    if not auto_check.is_running(): auto_check.start()
    print(f"Logged in as {bot.user}")

#AI STUFF
@event_menu.command(name="predict", description="AI: Predict lateness for an ongoing event")
@app_commands.autocomplete(event_name=event_autocomplete)
async def predict_lateness(interaction: Interaction, event_name: str, member: discord.Member = None):
    target = member or interaction.user
    
    
    row = query_db(
        "SELECT time FROM events WHERE name = ? AND user_id = ? AND guild_id = ? ORDER BY rowid DESC LIMIT 1", 
        (event_name, str(target.id), str(interaction.guild.id)), 
        one=True
    )

    if not row:
        return await interaction.response.send_message("❌ I couldn't find that event for this user.", ephemeral=True)

    
    event_datetime = row[0] 

    pred_res, lower_res, upper_res = ai_pipeline.predict_with_confidence(
        user_id=str(target.id),
        event_name=event_name,
        event_time=event_datetime
    )

    if pred_res is None:
        return await interaction.response.send_message("❌ Not enough data for this user yet.", ephemeral=True)

    def clean_format(decimal_mins):
        total_seconds = abs(int(decimal_mins * 60))
        mins, secs = divmod(total_seconds, 60)
        label = "Early" if decimal_mins < 0 else "Late"
        return f"{mins}m {secs}s {label}"

    main_pred_str = clean_format(pred_res[0])
    range_start = clean_format(lower_res[0])
    range_end = clean_format(upper_res[0])

    msg = f"🔮 Prediction: **{target.display_name}** will be **{main_pred_str}**."
    msg += f"\n📊 *80% Confidence Range: `{range_start}` to `{range_end}`*"
    
    await interaction.response.send_message(msg)

@admin_menu.command(name="retrain", description="Admin: Manually retrain the lateness model")
@app_commands.checks.has_permissions(manage_guild=True)
async def retrain_model(interaction: Interaction):
    await interaction.response.defer(ephemeral=True)
    
    # CHANGE: Use 'ai_pipeline.train()'
    try:
        ai_pipeline.train()
        await interaction.followup.send("✅ Model retrained successfully on new data!")
    except Exception as e:
        await interaction.followup.send(f"❌ Model retraining failed: {e}")

# ... [Keep the rest of your bot code: on_voice_state_update, etc.] ...

bot.run(TOKEN)