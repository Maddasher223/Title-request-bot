# main.py - Complete Code

import discord
from discord.ext import commands, tasks
import json
import os
import logging
from datetime import datetime, timedelta
import asyncio
from threading import Thread
from flask import Flask, render_template, request, redirect, url_for, send_from_directory
import csv
from waitress import serve
import requests

# --- Static Title Configuration ---
TITLES_CATALOG = {
    "Guardian of Harmony": {"effects": "All benders' ATK +5%, All benders' DEF +5%, All Benders' recruiting speed +15%", "image": "https://cdn.discordapp.com/attachments/1409793076955840583/1409793727018569758/guardian_harmony.png"},
    "Guardian of Air": {"effects": "All Resource Gathering Speed +20%, All Resource Production +20%", "image": "https://cdn.discordapp.com/attachments/1409793076955840583/1409793463817605181/guardian_air.png"},
    "Guardian of Water": {"effects": "All Benders' recruiting speed +15%", "image": "https://cdn.discordapp.com/attachments/1409793076955840583/1409793588778369104/guardian_water.png"},
    "Guardian of Earth": {"effects": "Construction Speed +10%, Research Speed +10%", "image": "https://cdn.discordapp.com/attachments/1409793076955840583/1409794927730229278/guardian_earth.png"},
    "Guardian of Fire": {"effects": "All benders' ATK +5%, All benders' DEF +5%", "image": "https://cdn.discordapp.com/attachments/1409793076955840583/1409794024948367380/guardian_fire.png"},
    "Architect": {"effects": "Construction Speed +10%", "image": "https://cdn.discordapp.com/attachments/1409793076955840583/1409796581661605969/architect.png"},
    "General": {"effects": "All benders' ATK +5%", "image": "https://cdn.discordapp.com/attachments/1409793076955840583/1409796597277266000/general.png"},
    "Governor": {"effects": "All Benders' recruiting speed +10%", "image": "https://cdn.discordapp.com/attachments/1409793076955840583/1409796936227356723/governor.png"},
    "Prefect": {"effects": "Research Speed +10%", "image": "https://cdn.discordapp.com/attachments/1409793076955840583/1409797574763741205/prefect.png"},
}
REQUESTABLE = {"Architect", "Governor", "Prefect", "General"}
ORDERED_TITLES = [
    "Guardian of Harmony", "Guardian of Air", "Guardian of Water", "Guardian of Earth", "Guardian of Fire",
    "Architect", "General", "Governor", "Prefect"
]
WEBHOOK_URL = "https://discord.com/api/webhooks/1409980293762253001/s5ffx0R9Tl9fhcvQXAWaqA_LG5b7SsUmpzeBHZOdGGznnLg_KRNwtk6sGvOOhh0oSw10"
GUARDIAN_ROLE_ID = 1409964411057344512
TITLE_REQUESTS_CHANNEL_ID = 1409770504696631347

# --- Initial Setup ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# --- State and Logging Configuration ---
STATE_FILE = 'titles_state.json'
LOG_FILE = 'log.json'
CSV_FILE = 'requests.csv'
state = {}
state_lock = asyncio.Lock()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s:%(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger(__name__)

# --- Helper Functions ---
async def initialize_titles():
    """Ensures all titles from the catalog exist in the state file."""
    async with state_lock:
        state.setdefault('titles', {})
        for title_name, details in TITLES_CATALOG.items():
            if title_name not in state['titles']:
                state['titles'][title_name] = {'holder': None, 'queue': [], 'claim_date': None, 'expiry_date': None, 'pending_claimant': None}
            state['titles'][title_name]['icon'] = details['image']
            state['titles'][title_name]['buffs'] = details['effects']
    await save_state()

async def load_state():
    """Loads the bot's state from a JSON file."""
    global state
    async with state_lock:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    state = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Error loading state file: {e}")
                initialize_state()
        else:
            initialize_state()

def initialize_state():
    """Initializes a default state structure."""
    global state
    state = {'titles': {}, 'users': {}, 'config': {}, 'schedules': {}, 'sent_reminders': []}

async def save_state():
    """Saves the bot's state to a JSON file."""
    async with state_lock:
        try:
            with open(STATE_FILE, 'w') as f:
                json.dump(state, f, indent=4)
        except IOError as e:
            logger.error(f"Error saving state file: {e}")

def log_action(action, user_id, details):
    log_entry = {'timestamp': datetime.utcnow().isoformat(), 'action': action, 'user_id': user_id, 'details': details}
    try:
        logs = []
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'r') as f:
                try: logs = json.load(f)
                except json.JSONDecodeError: pass
        logs.append(log_entry)
        with open(LOG_FILE, 'w') as f:
            json.dump(logs, f, indent=4)
    except IOError as e:
        logger.error(f"Error writing to log file: {e}")

def log_to_csv(request_data):
    file_exists = os.path.isfile(CSV_FILE)
    try:
        with open(CSV_FILE, 'a', newline='') as csvfile:
            fieldnames = ['timestamp', 'title_name', 'in_game_name', 'coordinates', 'discord_user']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(request_data)
    except IOError as e:
        logger.error(f"Error writing to CSV file: {e}")

def send_webhook_notification(data):
    """Sends a notification to a Discord webhook."""
    payload = {
        "content": f"<@&{GUARDIAN_ROLE_ID}> New Title Request Submitted!",
        "embeds": [{
            "title": "New Title Request",
            "color": 5814783,
            "fields": [
                {"name": "Title", "value": data['title_name'], "inline": True},
                {"name": "In-Game Name", "value": data['in_game_name'], "inline": True},
                {"name": "Coordinates", "value": data['coordinates'], "inline": True},
                {"name": "Submitted By", "value": data['discord_user'], "inline": False}
            ],
            "timestamp": data['timestamp']
        }]
    }
    try:
        response = requests.post(WEBHOOK_URL, json=payload)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error sending webhook notification: {e}")

def is_guardian_or_admin(ctx):
    """Check if the user is a Guardian or an Admin."""
    if ctx.author.guild_permissions.administrator:
        return True
    guardian_role_ids = state.get('config', {}).get('guardian_roles', [])
    user_role_ids = {role.id for role in ctx.author.roles}
    return any(role_id in user_role_ids for role_id in guardian_role_ids)

# --- Main Bot Cog ---
class TitleCog(commands.Cog, name="TitleRequest"):
    def __init__(self, bot):
        self.bot = bot
        self.title_check_loop.start()

    @tasks.loop(minutes=1)
    async def title_check_loop(self):
        await self.bot.wait_until_ready()
        now = datetime.utcnow()
        
        titles_to_release = []
        for title_name, data in state.get('titles', {}).items():
            if data.get('holder') and data.get('expiry_date'):
                if now >= datetime.fromisoformat(data['expiry_date']):
                    titles_to_release.append(title_name)
        
        for title_name in titles_to_release:
            await self.force_release_logic(title_name, self.bot.user.id, "Title expired.")

        state.setdefault('sent_reminders', [])
        for title_name, schedule_data in state.get('schedules', {}).items():
            for iso_time, ign in schedule_data.items():
                if iso_time in state['sent_reminders']: continue
                
                shift_time = datetime.fromisoformat(iso_time)
                reminder_time = shift_time - timedelta(minutes=5)

                if reminder_time <= now < shift_time:
                    try:
                        channel = await self.bot.fetch_channel(TITLE_REQUESTS_CHANNEL_ID)
                        await channel.send(f"<@&{GUARDIAN_ROLE_ID}> Reminder: The 3-hour shift for **{title_name}** held by **{ign}** starts in 5 minutes!")
                        state['sent_reminders'].append(iso_time)
                    except (discord.NotFound, discord.Forbidden) as e:
                        logger.error(f"Could not send shift reminder: {e}")

        if titles_to_release or any(iso_time not in state.get('sent_reminders', []) for schedule_data in state.get('schedules', {}).values() for iso_time in schedule_data):
            await save_state()

    @commands.command(help="Claim a title. Usage: !claim <Title Name> | <In-Game Name> | <X:Y Coords>")
    async def claim(self, ctx, *, args: str):
        try:
            title_name, ign, coords = [arg.strip() for arg in args.split('|')]
        except ValueError:
            await ctx.send("Invalid format. Use `!claim <Title Name> | <In-Game Name> | <X:Y Coords>`")
            return

        if title_name not in REQUESTABLE:
            await ctx.send(f"The title '{title_name}' is not requestable.")
            return

        if any(t.get('holder') and t['holder']['name'] == ign for t in state['titles'].values()):
            await ctx.send("You already hold a title.")
            return

        title = state['titles'][title_name]
        claimant_data = {'name': ign, 'coords': coords, 'discord_id': ctx.author.id}

        if not title.get('holder'):
            title['pending_claimant'] = claimant_data
            timestamp = datetime.utcnow().isoformat()
            discord_user = f"{ctx.author.name} ({ctx.author.id})"
            
            log_action('claim_request', ctx.author.id, {'title': title_name, 'ign': ign, 'coords': coords})
            csv_data = {'timestamp': timestamp, 'title_name': title_name, 'in_game_name': ign, 'coordinates': coords, 'discord_user': discord_user}
            log_to_csv(csv_data)
            send_webhook_notification(csv_data)

            guardian_message = (f"👑 **Title Request:** Player **{ign}** ({coords}) has requested **'{title_name}'**. "
                                f"Approve with `!assign {title_name} | {ign}`.")
            await self.notify_guardians(ctx.guild, title_name, guardian_message)
            await ctx.send(f"Your request for '{title_name}' for player **{ign}** has been submitted.")
        else:
            title.setdefault('queue', []).append(claimant_data)
            log_action('queue_join', ctx.author.id, {'title': title_name, 'ign': ign})
            await ctx.send(f"Player **{ign}** has been added to the queue for '{title_name}'.")
        await save_state()

    @commands.command(help="List all titles and their status.")
    async def titles(self, ctx):
        embed = discord.Embed(title="📜 Title Status", color=discord.Color.blue())
        for title_name in ORDERED_TITLES:
            data = state['titles'].get(title_name, {})
            details = TITLES_CATALOG.get(title_name, {})
            status = f"*{details.get('effects', 'No description.')}*\n"
            if data.get('holder'):
                holder = data['holder']
                holder_name = f"{holder['name']} ({holder['coords']})"
                expiry = datetime.fromisoformat(data['expiry_date'])
                remaining = expiry - datetime.utcnow()
                status += f"**Held by:** {holder_name}\n*Expires in: {str(timedelta(seconds=int(remaining.total_seconds())))}*"
            elif data.get('pending_claimant'):
                claimant = data['pending_claimant']
                status += f"**Pending Approval for:** {claimant['name']} ({claimant['coords']})"
            else:
                status += "**Status:** Available"
            embed.add_field(name=f"👑 {title_name}", value=status, inline=False)
        await ctx.send(embed=embed)

    @commands.command(help="Assign a title. Usage: !assign <Title Name> | <In-Game Name>")
    @commands.check(is_guardian_or_admin)
    async def assign(self, ctx, *, args: str):
        try:
            title_name, ign = [arg.strip() for arg in args.split('|')]
        except ValueError:
            await ctx.send("Invalid format. Use `!assign <Title Name> | <In-Game Name>`")
            return

        if title_name not in state['titles']:
            await ctx.send(f"Title '{title_name}' does not exist.")
            return

        title = state['titles'][title_name]
        pending_claimant = title.get('pending_claimant')
        if not pending_claimant or pending_claimant['name'] != ign:
            await ctx.send(f"**{ign}** is not the pending claimant for this title.")
            return

        min_hold_hours = state.get('config', {}).get('min_hold_duration_hours', 24)
        now = datetime.utcnow()
        expiry_date = now + timedelta(hours=min_hold_hours)
        title.update({
            'holder': pending_claimant, 'claim_date': now.isoformat(),
            'expiry_date': expiry_date.isoformat(), 'pending_claimant': None
        })
        log_action('assign', ctx.author.id, {'title': title_name, 'ign': ign})
        await save_state()
        user_mention = f"<@{title['holder']['discord_id']}>"
        await self.announce(f"🎉 SHIFT CHANGE: {user_mention}, player **{ign}** has been granted **'{title_name}'**.")
        await ctx.send(f"Successfully assigned '{title_name}' to player **{ign}**.")

    @commands.command(help="Set the announcement channel. Usage: !set_announce <#channel>")
    @commands.has_permissions(administrator=True)
    async def set_announce(self, ctx, channel: discord.TextChannel):
        state.setdefault('config', {})['announcement_channel'] = channel.id
        await save_state()
        await ctx.send(f"Announcement channel set to {channel.mention}.")

    @commands.command(help="Book a 3-hour time slot. Usage: !schedule <Title Name> | <In-Game Name> | <YYYY-MM-DD> | <HH:00>")
    async def schedule(self, ctx, *, full_argument: str):
        try:
            title_name, ign, date_str, time_str = [p.strip() for p in full_argument.split('|')]
        except ValueError:
            await ctx.send("Invalid format. Use `!schedule <Title Name> | <In-Game Name> | <YYYY-MM-DD> | <HH:00>`")
            return
        if title_name not in state['titles']:
            await ctx.send(f"Title '{title_name}' not found.")
            return
        try:
            schedule_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            if schedule_time.minute != 0 or schedule_time.hour % 3 != 0:
                raise ValueError
        except ValueError:
            await ctx.send("Invalid time. Use a 3-hour increment (00:00, 03:00, etc.).")
            return
        if schedule_time < datetime.now():
            await ctx.send("Cannot schedule a time in the past.")
            return
        schedules = state['schedules'].setdefault(title_name, {})
        schedule_key = schedule_time.isoformat()
        if schedule_key in schedules:
            await ctx.send(f"This slot is already booked by **{schedules[schedule_key]}**.")
            return
        for title_schedules in state['schedules'].values():
            if schedule_key in title_schedules and title_schedules[schedule_key] == ign:
                await ctx.send(f"**{ign}** has already booked another title for this slot.")
                return
        schedules[schedule_key] = ign
        log_action('schedule_book', ctx.author.id, {'title': title_name, 'time': schedule_key, 'ign': ign})
        await save_state()
        await ctx.send(f"Booked '{title_name}' for **{ign}** on {date_str} at {time_str} UTC.")
        await self.announce(f"🗓️ SCHEDULE UPDATE: A 3-hour slot for **'{title_name}'** was booked by **{ign}** for {date_str} at {time_str} UTC.")

    async def release_logic(self, ctx, title_name, reason):
        holder_info = state['titles'][title_name]['holder']
        log_action('release', ctx.author.id, {'title': title_name, 'ign': holder_info['name'], 'reason': reason})
        state['titles'][title_name].update({'holder': None, 'claim_date': None, 'expiry_date': None})
        await self.process_queue(ctx, title_name)
        await save_state()

    async def process_queue(self, ctx, title_name):
        queue = state['titles'][title_name].get('queue', [])
        if not queue:
            await self.announce(f"👑 The title **'{title_name}'** is now available!")
            return
        
        next_in_line = queue.pop(0)
        state['titles'][title_name]['pending_claimant'] = next_in_line
        user_mention = f"<@{next_in_line['discord_id']}>"
        guardian_message = (f"👑 **Next in Queue:** {user_mention}, it's **{next_in_line['name']}'s** turn for **'{title_name}'**! "
                            f"A guardian must use `!assign {title_name} | {next_in_line['name']}` to grant it.")
        await self.notify_guardians(ctx.guild, title_name, guardian_message)

    async def force_release_logic(self, title_name, actor_id, reason):
        if title_name not in state['titles'] or not state['titles'][title_name].get('holder'):
            return
        holder_info = state['titles'][title_name]['holder']
        log_action('force_release', actor_id, {'title': title_name, 'ign': holder_info['name'], 'reason': reason})
        state['titles'][title_name].update({'holder': None, 'claim_date': None, 'expiry_date': None})
        class FakeContext:
            def __init__(self, guild): self.guild = guild
        await self.process_queue(FakeContext(self.bot.guilds[0]), title_name)
        await save_state()
        await self.announce(f"👑 The title **'{title_name}'** held by **{holder_info['name']}** has automatically expired.")

    async def announce(self, message):
        channel_id = state.get('config', {}).get('announcement_channel')
        if channel_id:
            try:
                channel = await self.bot.fetch_channel(channel_id)
                if channel: await channel.send(message)
            except (discord.NotFound, discord.Forbidden) as e:
                logger.error(f"Could not send to announcement channel {channel_id}: {e}")

    async def notify_guardians(self, guild, title_name, message):
        await self.announce(message)

# --- Flask Web Server ---
app = Flask(__name__)

def get_bot_state():
    return state

@app.route("/")
def dashboard():
    bot_state = get_bot_state()
    titles_data = []
    for title_name in ORDERED_TITLES:
        data = bot_state['titles'].get(title_name, {})
        holder_info = "None"
        if data.get('holder'):
            holder = data['holder']
            holder_info = f"{holder['name']} ({holder['coords']})"
        
        remaining = "N/A"
        if data.get('expiry_date'):
            expiry = datetime.fromisoformat(data['expiry_date'])
            delta = expiry - datetime.utcnow()
            remaining = str(timedelta(seconds=int(delta.total_seconds()))) if delta.total_seconds() > 0 else "Expired"
        
        titles_data.append({
            'name': title_name, 'holder': holder_info, 'expires_in': remaining,
            'icon': TITLES_CATALOG[title_name]['image'], 'buffs': TITLES_CATALOG[title_name]['effects']
        })

    today = datetime.utcnow().date()
    days = [(today + timedelta(days=i)) for i in range(7)]
    hours = [f"{h:02d}:00" for h in range(0, 24, 3)]
    schedules = bot_state.get('schedules', {})
    requestable_titles = REQUESTABLE

    return render_template('dashboard.html', titles=titles_data, days=days, hours=hours, schedules=schedules, today=today.strftime('%Y-%m-%d'), requestable_titles=requestable_titles)

@app.route("/log")
def view_log():
    log_data = []
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                log_data.append(row)
    return render_template('log.html', logs=reversed(log_data))

@app.route("/book-slot", methods=['POST'])
def book_slot():
    title_name = request.form.get('title')
    ign = request.form.get('ign')
    coords = request.form.get('coords')
    date_str = request.form.get('date')
    time_str = request.form.get('time')
    
    if not all([title_name, ign, coords, date_str, time_str]):
        return "Missing form data.", 400

    schedule_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    
    async def do_booking():
        async with state_lock:
            schedules = state['schedules'].setdefault(title_name, {})
            schedule_key = schedule_time.isoformat()
            if schedule_key not in schedules:
                schedules[schedule_key] = f"{ign} ({coords})"
                log_action('schedule_book_web', 0, {'title': title_name, 'time': schedule_key, 'ign': ign})
                await save_state()
    
    bot.loop.call_soon_threadsafe(asyncio.create_task, do_booking())
    return redirect(url_for('dashboard'))

def run_flask_app():
    serve(app, host='0.0.0.0', port=8080)

if not os.path.exists('templates'):
    os.makedirs('templates')
with open('templates/dashboard.html', 'w') as f:
    f.write("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"><title>Title Requestor</title>
    <style>
        body { font-family: sans-serif; background-color: #36393f; color: #dcddde; margin: 2em; }
        .container { max-width: 1400px; margin: auto; }
        .title-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 20px; }
        .title-card { background-color: #2f3136; border-radius: 8px; padding: 15px; border-left: 5px solid #7289da; }
        .form-card, .schedule-card { background-color: #2f3136; padding: 20px; border-radius: 8px; margin-top: 2em; }
        h1, h2 { color: #ffffff; }
        h3 { display: flex; align-items: center; margin-top: 0; }
        h3 img { margin-right: 10px; }
        table { width: 100%; border-collapse: collapse; margin-top: 1em; }
        th, td { border: 1px solid #40444b; padding: 8px; text-align: center; }
        input, select, button { padding: 10px; margin: 5px; border-radius: 5px; border: 1px solid #555; background-color: #40444b; color: #dcddde; }
        button { background-color: #7289da; cursor: pointer; font-weight: bold; }
        a { color: #7289da; }
    </style>
</head>
<body>
    <div class="container">
        <h1>👑 Title Requestor</h1>
        <div class="title-grid">
            {% for title in titles %}
            <div class="title-card">
                <h3><img src="{{ title.icon }}" width="24" height="24">{{ title.name }}</h3>
                <p><em>{{ title.buffs }}</em></p>
                <p><strong>Holder:</strong> {{ title.holder }}</p>
                <p><strong>Expires:</strong> {{ title.expires_in }}</p>
            </div>
            {% endfor %}
        </div>

        <div class="form-card">
            <h2>Claim a Temple Title</h2>
            <form action="/book-slot" method="POST">
                <select name="title" required>
                    {% for title_name in requestable_titles %}
                    <option value="{{ title_name }}">{{ title_name }}</option>
                    {% endfor %}
                </select>
                <input type="text" name="ign" placeholder="In-Game Name" required>
                <input type="text" name="coords" placeholder="X:Y Coordinates" required>
                <input type="date" name="date" value="{{ today }}" required>
                <select name="time" required>{% for hour in hours %}<option value="{{ hour }}">{{ hour }}</option>{% endfor %}</select>
                <button type="submit">Submit</button>
            </form>
        </div>

        <div class="schedule-card">
            <h2>Upcoming Week Schedule</h2>
            <table>
                <thead><tr><th>Time (UTC)</th>{% for day in days %}<th>{{ day.strftime('%A') }}<br>{{ day.strftime('%Y-%m-%d') }}</th>{% endfor %}</tr></thead>
                <tbody>
                    {% for hour in hours %}
                    <tr>
                        <td>{{ hour }}</td>
                        {% for day in days %}
                        <td>
                            {% for title_name, schedule_data in schedules.items() %}
                                {% set slot_time = day.strftime('%Y-%m-%d') + 'T' + hour + ':00' %}
                                {% if schedule_data[slot_time] %}
                                    <div><strong>{{ title_name }}</strong><br>{{ schedule_data[slot_time] }}</div>
                                {% endif %}
                            {% endfor %}
                        </td>
                        {% endfor %}
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        <p style="text-align: center; margin-top: 2em;"><a href="/log">View Full Request Log</a></p>
    </div>
</body>
</html>
""")
with open('templates/log.html', 'w') as f:
    f.write("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"><title>Request Log</title>
    <style>
        body { font-family: sans-serif; background-color: #36393f; color: #dcddde; margin: 2em; }
        .container { max-width: 1200px; margin: auto; }
        h1 { color: #ffffff; }
        table { width: 100%; border-collapse: collapse; margin-top: 1em; }
        th, td { border: 1px solid #40444b; padding: 8px; text-align: left; }
        th { background-color: #2f3136; }
        a { color: #7289da; }
    </style>
</head>
<body>
    <div class="container">
        <h1>📜 Request Log</h1>
        <p><a href="/">Back to Dashboard</a></p>
        <table>
            <thead>
                <tr>
                    <th>Timestamp</th>
                    <th>Title</th>
                    <th>In-Game Name</th>
                    <th>Coordinates</th>
                    <th>Submitted By</th>
                </tr>
            </thead>
            <tbody>
                {% for log in logs %}
                <tr>
                    <td>{{ log.timestamp }}</td>
                    <td>{{ log.title_name }}</td>
                    <td>{{ log.in_game_name }}</td>
                    <td>{{ log.coordinates }}</td>
                    <td>{{ log.discord_user }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
</body>
</html>
""")

@bot.event
async def on_ready():
    await load_state()
    await initialize_titles()
    await bot.add_cog(TitleCog(bot))
    logger.info(f'{bot.user.name} has connected!')
    Thread(target=run_flask_app, daemon=True).start()

if __name__ == "__main__":
    bot_token = os.getenv("DISCORD_TOKEN")
    if not bot_token:
        print("Error: DISCORD_TOKEN environment variable not set.")
    else:
        bot.run(bot_token)
