import discord
from discord.ext import commands, tasks
import json
import time
from datetime import datetime
from dateutil import parser

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
DATA_FILE = "event_data.dat"

# Load / save data
def load_data():
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_data():
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)

data = load_data()

def get_user(user_id):
    if user_id not in data:
        data[user_id] = {"events": [], "lateness": []}
    return data[user_id]


auto_timers = {}  
manual_timers = {}  

#create event
@bot.tree.command(name="event_create", description="Create an event with date and time")
async def event_create(
    interaction: discord.Interaction,
    name: str,
    date: str = discord.Option(description="Date of the event, e.g., 2026-03-28 or 03-28-2026"),
    time_str: str = discord.Option(description="Time of the event in 24h HH:MM format, e.g., 16:00"),
    member: discord.Member = None,
    channel: discord.VoiceChannel = None
):
    user = get_user(str(member.id if member else interaction.user.id))
    
    # Try multiple date formats
    date_formats = ["%Y-%m-%d %H:%M", "%m-%d-%Y %H:%M"]
    datetime_obj = None
    for fmt in date_formats:
        try:
            datetime_obj = datetime.strptime(f"{date} {time_str}", fmt)
            break
        except ValueError:
            continue

    if not datetime_obj:
        await interaction.response.send_message(
            "Invalid date/time format. Use YYYY-MM-DD or MM-DD-YYYY for date, HH:MM (24h) for time.",
            ephemeral=True
        )
        return

    # Store consistently in YYYY-MM-DD HH:MM
    event = {
        "name": name,
        "datetime": datetime_obj.strftime("%Y-%m-%d %H:%M"),
        "channel_id": channel.id if channel else None,
        "lateness": None,
        "started": False
    }

    user["events"].append(event)
    save_data()

    await interaction.response.send_message(
        f"Event '{name}' created for {datetime_obj.strftime('%Y-%m-%d %H:%M')}" +
        (f" in {channel.name}" if channel else ""),
        ephemeral=True
    )

#manual start 
@bot.tree.command(name="event_late_start", description="Start lateness stopwatch for IRL event")
async def event_late_start(interaction: discord.Interaction, event_name: str):
    user_id = str(interaction.user.id)

    if user_id in manual_timers:
        await interaction.response.send_message("Active timer already recording lateness.", ephemeral=True)
        return
    
    manual_timers[user_id] = {
        "start": time.time(),
        "event_name": event_name
    }

    await interaction.response.send_message(f"Lateness record started for '{event_name}'", ephemeral=True)

#manual stop
@bot.tree.command(name="event_late_stop", description="Stop record for lateness")
async def event_late_stop(interaction: discord.Interaction):
    user_id = str(interaction.user.id)


    timer = manual_timers.pop(user_id, None)

  
    if not timer:
        user_auto_timers = auto_timers.get(user_id, [])
        if user_auto_timers:
            timer = user_auto_timers.pop(0)
            auto_timers[user_id] = user_auto_timers

  
    if not timer:
        user = get_user(user_id)
        for e in user["events"]:
            if e.get("started") and e.get("lateness") is None:
                try:
                    # Use event start time to calculate lateness
                    event_time = datetime.strptime(e["datetime"], "%Y-%m-%d %H:%M")
                    timer = {
                        "start": time.mktime(event_time.timetuple()),
                        "event_name": e["name"]
                    }
                    break
                except:
                    continue


    if not timer:
        await interaction.response.send_message("No active recording of lateness", ephemeral=True)
        return


    late_seconds = int(time.time() - timer["start"])
    user = get_user(user_id)
    event_found = False

    for e in user["events"]:
        if e["name"] == timer["event_name"] and e.get("lateness") is None:
            e["lateness"] = late_seconds
            event_found = True
            break


    if not event_found:
        event = {
            "name": timer["event_name"],
            "datetime": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "channel_id": None,
            "lateness": late_seconds,
            "started": True
        }
        user["events"].append(event)

    save_data()
    await interaction.response.send_message(
        f"{interaction.user.mention} is late for {late_seconds}s for '{timer['event_name']}'",
        ephemeral=True
    )

#list events
@bot.tree.command(name="event_list", description="View current events for a user")
async def event_list(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    user_id = str(target.id)
    user = get_user(user_id)

    if not user["events"]:
        await interaction.response.send_message(f"{target.display_name} has no events", ephemeral=True)
        return

    message = f"{target.display_name}'s Events:\n\n"

    for i, event in enumerate(user["events"], start=1):
        status = "Started" if event.get("started") else "Not started"

        if event.get("lateness") is not None:
            mins = event["lateness"] // 60
            secs = event["lateness"] % 60
            lateness = f"Late: {mins}m {secs}s"
        elif event.get("started"):
            # Dynamic ongoing
            user_timers = auto_timers.get(user_id, [])
            ongoing_timer = next((t for t in user_timers if t["event_name"] == event["name"]), None)
            if ongoing_timer:
                current_late = int(time.time() - ongoing_timer["start"])
                mins = current_late // 60
                secs = current_late % 60
                lateness = f"Late: {mins}m {secs}s (ongoing)"
            else:
                lateness = "In progress"
        else:
            lateness = "Not started"

        message += f"{i}. {event['name']} - {event['datetime']} ({status}, {lateness})\n"

    await interaction.response.send_message(message, ephemeral=True)

#delete event
@bot.tree.command(name="event_delete", description="Delete an event by name")
async def event_delete(interaction: discord.Interaction, event_name: str):
    user_id = str(interaction.user.id)
    user = get_user(user_id)

    before_count = len(user["events"])
    user["events"] = [e for e in user["events"] if e["name"] != event_name]
    after_count = len(user["events"])

    save_data()
    if before_count == after_count:
        await interaction.response.send_message(f"No event found with name '{event_name}'", ephemeral=True)
    else:
        await interaction.response.send_message(f"Deleted event '{event_name}'", ephemeral=True)

#clear all events
@bot.tree.command(name="event_clear", description="Clear all events for the user")
async def event_clear(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    user = get_user(user_id)
    user["events"] = []
    auto_timers.pop(user_id, None)
    manual_timers.pop(user_id, None)
    save_data()
    await interaction.response.send_message("All your events have been cleared.", ephemeral=True)

#auto start
@tasks.loop(seconds=30)
async def auto_start_events():
    now = datetime.now()

    for user_id, user_data in data.items():
        for event in user_data["events"]:
            if event.get("started"):
                continue
            if "datetime" not in event:
                continue
            try:
                event_time = datetime.strptime(event["datetime"], "%Y-%m-%d %H:%M")
            except Exception:
                print(f"Invalid datetime for event {event['name']}: {event.get('datetime')}")
                continue
            if now >= event_time:
                event["started"] = True
                # Add to auto timers (support multiple timers per user)
                auto_timers.setdefault(user_id, []).append({
                    "event_name": event["name"],
                    "start": time.time()
                })
                print(f"Auto-started timer for {event['name']} ({user_id})")

    save_data()

@bot.tree.command(name="event_late_set", description="Manually set lateness for an event")
async def event_late_set(interaction: discord.Interaction, event_name: str, late_seconds: int):
    """
    Set the lateness for a user's event manually.
    - event_name: the name of the event
    - late_seconds: lateness in seconds (can convert minutes externally)
    """
    user_id = str(interaction.user.id)
    user = get_user(user_id)
    event_found = False

    for e in user["events"]:
        if e["name"] == event_name:
            e["lateness"] = late_seconds
            e["started"] = True 
            event_found = True
            break

    if not event_found:
      
        event = {
            "name": event_name,
            "datetime": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "channel_id": None,
            "lateness": late_seconds,
            "started": True
        }
        user["events"].append(event)

    save_data()
    mins = late_seconds // 60
    secs = late_seconds % 60
    await interaction.response.send_message(
        f"{interaction.user.mention}'s lateness for '{event_name}' manually set to {mins}m {secs}s",
        ephemeral=True
    )

#auto stop when voice join
@bot.event
async def on_voice_state_update(member, before, after):
    user_id = str(member.id)

    # Joined a voice channel
    if before.channel is None and after.channel is not None:
        user_timers = auto_timers.get(user_id, [])
        if user_timers:
            for timer in user_timers:
                late_seconds = int(time.time() - timer["start"])
                user = get_user(user_id)
                for e in user["events"]:
                    if e["name"] == timer["event_name"] and e.get("lateness") is None:
                        e["lateness"] = late_seconds
                        print(f"{member.display_name} joined {after.channel.name}, lateness recorded: {late_seconds}s")
                        break
            auto_timers[user_id] = []
            save_data()


@bot.event
async def on_ready():
    await bot.tree.sync()
    auto_start_events.start()
    print(f"Logged in as {bot.user}")


bot.run("TOKEN")