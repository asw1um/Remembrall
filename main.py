import discord
from discord.ext import commands, tasks
import json
import time
from datetime import datetime

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix = "!", intents = intents)
DATA_FILE = "lateness_data.dat"

def load_data():
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f);
    except:
        return{};

def save_data():
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent =4)


data = load_data()

def get_user(user_id):  #just in case if we wanna log other latness
    if user_id not in data:
        data[user_id] = {
            "schedule" : [],
            "lateness"  : []
        }
    return data[user_id]

auto_timers={}
manual_timers={}

@bot.tree.command(name = "event", description = "Insert event")