# main.py - Complete Code

import discord
from discord.ext import commands, tasks
import json
import os
import logging
from datetime import datetime, timedelta
import asyncio
from threading import Thread
from flask import Flask, render_template, request, redirect, url_for
import csv
from waitress import serve

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
async def load_state():
    """Loads the bot's state from a JSON file."""
    global state
    async with state_lock:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    state = json.load(f)
                    if 'config' in state and 'reminders' in state['config']:
                        state['config']['reminders'] = {int(k): v for k, v in state['config']['reminders'].items()}
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Error loading state file: {e}")
                initialize_state()
        else:
            initialize_state()

def initialize_state():
    """Initializes a default state structure."""
    global state
    state = {
        'titles': {},
        'users': {},
        'config': {
            'min_hold_duration_hours': 24,
            'announcement_channel': None,
            'guardian_roles': [],
            'guardian_titles': {},
            'reminders': {24: "24 hours remaining on your title.", 1: "1 hour remaining on your title."}
        },
        'schedules': {}
    }

async def save_state():
    """Saves the bot's state to a JSON file."""
    async with state_lock:
        try:
            temp_state = state.copy()
            if 'config' in temp_state and 'reminders' in temp_state['config']:
                 temp_state['config']['reminders'] = {str(k): v for k, v in temp_state['config']['reminders'].items()}
            with open(STATE_FILE, 'w') as f:
                json.dump(temp_state, f, indent=4)
        except IOError as e:
            logger.error(f"Error saving state file: {e}")

def log_action(action, user_id, details):
    """Logs an action to the log file."""
    log_entry = {'timestamp': datetime.utcnow().isoformat(), 'action': action, 'user_id': user_id, 'details': details}
    try:
        logs = []
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'r') as f:
                try:
                    logs = json.load(f)
                except json.JSONDecodeError:
                    pass
        logs.append(log_entry)
        with open(LOG_FILE, 'w') as f:
            json.dump(logs, f, indent=4)
    except IOError as e:
        logger.error(f"Error writing to log file: {e}")

def log_to_csv(request_data):
    """Logs a title request to a CSV file."""
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

def is_guardian_or_admin(ctx):
    """Check if the user is a Guardian or an Admin."""
    if ctx.author.guild_permissions.administrator:
        return True
    guardian_role_ids = state.get('config', {}).get('guardian_roles', [])
    user_role_ids = {role.id for role in ctx.author.roles}
    return any(role_id in user_role_ids for role_id in guardian_role_ids)

def is_title_guardian(user, title_name):
    """Check if a user is a designated guardian for a specific title."""
    if user.guild_permissions.administrator:
        return True
    guardian_titles = state.get('config', {}).get('guardian_titles', {})
    for role in user.roles:
        if str(role.id) in guardian_titles and title_name in guardian_titles[str(role.id)]:
            return True
    return False

# --- Main Bot Cog ---
class TitleCog(commands.Cog, name="TitleRequest"):
    def __init__(self, bot):
        self.bot = bot
        self.title_check_loop.start()

    def cog_unload(self):
        self.title_check_loop.cancel()

    @tasks.loop(minutes=1)
    async def title_check_loop(self):
        await self.bot.wait_until_ready()
        now = datetime.utcnow()
        titles_to_release = []
        for title_name, data in state.get('titles', {}).items():
            if data.get('holder') and data.get('expiry_date'):
                expiry = datetime.fromisoformat(data['expiry_date'])
                if now >= expiry:
                    titles_to_release.append(title_name)
        
        for title_name in titles_to_release:
            holder_info = state['titles'][title_name]['holder']
            logger.info(f"Title '{title_name}' expired for {holder_info['name']}. Forcing release.")
            await self.force_release_logic(title_name, self.bot.user.id, "Title expired.")
        if titles_to_release:
            await save_state()

    # --- Member Commands ---
    @commands.command(help="Claim a title. Usage: !claim <Title Name> | <In-Game Name> | <X:Y Coords>")
    async def claim(self, ctx, *, args: str):
        try:
            title_name, ign, coords = [arg.strip() for arg in args.split('|')]
        except ValueError:
            await ctx.send("Invalid format. Use `!claim <Title Name> | <In-Game Name> | <X:Y Coords>`")
            return

        if title_name not in state['titles']:
            await ctx.send(f"Title '{title_name}' does not exist.")
            return

        if any(t.get('holder') and t['holder']['name'] == ign for t in state['titles'].values()):
            await ctx.send("You already hold a title. Please have it released before claiming another.")
            return

        title = state['titles'][title_name]
        claimant_data = {'name': ign, 'coords': coords, 'discord_id': ctx.author.id}

        if not title['holder']:
            title['pending_claimant'] = claimant_data
            log_action('claim_request', ctx.author.id, {'title': title_name, 'ign': ign, 'coords': coords})
            csv_data = {
                'timestamp': datetime.utcnow().isoformat(), 'title_name': title_name,
                'in_game_name': ign, 'coordinates': coords,
                'discord_user': f"{ctx.author.name} ({ctx.author.id})"
            }
            log_to_csv(csv_data)
            guardian_message = (f"👑 **Title Request:** Player **{ign}** ({coords}) has requested to claim **'{title_name}'**. "
                                f"A guardian must approve with `!assign {title_name} | {ign}`.")
            await self.notify_guardians(ctx.guild, title_name, guardian_message)
            await ctx.send(f"Your request for '{title_name}' has been submitted for player **{ign}**. A guardian must approve it.")
        else:
            if any(q['name'] == ign for q in title['queue']):
                await ctx.send(f"Player **{ign}** is already in the queue for '{title_name}'.")
            else:
                title['queue'].append(claimant_data)
                log_action('queue_join', ctx.author.id, {'title': title_name, 'ign': ign})
                await ctx.send(f"Player **{ign}** has been added to the queue for '{title_name}'. Position: {len(title['queue'])}")
        await save_state()

    @commands.command(help="Join the queue. Usage: !queue <Title Name> | <In-Game Name> | <X:Y Coords>")
    async def queue(self, ctx, *, args: str):
        await self.claim(ctx, args=args)

    @commands.command(help="List all titles and their status.")
    async def titles(self, ctx):
        if not state['titles']:
            await ctx.send("No titles have been configured.")
            return
        embed = discord.Embed(title="📜 Title Status", color=discord.Color.blue())
        for title_name, data in sorted(state['titles'].items()):
            status = ""
            if data.get('holder'):
                holder = data['holder']
                holder_name = f"{holder['name']} ({holder['coords']})"
                expiry = datetime.fromisoformat(data['expiry_date'])
                remaining = expiry - datetime.utcnow()
                status = f"**Held by:** {holder_name}\n*Expires in: {str(timedelta(seconds=int(remaining.total_seconds())))}*"
                if data['queue']:
                    status += f"\n*Queue: {len(data['queue'])}*"
            elif data.get('pending_claimant'):
                claimant = data['pending_claimant']
                status = f"**Pending Approval for:** {claimant['name']} ({claimant['coords']})"
            else:
                status = "**Status:** Available"
            details = []
            if data.get('icon'): details.append(f"Icon: {data.get('icon')}")
            if data.get('buffs'): details.append(f"Buffs: {data.get('buffs')}")
            if details:
                status += "\n" + " | ".join(details)
            embed.add_field(name=f"👑 {title_name}", value=status, inline=False)
        await ctx.send(embed=embed)

    # --- Guardian & Admin Commands ---
    @commands.command(help="Release a title. Usage: !release <Title Name>")
    @commands.check(is_guardian_or_admin)
    async def release(self, ctx, *, title_name: str):
        title_name = title_name.strip()
        if title_name not in state['titles'] or not state['titles'][title_name].get('holder'):
            await ctx.send("Title not found or is not currently held.")
            return
        holder_name = state['titles'][title_name]['holder']['name']
        await self.release_logic(ctx, title_name, f"Released by {ctx.author.display_name}")
        await ctx.send(f"You have released '{title_name}' from player **{holder_name}**.")
        await self.announce(f"SHIFT CHANGE: The title **'{title_name}'** has been manually released from **{holder_name}**.")

    @commands.command(help="Assign a title. Usage: !assign <Title Name> | <In-Game Name>")
    @commands.check(is_guardian_or_admin)
    async def assign(self, ctx, *, args: str):
        try:
            title_name, ign = [arg.strip() for arg in args.split('|')]
        except ValueError:
            await ctx.send("Invalid format. Use `!assign <Title Name> | <In-Game Name>`")
            return
        if not is_title_guardian(ctx.author, title_name):
            await ctx.send(f"You are not a designated guardian for '{title_name}'.")
            return
        if title_name not in state['titles']:
            await ctx.send(f"Title '{title_name}' does not exist.")
            return
        title = state['titles'][title_name]
        pending_claimant = title.get('pending_claimant')
        if not pending_claimant or pending_claimant['name'] != ign:
            await ctx.send(f"**{ign}** is not the pending claimant for this title.")
            return
        if any(t.get('holder') and t['holder']['name'] == ign for t in state['titles'].values()):
            await ctx.send(f"Player **{ign}** already holds a title.")
            return
        min_hold_hours = state['config']['min_hold_duration_hours']
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

    @commands.command(help="Extend a title's hold. Usage: !snooze <hours> <Title Name>")
    @commands.check(is_guardian_or_admin)
    async def snooze(self, ctx, hours: int, *, title_name: str):
        title_name = title_name.strip()
        if not is_title_guardian(ctx.author, title_name) or title_name not in state['titles']:
            await ctx.send("You are not a guardian for this title or it does not exist.")
            return
        title = state['titles'][title_name]
        if not title.get('holder'):
            await ctx.send(f"'{title_name}' is not currently held.")
            return
        expiry = datetime.fromisoformat(title['expiry_date'])
        new_expiry = expiry + timedelta(hours=hours)
        title['expiry_date'] = new_expiry.isoformat()
        log_action('snooze', ctx.author.id, {'title': title_name, 'ign': title['holder']['name'], 'hours': hours})
        await save_state()
        holder_info = title['holder']
        await ctx.send(f"Extended hold for **{holder_info['name']}** on '{title_name}' by {hours} hours.")
        await self.announce(f"⏰ SHIFT CHANGE: The hold on **'{title_name}'** for **{holder_info['name']}** has been extended by {hours} hours.")

    # --- Admin-Only Commands ---
    @commands.command(help="Import titles. Usage: !import_titles <Title One, Title Two, ...>")
    @commands.has_permissions(administrator=True)
    async def import_titles(self, ctx, *, titles_csv: str):
        titles = [t.strip() for t in titles_csv.split(',')]
        added_count = 0
        for title in titles:
            if title and title not in state['titles']:
                state['titles'][title] = {'holder': None, 'queue': []}
                added_count += 1
        if added_count > 0:
            log_action('import_titles', ctx.author.id, {'count': added_count, 'titles': titles})
            await save_state()
            await ctx.send(f"Successfully added {added_count} new titles.")
        else:
            await ctx.send("No new titles were added.")

    @commands.command(help="Set title details. Usage: !set_title_details <Title Name> | icon=<url> | buffs=<text>")
    @commands.has_permissions(administrator=True)
    async def set_title_details(self, ctx, *, full_argument: str):
        parts = [p.strip() for p in full_argument.split('|')]
        title_name = parts[0]
        if title_name not in state['titles']:
            await ctx.send(f"Title '{title_name}' not found.")
            return
        details_updated = []
        for part in parts[1:]:
            if '=' in part:
                key, value = part.split('=', 1)
                key = key.strip().lower()
                if key in ['icon', 'buffs']:
                    state['titles'][title_name][key] = value.strip()
                    details_updated.append(key)
        if details_updated:
            await save_state()
            await ctx.send(f"Updated {', '.join(details_updated)} for '{title_name}'.")
        else:
            await ctx.send("No valid details provided.")

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

    # --- Helper Methods ---
    async def release_logic(self, ctx, title_name, reason):
        holder_info = state['titles'][title_name]['holder']
        log_action('release', ctx.author.id, {'title': title_name, 'ign': holder_info['name'], 'reason': reason})
        state['titles'][title_name].update({'holder': None, 'claim_date': None, 'expiry_date': None})
        await self.process_queue(ctx, title_name)
        await save_state()

    async def process_queue(self, ctx, title_name):
        queue = state['titles'][title_name]['queue']
        if not queue:
            await self.announce(f"👑 The title **'{title_name}'** is now available!")
            return
        next_in_line = None
        for user_data in list(queue):
            if any(t.get('holder') and t['holder']['name'] == user_data['name'] for t in state['titles'].values()):
                queue.pop(0)
            else:
                next_in_line = queue.pop(0)
                break
        if next_in_line:
            state['titles'][title_name]['pending_claimant'] = next_in_line
            user_mention = f"<@{next_in_line['discord_id']}>"
            guardian_message = (f"👑 **Next in Queue:** {user_mention}, it's **{next_in_line['name']}'s** turn for **'{title_name}'**! "
                                f"A guardian must use `!assign {title_name} | {next_in_line['name']}` to grant it.")
            await self.notify_guardians(ctx.guild, title_name, guardian_message)
        else:
            await self.announce(f"👑 The title **'{title_name}'** is now available!")

    async def force_release_logic(self, title_name, actor_id, reason):
        if title_name not in state['titles'] or not state['titles'][title_name].get('holder'):
            return
        holder_info = state['titles'][title_name]['holder']
        log_action('force_release', actor_id, {'title': title_name, 'ign': holder_info['name'], 'reason': reason})
        state['titles'][title_name].update({'holder': None, 'claim_date': None, 'expiry_date': None})
        class FakeContext:
            def __init__(self, guild, author_id):
                self.guild = guild
                self.author = type('Author', (), {'id': author_id})()
        await self.process_queue(FakeContext(self.bot.guilds[0], actor_id), title_name)
        await save_state()
        await self.announce(f"👑 The title **'{title_name}'** held by **{holder_info['name']}** has been automatically released (expired).")

    async def announce(self, message):
        channel_id = state.get('config', {}).get('announcement_channel')
        if channel_id:
            try:
                await self.bot.get_channel(channel_id).send(message)
            except (discord.NotFound, discord.Forbidden) as e:
                logger.error(f"Could not send to announcement channel {channel_id}: {e}")

    async def notify_guardians(self, guild, title_name, message):
        guardian_roles = state.get('config', {}).get('guardian_roles', [])
        guardian_titles = state.get('config', {}).get('guardian_titles', {})
        roles_to_ping = {int(rid) for rid, titles in guardian_titles.items() if title_name in titles}
        if not roles_to_ping:
            roles_to_ping.update(guardian_roles)
        if not roles_to_ping:
            await self.announce(f"⚠️ **Guardian Alert:** No guardians configured for **'{title_name}'**.")
            return
        ping_msg = ' '.join(f"<@&{rid}>" for rid in roles_to_ping)
        await self.announce(f"{ping_msg}\n{message}")

# --- Flask Web Server ---
app = Flask(__name__)

@app.route("/")
def dashboard():
    bot_state = get_bot_state()
    titles_data = []
    for name, data in sorted(bot_state.get('titles', {}).items()):
        holder_info = "None"
        if data.get('holder'):
            holder = data['holder']
            holder_info = f"{holder['name']} ({holder['coords']})"
        pending_info = "None"
        if data.get('pending_claimant'):
            pending = data['pending_claimant']
            pending_info = f"{pending['name']} ({pending['coords']})"
        remaining = "N/A"
        if data.get('expiry_date'):
            expiry = datetime.fromisoformat(data['expiry_date'])
            delta = expiry - datetime.utcnow()
            remaining = str(timedelta(seconds=int(delta.total_seconds()))) if delta.total_seconds() > 0 else "Expired"
        queue_info = [f"{q['name']} ({q['coords']})" for q in data.get('queue', [])]
        titles_data.append({
            'name': name, 'holder': holder_info, 'pending': pending_info,
            'expires_in': remaining, 'queue': queue_info,
            'icon': data.get('icon'), 'buffs': data.get('buffs')
        })
    return render_template('dashboard.html', titles=titles_data)

@app.route("/scheduler")
def scheduler():
    bot_state = get_bot_state()
    today = datetime.utcnow().date()
    days = [(today + timedelta(days=i)) for i in range(7)]
    hours = [f"{h:02d}:00" for h in range(0, 24, 3)]
    title_names = sorted(bot_state.get('titles', {}).keys())
    schedules = bot_state.get('schedules', {})
    return render_template('scheduler.html', days=days, hours=hours, titles=title_names, schedules=schedules)

def run_flask_app():
    serve(app, host='0.0.0.0', port=8080)

if not os.path.exists('templates'):
    os.makedirs('templates')
with open('templates/dashboard.html', 'w') as f:
    f.write("""
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>TitleRequest Dashboard</title></head>
<body>
    <h1>👑 TitleRequest Dashboard</h1>
    <a href="/scheduler">View Scheduler</a>
    {% for title in titles %}
        <div>
            <h3>{{ title.name }}</h3>
            <p><strong>Holder:</strong> {{ title.holder }}</p>
            <p><strong>Expires In:</strong> {{ title.expires_in }}</p>
            {% if title.queue %}<h4>Queue:</h4><ul>{% for user in title.queue %}<li>{{ user }}</li>{% endfor %}</ul>{% endif %}
        </div>
    {% endfor %}
    <h2>Request a Title</h2>
    <form action="/request-title" method="POST">
        <select name="title_name" required>{% for title in titles %}<option value="{{ title.name }}">{{ title.name }}</option>{% endfor %}</select>
        <input type="text" name="ign" placeholder="In-Game Name" required>
        <input type="text" name="coords" placeholder="X:Y Coordinates" required>
        <button type="submit">Submit</button>
    </form>
</body>
</html>
""")
with open('templates/scheduler.html', 'w') as f:
    f.write("""
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Title Scheduler</title></head>
<body>
    <h1>🗓️ Title Scheduler</h1>
    <a href="/">Back to Dashboard</a>
    <h2>Book a Slot</h2>
    <form action="/book-slot" method="POST">
        <select name="title" required>{% for title in titles %}<option value="{{ title }}">{{ title }}</option>{% endfor %}</select>
        <input type="date" name="date" required>
        <select name="time" required>{% for hour in hours %}<option value="{{ hour }}">{{ hour }}</option>{% endfor %}</select>
        <input type="text" name="ign" placeholder="In-Game Name" required>
        <input type="text" name="coords" placeholder="X:Y Coordinates" required>
        <button type="submit">Book</button>
    </form>
    <h2>Upcoming Schedule</h2>
    <table>
        <thead><tr><th>Time (UTC)</th>{% for day in days %}<th>{{ day.strftime('%A %Y-%m-%d') }}</th>{% endfor %}</tr></thead>
        <tbody>
            {% for hour in hours %}
            <tr>
                <td>{{ hour }}</td>
                {% for day in days %}
                <td>
                    {% for title_name, schedule_data in schedules.items() %}
                        {% set slot_time = day.strftime('%Y-%m-%d') + 'T' + hour + ':00' %}
                        {% if schedule_data[slot_time] %}<div><strong>{{ title_name }}</strong><br>{{ schedule_data[slot_time] }}</div>{% endif %}
                    {% endfor %}
                </td>
                {% endfor %}
            </tr>
            {% endfor %}
        </tbody>
    </table>
</body>
</html>
""")

@bot.event
async def on_ready():
    await load_state()
    await bot.add_cog(TitleCog(bot))
    logger.info(f'{bot.user.name} has connected!')
    Thread(target=run_flask_app, daemon=True).start()

if __name__ == "__main__":
    bot_token = os.getenv("DISCORD_TOKEN")
    if not bot_token:
        print("Error: DISCORD_TOKEN environment variable not set.")
    else:
        bot.run(bot_token)
