# main.py — CORE + DISCORD BOT + APP SETUP (routes registered from web_routes.py)

import os
import csv
import json
import logging
import asyncio
import requests
import shutil
from threading import Thread, Event
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from web_routes import register_routes
from flask import Flask, jsonify
from waitress import serve

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ========= UTC helpers & constants =========
UTC = timezone.utc
SHIFT_HOURS = 3  # default; can be overridden by admin setting

def now_utc() -> datetime:
    return datetime.now(UTC)

def parse_iso_utc(s: str) -> datetime:
    """Parse ISO strings into UTC-aware datetimes; handle naive and 'Z' suffix."""
    if isinstance(s, str) and s.endswith('Z'):
        s = s[:-1]
    dt = datetime.fromisoformat(s)
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
        dt = datetime.fromisoformat(s.rstrip('Z'))
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
intents.members = True       # must also be enabled in Developer Portal
intents.message_content = True
bot = commands.Bot(
    command_prefix='!',
    intents=intents,
    allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=True)
)
tree = bot.tree  # for slash commands

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
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s | %(message)s')
logger = logging.getLogger("titlebot")

def log_kv(**kv):
    """Short structured logs."""
    logger.info(" ".join(f"{k}={v}" for k, v in kv.items()))

# ========= Helper: state & logs =========
def initialize_state():
    global state
    state = {
        'titles': {},
        'users': {},
        'config': {},
        'schedules': {},
        'sent_reminders': [],
        'activated_slots': {},   # reservations already auto-assigned
        'approvals': {}
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
def _sanitize_for_discord(s: str) -> str:
    """Basic sanitize to avoid mass mentions and weird control chars."""
    if not isinstance(s, str):
        return "-"
    # Neutralize @everyone and @here and bare role mentions
    s = s.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    return "".join(ch for ch in s if ch.isprintable())

def send_webhook_notification(data, reminder=False):
    if not WEBHOOK_URL:
        return
    role_tag = f"<@&{GUARDIAN_ROLE_ID}>" if GUARDIAN_ROLE_ID else ""
    channel_tag = f"<#{TITLE_REQUESTS_CHANNEL_ID}>" if TITLE_REQUESTS_CHANNEL_ID else ""

    title_name = _sanitize_for_discord(data.get('title_name', '-'))
    ign = _sanitize_for_discord(data.get('in_game_name', '-'))
    coordinates = _sanitize_for_discord(data.get('coordinates', '-'))
    submitted_by = _sanitize_for_discord(data.get('discord_user', '-'))

    if reminder:
        title = f"Reminder: {title_name} shift starts soon!"
        content = f"{role_tag} {channel_tag}  The {get_shift_hours()}-hour shift for **{title_name}** by **{ign}** starts in 5 minutes!"
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
                {"name": "Title", "value": title_name, "inline": True},
                {"name": "In-Game Name", "value": ign, "inline": True},
                {"name": "Coordinates", "value": coordinates, "inline": True},
                {"name": "Submitted By", "value": submitted_by, "inline": False}
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
    def __init__(self, bot_):
        self.bot = bot_
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
        await self.announce(f"TITLE RELEASED: **'{_sanitize_for_discord(title_name)}'** is now available. Reason: {reason}")
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
                                await self.announce(f"SCHEDULED HANDOFF: **{_sanitize_for_discord(title_name)}** is now assigned to **{_sanitize_for_discord(reserver_ign)}**.")
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

    # ------- Classic text commands (legacy) -------
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
        await self.announce(f"SHIFT CHANGE: **{_sanitize_for_discord(ign)}** has been granted **'{_sanitize_for_discord(title_name)}'**.")
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

# Health endpoint for Render
@app.route("/healthz")
def healthz():
    try:
        titles_loaded = bool(state.get("titles"))
        return jsonify({"ok": True, "titles_loaded": titles_loaded, "time": now_utc().isoformat()}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

def compute_next_reservation_for_title(title_name: str) -> Tuple[Optional[str], Optional[str]]:
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

# start Flask exactly once (before Discord connects)
_flask_started_evt = Event()
def start_flask_once():
    if not _flask_started_evt.is_set():
        Thread(target=run_flask_app, daemon=True).start()
        _flask_started_evt.set()

# ========= Templates (auto-create if missing) =========
os.makedirs('templates', exist_ok=True)

def _write_if_missing(path, content):
    if not os.path.exists(path):
        with open(path, 'w') as f:
            f.write(content)

# Minimal templates (same as your current ones) omitted for brevity in this file:
# If you rely on auto-creation, keep your existing content blocks here.
# Otherwise, ship templates/ as real files in your repo.

# ========= Slash Commands =========

def _admin_only(interaction: discord.Interaction) -> bool:
    """DM-safe admin check for slash commands."""
    if interaction.guild is None:
        return False
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)

@tree.command(name="titles", description="List all titles and their status (slash).")
async def slash_titles(interaction: discord.Interaction):
    embed = discord.Embed(title="Title Status", color=discord.Color.blurple())
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
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="assign", description="Assign a title to an IGN immediately (admin only).")
@app_commands.describe(title="Exact title name", ign="In-game name", coords="Optional coordinates (X:Y)")
async def slash_assign(interaction: discord.Interaction, title: str, ign: str, coords: Optional[str] = "-"):
    if not _admin_only(interaction):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    title = title.strip()
    ign = ign.strip()
    coords = (coords or "-").strip()
    if title not in state.get("titles", {}):
        await interaction.response.send_message(f"Title '{title}' not found.", ephemeral=True)
        return
    now = now_utc()
    end = now + timedelta(hours=get_shift_hours())
    state["titles"][title].update({
        "holder": {"name": ign, "coords": coords, "discord_id": interaction.user.id},
        "claim_date": now.isoformat(),
        "expiry_date": end.isoformat(),
        "pending_claimant": None
    })
    log_action('assign_slash', interaction.user.id, {'title': title, 'ign': ign})
    await save_state()
    await interaction.response.send_message(f"Assigned **{_sanitize_for_discord(title)}** to **{_sanitize_for_discord(ign)}**.", ephemeral=True)
    await send_to_log_channel(bot, f"[ASSIGN/SLASH] {interaction.user.display_name} assigned {title} -> {ign}")

@tree.command(name="reserve", description="Reserve a slot (web-equivalent reserve).")
@app_commands.describe(title="Title (Architect/General/Governor/Prefect)", start_utc="Slot start in ISO (YYYY-MM-DDTHH:MM:00)", ign="Your IGN", coords="X:Y coords")
async def slash_reserve(interaction: discord.Interaction, title: str, start_utc: str, ign: str, coords: Optional[str] = "-"):
    # Only allow specific requestable titles
    if title not in REQUESTABLE:
        await interaction.response.send_message("This title cannot be requested via reserve.", ephemeral=True)
        return
    try:
        schedule_time = parse_iso_utc(start_utc)
    except Exception:
        await interaction.response.send_message("Bad time format. Use ISO: YYYY-MM-DDTHH:MM:00 (UTC).", ephemeral=True)
        return
    if schedule_time < now_utc():
        await interaction.response.send_message("Cannot schedule in the past.", ephemeral=True)
        return

    slot_key = iso_slot_key_naive(schedule_time)
    schedules_for_title = state.setdefault('schedules', {}).setdefault(title, {})
    if slot_key in schedules_for_title:
        await interaction.response.send_message(f"That slot for {title} is already reserved.", ephemeral=True)
        return

    schedules_for_title[slot_key] = {"ign": ign, "coords": coords or "-"}
    log_action('schedule_book', interaction.user.id, {'title': title, 'time': slot_key, 'ign': ign, 'coords': coords or '-'})

    # Auto-assign if current slot and vacant
    try:
        hours = get_shift_hours()
        if (schedule_time <= now_utc() < schedule_time + timedelta(hours=hours)) and title_is_vacant_now(title):
            end = schedule_time + timedelta(hours=hours)
            state['titles'].setdefault(title, {})
            state['titles'][title].update({
                'holder': {'name': ign, 'coords': coords or '-', 'discord_id': interaction.user.id},
                'claim_date': schedule_time.isoformat(),
                'expiry_date': end.isoformat(),
                'pending_claimant': None
            })
            log_action('auto_assign_now', interaction.user.id, {'title': title, 'ign': ign, 'start': schedule_time.isoformat()})
    except Exception:
        pass

    csv_data = {
        "timestamp": now_utc().isoformat(),
        "title_name": title,
        "in_game_name": ign,
        "coordinates": coords or "-",
        "discord_user": f"{interaction.user} (slash)"
    }
    log_to_csv(csv_data)
    try:
        send_webhook_notification(csv_data, reminder=False)
    except Exception:
        pass

    await save_state()
    await interaction.response.send_message(f"Reserved {title} for {ign} at {slot_key} (UTC).", ephemeral=True)
    await send_to_log_channel(bot, f"[SCHEDULE/SLASH] reserved {title} for {ign} @ {slot_key} UTC")

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
        get_shift_hours=get_shift_hours,   # share getter with routes
        bot=bot,
    )
)

# ========= Discord lifecycle =========
_cog_loaded = False

@bot.event
async def on_ready():
    global _cog_loaded
    try:
        await load_state()
        await initialize_titles()
        await rebuild_schedules_from_log()

        if not _cog_loaded:
            # add_cog is synchronous in discord.py/py-cord 2.x
            bot.add_cog(TitleCog(bot))
            _cog_loaded = True

        # Sync slash commands to this guild set (global by default)
        try:
            await tree.sync()
            log_kv(event="slash_sync_ok", count=len(tree.get_commands()))
        except Exception as e:
            logger.error(f"Slash sync failed: {e}")

        logger.info(f'{bot.user.name} connected (id={bot.user.id})')
    except Exception as e:
        logger.exception(f"on_ready failed: {e}")

# ========= Entry =========
if __name__ == "__main__":
    # Ensure Flask binds PORT early so Render detects an open port even if Discord errors/reconnects
    start_flask_once()

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("Error: DISCORD_TOKEN environment variable not set.")
    else:
        # Tip: On Render, pin Python 3.12 and these versions:
        # py-cord==2.6.1 Flask==2.3.3 Jinja2==3.1.6 waitress==2.1.2 requests==2.32.5
        bot.run(token)