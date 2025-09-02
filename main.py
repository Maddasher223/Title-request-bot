# main.py — CORE + DISCORD BOT + APP SETUP (routes registered from web_routes.py)

import os
import csv
import json
import logging
import asyncio
import requests
import shutil
from threading import Thread
from datetime import datetime, timedelta, timezone
from web_routes import register_routes

from flask import Flask
from waitress import serve

import discord
from discord.ext import commands, tasks

# ========= UTC helpers & constants =========
UTC = timezone.utc
SHIFT_HOURS = 3  # default; can be overridden by admin setting

def now_utc() -> datetime:
    return datetime.now(UTC)

def parse_iso_utc(s: str) -> datetime:
    """Parse ISO strings into UTC-aware datetimes; handle naive and 'Z' suffixed."""
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        if isinstance(s, str) and s.endswith('Z'):
            dt = datetime.fromisoformat(s[:-1])
        else:
            raise
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt

def iso_slot_key_naive(dt: datetime) -> str:
    """
    Normalized slot key used in schedules:
    'YYYY-MM-DDTHH:MM:SS' naive (no timezone), seconds forced to :00.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt.replace(second=0, microsecond=0).isoformat()

def normalize_iso_slot_string(s: str) -> str:
    """Accept naive or tz-aware strings; return naive 'YYYY-MM-DDTHH:MM:00'."""
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return s
    return iso_slot_key_naive(dt)

def get_shift_hours() -> int:
    """Single source of truth for shift duration (hours)."""
    try:
        return int(state.get('config', {}).get('shift_hours', SHIFT_HOURS))
    except Exception:
        return SHIFT_HOURS

def in_current_slot(slot_start: datetime) -> bool:
    """Is now within [slot_start, slot_start + SHIFT_HOURS)?"""
    if slot_start.tzinfo is None:
        slot_start = slot_start.replace(tzinfo=UTC)
    end = slot_start + timedelta(hours=get_shift_hours())
    n = now_utc()
    return slot_start <= n < end

def title_is_vacant_now(title_name: str) -> bool:
    t = state.get('titles', {}).get(title_name, {})
    holder = t.get('holder')
    if not holder:
        return True
    exp = t.get('expiry_date')
    if exp:
        try:
            if now_utc() >= parse_iso_utc(exp):
                return True
        except Exception:
            pass
    return False

# ========= Static Titles =========
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

WEBHOOK_URL = os.getenv("WEBHOOK_URL")
GUARDIAN_ROLE_ID = os.getenv("GUARDIAN_ROLE_ID")  # string ok; only used in webhook content
TITLE_REQUESTS_CHANNEL_ID = os.getenv("TITLE_REQUESTS_CHANNEL_ID")  # string ok; only used in webhook content
ADMIN_PIN = os.getenv("ADMIN_PIN", "letmein")

# ========= Discord setup =========
intents = discord.Intents.default()
intents.members = True       # must be enabled in Developer Portal
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# ========= Persistence =========
BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

STATE_FILE = os.path.join(DATA_DIR, "titles_state.json")
LOG_FILE   = os.path.join(DATA_DIR, "log.json")
CSV_FILE   = os.path.join(DATA_DIR, "requests.csv")

state: dict = {}
state_lock = asyncio.Lock()

# ========= Logging =========
logging.basicConfig(level=logging.INFO, format='%(asctime)s:%(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger(__name__)

# ========= Helper: state & logs =========
def initialize_state():
    global state
    state = {
        'titles': {},
        'users': {},
        'config': {},
        'schedules': {},
        'sent_reminders': [],
        'activated_slots': {}   # reservations already auto-assigned
    }

async def load_state():
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

async def save_state():
    async with state_lock:
        try:
            with open(STATE_FILE, 'w') as f:
                json.dump(state, f, indent=4)
        except IOError as e:
            logger.error(f"Error saving state file: {e}")

def mark_activated(title_name: str, slot_key: str):
    act = state.setdefault('activated_slots', {}).setdefault(title_name, [])
    if slot_key not in act:
        act.append(slot_key)

def is_activated(title_name: str, slot_key: str) -> bool:
    return slot_key in state.get('activated_slots', {}).get(title_name, [])

def log_action(action, user_id, details):
    entry = {'timestamp': now_utc().isoformat(), 'action': action, 'user_id': user_id, 'details': details}
    try:
        existing = []
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'r') as f:
                try:
                    existing = json.load(f)
                except json.JSONDecodeError:
                    existing = []
        existing.append(entry)
        with open(LOG_FILE, 'w') as f:
            json.dump(existing, f, indent=4)
    except IOError as e:
        logger.error(f"Error writing log: {e}")

def log_to_csv(request_data):
    file_exists = os.path.isfile(CSV_FILE)
    try:
        with open(CSV_FILE, 'a', newline='', encoding='utf-8') as csvfile:
            fieldnames = ['timestamp', 'title_name', 'in_game_name', 'coordinates', 'discord_user']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(request_data)
    except IOError as e:
        logger.error(f"Error writing CSV: {e}")

async def initialize_titles():
    async with state_lock:
        state.setdefault('titles', {})
        for title_name, details in TITLES_CATALOG.items():
            if title_name not in state['titles']:
                state['titles'][title_name] = {
                    'holder': None, 'queue': [], 'claim_date': None, 'expiry_date': None, 'pending_claimant': None
                }
            state['titles'][title_name]['icon'] = details['image']
            state['titles'][title_name]['buffs'] = details['effects']
    await save_state()

# ========= Rebuild schedules from log on restart =========
async def rebuild_schedules_from_log():
    try:
        if not os.path.exists(LOG_FILE):
            return
        with open(LOG_FILE, 'r') as f:
            try:
                entries = json.load(f)
            except json.JSONDecodeError:
                entries = []

        state.setdefault('schedules', {})
        for entry in entries:
            if entry.get('action') not in ('schedule_book', 'schedule_book_web'):
                continue
            d = entry.get('details', {})
            title_name = d.get('title')
            iso_time   = d.get('time')
            ign        = d.get('ign')
            coords     = d.get('coords', '-')
            if not all([title_name, iso_time, ign]):
                continue
            if title_name not in TITLES_CATALOG:
                continue

            norm_key = normalize_iso_slot_string(iso_time)
            state['schedules'].setdefault(title_name, {})[norm_key] = {
                "ign": ign,
                "coords": coords
            }
        await save_state()
    except Exception as e:
        logger.error(f"rebuild_schedules_from_log failed: {e}")

# ========= Icons =========
ICON_FILES = {
    "Guardian of Harmony": "guardian_harmony.png",
    "Guardian of Air": "guardian_air.png",
    "Guardian of Water": "guardian_water.png",
    "Guardian of Earth": "guardian_earth.png",
    "Guardian of Fire": "guardian_fire.png",
    "Architect": "architect.png",
    "General": "general.png",
    "Governor": "governor.png",
    "Prefect": "prefect.png",
}
ICON_SOURCES = {
    "Guardian of Harmony": "https://cdn.discordapp.com/attachments/1409793076955840583/1409793727018569758/guardian_harmony.png",
    "Guardian of Air": "https://cdn.discordapp.com/attachments/1409793076955840583/1409793463817605181/guardian_air.png",
    "Guardian of Water": "https://cdn.discordapp.com/attachments/1409793076955840583/1409793588778369104/guardian_water.png",
    "Guardian of Earth": "https://cdn.discordapp.com/attachments/1409793076955840583/1409794927730229278/guardian_earth.png",
    "Guardian of Fire": "https://cdn.discordapp.com/attachments/1409793076955840583/1409794024948367380/guardian_fire.png",
    "Architect": "https://cdn.discordapp.com/attachments/1409793076955840583/1409796581661605969/architect.png",
    "General": "https://cdn.discordapp.com/attachments/1409793076955840583/1409796597277266000/general.png",
    "Governor": "https://cdn.discordapp.com/attachments/1409793076955840583/1409796936227356723/governor.png",
    "Prefect": "https://cdn.discordapp.com/attachments/1409793076955840583/1409797574763741205/prefect.png",
}
def ensure_icons_cached():
    static_dir = os.path.join(os.path.dirname(__file__), "static", "icons")
    os.makedirs(static_dir, exist_ok=True)
    for title, fname in ICON_FILES.items():
        path = os.path.join(static_dir, fname)
        if not os.path.exists(path):
            url = ICON_SOURCES[title]
            try:
                r = requests.get(url, timeout=15)
                r.raise_for_status()
                with open(path, "wb") as f:
                    f.write(r.content)
            except Exception as e:
                logger.error(f"Icon download failed for {title}: {e}")
    # Ensure app favicon exists
    app_icon = os.path.join(static_dir, "title-requestor.png")
    if not os.path.exists(app_icon):
        src = os.path.join(static_dir, ICON_FILES["Guardian of Harmony"])
        if os.path.exists(src):
            try:
                shutil.copyfile(src, app_icon)
            except Exception as e:
                logger.error(f"Failed to create app icon: {e}")

# ========= Webhook + Log Channel helper =========
def send_webhook_notification(data, reminder=False):
    if not WEBHOOK_URL:
        return
    role_tag = f"<@&{GUARDIAN_ROLE_ID}>" if GUARDIAN_ROLE_ID else ""
    channel_tag = f"<#{TITLE_REQUESTS_CHANNEL_ID}>" if TITLE_REQUESTS_CHANNEL_ID else ""

    if reminder:
        title = f"Reminder: {data.get('title_name','-')} shift starts soon!"
        content = f"{role_tag} {channel_tag}  The {get_shift_hours()}-hour shift for **{data.get('title_name','-')}** by **{data.get('in_game_name','-')}** starts in 5 minutes!"
    else:
        title = "New Title Request"
        content = f"{role_tag} {channel_tag}  A new request was submitted."

    payload = {
        "content": content,
        "allowed_mentions": {"parse": ["roles"]},  # avoid @everyone
        "embeds": [{
            "title": title,
            "color": 5814783,
            "fields": [
                {"name": "Title", "value": data.get('title_name','-'), "inline": True},
                {"name": "In-Game Name", "value": data.get('in_game_name','-'), "inline": True},
                {"name": "Coordinates", "value": data.get('coordinates','-'), "inline": True},
                {"name": "Submitted By", "value": data.get('discord_user','-'), "inline": False}
            ],
            "timestamp": data.get('timestamp')
        }]
    }
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=8)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Webhook send failed: {e}")

def is_guardian_or_admin(ctx):
    # Safe for DMs
    if not getattr(ctx, "guild", None):
        return False
    if ctx.author.guild_permissions.administrator:
        return True
    guardian_role_ids = set(state.get('config', {}).get('guardian_roles', []))
    user_role_ids = {role.id for role in getattr(ctx.author, "roles", [])}
    return bool(guardian_role_ids & user_role_ids)

async def send_to_log_channel(bot_obj, message: str):
    channel_id = state.get('config', {}).get('log_channel')
    if not channel_id:
        return
    try:
        channel = await bot_obj.fetch_channel(int(channel_id))
        if channel:
            await channel.send(message)
    except Exception as e:
        logger.error(f"send_to_log_channel failed: {e}")

# ========= Discord Cog =========
class TitleCog(commands.Cog, name="TitleRequest"):
    def __init__(self, bot):
        self.bot = bot
        self.title_check_loop.start()
        self.housekeeping_loop.start()

    async def force_release_logic(self, title_name, user_id, reason):
        """Internal logic to release a title, callable from tasks or commands."""
        state['titles'][title_name].update({
            'holder': None,
            'claim_date': None,
            'expiry_date': None,
            'pending_claimant': None
        })
        log_action('force_release', user_id, {'title': title_name, 'reason': reason})
        await save_state()
        await self.announce(f"TITLE RELEASED: **'{title_name}'** is now available. Reason: {reason}")
        await send_to_log_channel(self.bot, f"[RELEASE] {title_name} released. Reason: {reason}")

    @tasks.loop(minutes=1)
    async def title_check_loop(self):
        await self.bot.wait_until_ready()
        try:
            now = now_utc()

            # auto-expire
            titles_to_release = []
            for title_name, data in state.get('titles', {}).items():
                if data.get('holder') and data.get('expiry_date'):
                    if now >= parse_iso_utc(data['expiry_date']):
                        titles_to_release.append(title_name)
            for title_name in titles_to_release:
                await self.force_release_logic(title_name, self.bot.user.id, "Title expired.")

            # T-5 reminders
            state.setdefault('sent_reminders', [])
            for title_name, schedule_data in state.get('schedules', {}).items():
                for iso_time, reservation in schedule_data.items():
                    if iso_time in state['sent_reminders']:
                        continue

                    try:
                        shift_time = parse_iso_utc(iso_time)
                    except Exception:
                        try:
                            shift_time = datetime.fromisoformat(iso_time).replace(tzinfo=UTC)
                        except Exception:
                            continue

                    reminder_time = shift_time - timedelta(minutes=5)
                    if reminder_time <= now < shift_time:
                        try:
                            ign = reservation.get('ign') if isinstance(reservation, dict) else reservation
                            coords = reservation.get('coords', '-') if isinstance(reservation, dict) else '-'

                            csv_data = {
                                "timestamp": now_utc().isoformat(), "title_name": title_name,
                                "in_game_name": ign, "coordinates": coords, "discord_user": "Scheduler"
                            }
                            send_webhook_notification(csv_data, reminder=True)
                            state['sent_reminders'].append(iso_time)
                        except Exception as e:
                            logger.error(f"Could not send shift reminder: {e}")

            # Auto-assign reserved slots at slot start if vacant
            for title_name, schedule_data in state.get('schedules', {}).items():
                for iso_time, reservation in list(schedule_data.items()):
                    try:
                        reserver_ign = reservation.get('ign') if isinstance(reservation, dict) else reservation
                        reserver_coords = reservation.get('coords', '-') if isinstance(reservation, dict) else '-'

                        slot_start = parse_iso_utc(iso_time)
                        slot_end = slot_start + timedelta(hours=get_shift_hours())

                        if slot_start <= now < slot_end and not is_activated(title_name, iso_time):
                            if title_is_vacant_now(title_name):
                                state['titles'].setdefault(title_name, {})
                                state['titles'][title_name].update({
                                    'holder': {'name': reserver_ign, 'coords': reserver_coords, 'discord_id': None},
                                    'claim_date': slot_start.isoformat(),
                                    'expiry_date': slot_end.isoformat(),
                                    'pending_claimant': None
                                })
                                mark_activated(title_name, iso_time)
                                await self.announce(f"SCHEDULED HANDOFF: **{title_name}** is now assigned to **{reserver_ign}**.")
                                await send_to_log_channel(self.bot, f"[AUTO-ASSIGN] {title_name} -> {reserver_ign} at {slot_start.strftime('%Y-%m-%d %H:%M')}Z")
                    except Exception as e:
                        logger.error(f"Auto-assign-from-schedule failed for {title_name} {iso_time}: {e}")

            if titles_to_release:
                await save_state()
        except Exception as e:
            logger.exception(f"title_check_loop crashed: {e}")

    @tasks.loop(minutes=60)
    async def housekeeping_loop(self):
        await self.bot.wait_until_ready()
        try:
            # prune old reminders and activations (>48h old)
            cutoff = now_utc() - timedelta(days=2)
            kept = []
            for k in state.get('sent_reminders', []):
                try:
                    if parse_iso_utc(k) >= cutoff:
                        kept.append(k)
                except Exception:
                    pass
            state['sent_reminders'] = kept

            for title, keys in list(state.get('activated_slots', {}).items()):
                new_keys = []
                for k in keys:
                    try:
                        dt = parse_iso_utc(k)
                        if dt + timedelta(hours=get_shift_hours()) >= cutoff:
                            new_keys.append(k)
                    except Exception:
                        pass
                state['activated_slots'][title] = new_keys

            await save_state()
        except Exception as e:
            logger.error(f"housekeeping failed: {e}")

    @commands.command(help="List all titles and their status.")
    async def titles(self, ctx):
        embed = discord.Embed(title="Title Status", color=discord.Color.blue())
        for title_name in ORDERED_TITLES:
            data = state['titles'].get(title_name, {})
            details = TITLES_CATALOG.get(title_name, {})
            status = f"*{details.get('effects', 'No description.')}*\n"
            if data.get('holder'):
                holder = data['holder']
                holder_name = f"{holder.get('name','?')} ({holder.get('coords', '-')})"
                try:
                    expiry = parse_iso_utc(data['expiry_date'])
                    remaining = expiry - now_utc()
                    status += f"**Held by:** {holder_name}\n*Expires in: {str(timedelta(seconds=int(remaining.total_seconds())))}*"
                except Exception:
                    status += f"**Held by:** {holder_name}\n*Invalid expiry date.*"
            else:
                status += "**Status:** Available"
            embed.add_field(name=f"{title_name}", value=status, inline=False)
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

        now = now_utc()
        expiry_date = now + timedelta(hours=get_shift_hours())
        state['titles'][title_name].update({
            'holder': {'name': ign, 'coords': '-', 'discord_id': ctx.author.id},
            'claim_date': now.isoformat(),
            'expiry_date': expiry_date.isoformat(),
            'pending_claimant': None
        })
        log_action('assign', ctx.author.id, {'title': title_name, 'ign': ign})
        await save_state()
        await self.announce(f"SHIFT CHANGE: **{ign}** has been granted **'{title_name}'**.")
        await send_to_log_channel(self.bot, f"[ASSIGN] {ctx.author.display_name} assigned {title_name} -> {ign}")

    @commands.command(help="Set the announcement channel. Usage: !set_announce <#channel>")
    @commands.has_permissions(administrator=True)
    async def set_announce(self, ctx, channel: discord.TextChannel):
        state.setdefault('config', {})['announcement_channel'] = int(channel.id)
        await save_state()
        await ctx.send(f"Announcement channel set to {channel.mention}.")

    @commands.command(help="Set the log channel. Usage: !set_log <#channel>")
    @commands.has_permissions(administrator=True)
    async def set_log(self, ctx, channel: discord.TextChannel):
        state.setdefault('config', {})['log_channel'] = int(channel.id)
        await save_state()
        await ctx.send(f"Log channel set to {channel.mention}.")

    async def announce(self, message):
        channel_id = state.get('config', {}).get('announcement_channel')
        if channel_id:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
                if channel:
                    await channel.send(message)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                logger.error(f"Could not send to announcement channel {channel_id}: {e}")

# ========= Flask App =========
ensure_icons_cached()
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret")

def compute_next_reservation_for_title(title_name: str):
    schedules = state.get('schedules', {}).get(title_name, {})
    if not schedules:
        return (None, None)
    future = []
    for k, v in schedules.items():
        try:
            dt = parse_iso_utc(k)
            if dt >= now_utc():
                ign = v.get('ign') if isinstance(v, dict) else v
                future.append((dt, k, ign))
        except Exception:
            continue
    if not future:
        return (None, None)
    future.sort(key=lambda x: x[0])
    _, k, ign = future[0]
    return (k, ign)

def get_all_upcoming_reservations():
    items = []
    for title_name, sched in state.get('schedules', {}).items():
        for slot_key, reservation in sched.items():
            try:
                dt = parse_iso_utc(slot_key)
            except Exception:
                continue
            if dt >= now_utc():
                ign = reservation.get('ign') if isinstance(reservation, dict) else reservation
                coords = reservation.get('coords', '-') if isinstance(reservation, dict) else '-'
                items.append({
                    "title": title_name, "slot_iso": slot_key, "slot_dt": dt,
                    "ign": ign, "coords": coords
                })
    items.sort(key=lambda x: x["slot_dt"])
    return items

def set_shift_hours(new_hours: int):
    global SHIFT_HOURS
    SHIFT_HOURS = int(new_hours)
    state.setdefault('config', {})['shift_hours'] = SHIFT_HOURS
    return SHIFT_HOURS

def run_flask_app():
    port = int(os.getenv("PORT", "10000"))
    serve(app, host='0.0.0.0', port=port)

# ========= Templates (auto-create if missing) =========
os.makedirs('templates', exist_ok=True)

def _write_if_missing(path, content):
    if not os.path.exists(path):
        with open(path, 'w') as f:
            f.write(content)

_write_if_missing('templates/dashboard.html', """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Title Requestor</title>
    <link rel="icon" type="image/png" href="{{ url_for('static', filename='icons/title-requestor.png') }}">
    <style>
        body { font-family: sans-serif; background-color: #36393f; color: #dcddde; margin: 2em; }
        .container { max-width: 1400px; margin: auto; }
        .title-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 20px; }
        .title-card { background-color: #2f3136; border-radius: 8px; padding: 15px; border-left: 5px solid #7289da; }
        .badge { display:inline-block; padding:2px 6px; border-radius:4px; background:#3b82f6; color:#fff; font-size:11px; margin-left:6px;}
        .form-card, .schedule-card { background-color: #2f3136; padding: 20px; border-radius: 8px; margin-top: 2em; }
        h1, h2 { color: #ffffff; }
        h3 { display: flex; align-items: center; margin-top: 0; }
        h3 img { margin-right: 10px; }
        table { width: 100%; border-collapse: collapse; margin-top: 1em; }
        th, td { border: 1px solid #40444b; padding: 8px; text-align: center; vertical-align: top; }
        input, select, button { padding: 10px; margin: 5px; border-radius: 5px; border: 1px solid #555; background-color: #40444b; color: #dcddde; }
        button { background-color: #7289da; cursor: pointer; font-weight: bold; }
        a { color: #7289da; }
        .small { font-size: 12px; }
        .cell-booking { background:#23262a; border:1px solid #3a3f44; padding:6px; border-radius:6px; margin-bottom:8px;}
        .utc-note { font-size:12px; opacity:0.8; margin-top:4px;}
        .footer-links { text-align:center; margin-top:2em;}
    </style>
</head>
<body>
    <div class="container">
        <h1>
            <img src="{{ url_for('static', filename='icons/title-requestor.png') }}" width="32" height="32" style="vertical-align: middle; margin-right: 8px;">
            Title Requestor
        </h1>

        <div class="title-grid">
            {% for title in titles %}
            <div class="title-card">
                <h3><img src="{{ title.icon }}" width="24" height="24">{{ title.name }}</h3>
                <p><em>{{ title.buffs }}</em></p>
                <p><strong>Holder:</strong> {{ title.holder }}</p>
                <p><strong>Expires:</strong> {{ title.expires_in }}</p>
                <p><strong>Next reserved:</strong> {{ title.next_reserved if title.next_reserved else "—" }}</p>
            </div>
            {% endfor %}
        </div>

        <div class="form-card">
            <h2>Reserve a {{ (config.shift_hours or 3) if config else 3 }}-hour Slot</h2>
            <div class="utc-note">All times are in <strong>UTC</strong>. The grid below updates immediately when reserved.</div>
            <form action="/book-slot" method="POST">
                <select name="title" required>
                    {% for t in requestable_titles %}
                    <option value="{{ t }}">{{ t }}</option>
                    {% endfor %}
                </select>
                <input type="text" name="ign" placeholder="In-Game Name" required>
                <input type="text" name="coords" placeholder="X:Y Coordinates" required>
                <input type="date" name="date" value="{{ today }}" required>
                <select name="time" required>
                    {% for hour in hours %}
                    <option value="{{ hour }}">{{ hour }}</option>
                    {% endfor %}
                </select>
                <button type="submit">Submit</button>
            </form>
        </div>

        <div class="schedule-card">
            <h2>Upcoming Week Schedule</h2>
            {% with messages = get_flashed_messages() %}
              {% if messages %}
                <ul>
                  {% for m in messages %}
                    <li class="small">{{ m }}</li>
                  {% endfor %}
                </ul>
              {% endif %}
            {% endwith %}
            <div class="utc-note">Cells show <em>reservations</em> immediately (Reserved = taken) and assignments when active.</div>
            <table>
                <thead>
                    <tr>
                        <th>Time (UTC)</th>
                        {% for day in days %}
                        <th>{{ day.strftime('%A') }}<br>{{ day.strftime('%Y-%m-%d') }}</th>
                        {% endfor %}
                    </tr>
                </thead>
                <tbody>
                    {% for hour in hours %}
                    <tr>
                        <td>{{ hour }}</td>
                        {% for day in days %}
                        <td>
                            {% set slot_time = day.strftime('%Y-%m-%d') ~ 'T' ~ hour ~ ':00' %}
                            {% for title_name, schedule_data in schedules.items() %}
                                {% set reservation = schedule_data.get(slot_time) %}
                                {% if reservation %}
                                    <div class="cell-booking">
                                        <strong>{{ title_name }}</strong>
                                        <span class="badge">Reserved</span><br>
                                        {{ reservation.ign if reservation is mapping else reservation }}
                                        {% if reservation is mapping and reservation.coords %}
                                            <br><span class="small">({{ reservation.coords }})</span>
                                        {% endif %}
                                    </div>
                                {% endif %}
                            {% endfor %}
                        </td>
                        {% endfor %}
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>

        <div class="footer-links">
          <a href="{{ url_for('view_log') }}">View Full Request Log</a> ·
          <a href="{{ url_for('admin_login_form') }}">Admin Dashboard</a>
        </div>
    </div>
</body>
</html>
""")

_write_if_missing('templates/log.html', """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Request Log</title>
    <link rel="icon" type="image/png" href="{{ url_for('static', filename='icons/title-requestor.png') }}">
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
        <h1> Request Log</h1>
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

_write_if_missing('templates/admin_login.html', """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Admin Login · Title Requestor</title>
  <link rel="icon" type="image/png" href="{{ url_for('static', filename='icons/title-requestor.png') }}">
  <style>
    body { font-family: sans-serif; background:#121417; color:#e7e9ea; margin: 2rem; }
    .box { max-width: 420px; margin: 8vh auto; background:#1b1f24; border:1px solid #2a2f36; border-radius:10px; padding:16px; }
    input, button { width: 100%; padding:10px; border-radius:6px; border:1px solid #2a2f36; background:#0f1114; color:#e7e9ea; }
    button { margin-top:10px; background:#2f6feb; border:none; cursor:pointer; font-weight:600; }
    .muted { opacity: .8; font-size: 12px; margin-top: 10px; }
    a { color:#8ab4f8; }
  </style>
</head>
<body>
  <div class="box">
    <h2>Admin Login</h2>
    {% with messages = get_flashed_messages() %}
      {% if messages %}
        <ul>
          {% for m in messages %}
            <li class="muted">{{ m }}</li>
          {% endfor %}
        </ul>
      {% endif %}
    {% endwith %}

    <form method="post" action="{{ url_for('admin_login_submit') }}">
      <input type="password" name="pin" placeholder="Enter Admin PIN" required>
      <button type="submit">Sign In</button>
    </form>

    <p class="muted" style="margin-top: 12px;">
      <a href="{{ url_for('dashboard') }}"> Back to Dashboard</a>
    </p>
  </div>
</body>
</html>
""")

_write_if_missing('templates/admin.html', """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Admin · Title Requestor</title>
  <link rel="icon" type="image/png" href="{{ url_for('static', filename='icons/title-requestor.png') }}">
  <style>
    body{font-family:sans-serif;background:#121417;color:#e7e9ea;margin:2rem}
    table{width:100%;border-collapse:collapse;margin:1rem 0}
    th,td{border:1px solid #2a2f36;padding:8px;text-align:left}
    input,select,button{padding:8px;border-radius:6px;border:1px solid #2a2f36;background:#0f1114;color:#e7e9ea}
    button{background:#2f6feb;border:none;cursor:pointer;font-weight:600}
    .row{display:flex;gap:12px;flex-wrap:wrap}
  </style>
</head>
<body>
  <h2>Active Titles</h2>
  <table>
    <thead><tr><th>Title</th><th>Holder</th><th>Coords</th><th>Expires</th></tr></thead>
    <tbody>{% for t in active_titles %}<tr><td>{{t.title}}</td><td>{{t.holder}}</td><td>{{t.coords}}</td><td>{{t.expires}}</td></tr>{% endfor %}</tbody>
  </table>

  <h2>Upcoming Reservations</h2>
  <table>
    <thead><tr><th>Title</th><th>When (UTC)</th><th>IGN</th><th>Coords</th><th>Approved</th></tr></thead>
    <tbody>{% for u in upcoming %}<tr><td>{{u.title}}</td><td>{{u.slot_iso}}</td><td>{{u.ign}}</td><td>{{u.coords}}</td><td>{{'Yes' if u.approved else 'No'}}</td></tr>{% endfor %}</tbody>
  </table>

  <h2>Settings</h2>
  <form method="post" action="{{ url_for('admin_settings') }}" class="row">
    <label>Announcement Channel ID <input name="announce_channel" value="{{settings.announcement_channel or ''}}"></label>
    <label>Log Channel ID <input name="log_channel" value="{{settings.log_channel or ''}}"></label>
    <label>Shift Hours <input name="shift_hours" value="{{settings.shift_hours}}"></label>
    <button type="submit">Save</button>
  </form>

  <h2>Manual Assign</h2>
  <form method="post" action="{{ url_for('admin_manual_assign') }}" class="row">
    <label>Title
      <select name="title">{% for t in all_titles %}<option value="{{t}}">{{t}}</option>{% endfor %}</select>
    </label>
    <label>IGN <input name="ign" required></label>
    <label>Coords <input name="coords"></label>
    <button type="submit">Assign</button>
  </form>

  <h2>Approve / Cancel / Move Reservation</h2>
  <form method="post" action="{{ url_for('admin_approve') }}" class="row">
    <label>Title <input name="title" required></label>
    <label>Slot ISO <input name="slot" required placeholder="YYYY-MM-DDTHH:MM:00"></label>
    <button type="submit">Approve</button>
  </form>
  <form method="post" action="{{ url_for('admin_cancel') }}" class="row">
    <label>Title <input name="title" required></label>
    <label>Slot ISO <input name="slot" required></label>
    <button type="submit">Cancel</button>
  </form>
  <form method="post" action="{{ url_for('admin_move') }}" class="row">
    <label>Title <input name="title" required></label>
    <label>Slot ISO <input name="slot" required></label>
    <label>New Title <input name="new_title" required></label>
    <label>New Slot ISO <input name="new_slot" required></label>
    <button type="submit">Move</button>
  </form>

  <p><a href="{{ url_for('dashboard') }}">Back to dashboard</a></p>
</body>
</html>
""")

# ========= Register routes from web_routes.py =========
register_routes(
    app=app,
    deps=dict(
        ORDERED_TITLES=ORDERED_TITLES, TITLES_CATALOG=TITLES_CATALOG,
        ICON_FILES=ICON_FILES, REQUESTABLE=REQUESTABLE, ADMIN_PIN=ADMIN_PIN,
        state=state, save_state=save_state, log_action=log_action, log_to_csv=log_to_csv,
        send_webhook_notification=send_webhook_notification, send_to_log_channel=send_to_log_channel,
        parse_iso_utc=parse_iso_utc, now_utc=now_utc, iso_slot_key_naive=iso_slot_key_naive,
        in_current_slot=in_current_slot, title_is_vacant_now=title_is_vacant_now,
        compute_next_reservation_for_title=compute_next_reservation_for_title,
        get_all_upcoming_reservations=get_all_upcoming_reservations,
        set_shift_hours=set_shift_hours,
        get_shift_hours=get_shift_hours,   # NEW: share getter with routes
        bot=bot,
    )
)

# ========= Discord lifecycle =========
@bot.event
async def on_ready():
    await load_state()
    await initialize_titles()
    await rebuild_schedules_from_log()

    await bot.add_cog(TitleCog(bot))
    logger.info(f'{bot.user.name} has connected!')
    Thread(target=run_flask_app, daemon=True).start()

# ========= Entry =========
if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("Error: DISCORD_TOKEN environment variable not set.")
    else:
        bot.run(token)