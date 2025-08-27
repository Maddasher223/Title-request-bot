# main.py — COMPLETE & FIXED (reservations show immediately, per-title checks only, log channel, no-IGN cancel)

import os
import csv
import json
import logging
import asyncio
import requests
from threading import Thread
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, request, redirect, url_for, flash
from waitress import serve

import discord
from discord.ext import commands, tasks

# ========= UTC helpers & constants =========
UTC = timezone.utc
SHIFT_HOURS = 3  # 3-hour reservation/assignment windows

def now_utc() -> datetime:
    return datetime.now(UTC)

def parse_iso_utc(s: str) -> datetime:
    """Parse ISO strings; make them UTC-aware if they were saved naive."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt

def iso_slot_key_naive(dt: datetime) -> str:
    """
    Normalized slot key we use everywhere in schedules:
    'YYYY-MM-DDTHH:MM:SS' naive (no timezone), seconds forced to :00.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt.replace(second=0, microsecond=0).isoformat()

def normalize_iso_slot_string(s: str) -> str:
    """
    Accept stored slot strings in naive or tz-aware forms, return naive 'YYYY-MM-DDTHH:MM:00'.
    """
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return s
    return iso_slot_key_naive(dt)

def in_current_slot(slot_start: datetime) -> bool:
    """Is now within [slot_start, slot_start+3h)?"""
    if slot_start.tzinfo is None:
        slot_start = slot_start.replace(tzinfo=UTC)
    end = slot_start + timedelta(hours=SHIFT_HOURS)
    return slot_start <= now_utc() < end

def slot_is_reserved(title_name: str, slot_key: str) -> bool:
    return slot_key in state.get('schedules', {}).get(title_name, {})

def reserve_slot(title_name: str, slot_key: str, reserver_ign: str) -> bool:
    """Write a future reservation if not already taken (PER TITLE ONLY)."""
    schedules = state.setdefault('schedules', {}).setdefault(title_name, {})
    if slot_key in schedules:
        return False
    schedules[slot_key] = reserver_ign
    return True

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

WEBHOOK_URL = "https://discord.com/api/webhooks/1409980293762253001/s5ffx0R9Tl9fhcvQXAWaqA_LG5b7SsUmpzeBHZOdGGznnLg_KRNwtk6sGvOOhh0oSw10"
GUARDIAN_ROLE_ID = 1409964411057344512
TITLE_REQUESTS_CHANNEL_ID = 1409770504696631347
# Simple admin PIN for /admin (set env ADMIN_PIN, or it defaults to "ARC1041")
ADMIN_PIN = os.getenv("ADMIN_PIN", "ARC1041")

# ========= Discord setup =========
intents = discord.Intents.default()
intents.members = True
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
        with open(CSV_FILE, 'a', newline='') as csvfile:
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
            if not title_name or not iso_time or not ign:
                continue
            if title_name not in TITLES_CATALOG:
                continue
            norm_key = normalize_iso_slot_string(iso_time)
            state['schedules'].setdefault(title_name, {}).setdefault(norm_key, ign)
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

# ========= Webhook helper =========
def send_webhook_notification(data, reminder=False):
    role_tag = f"<@&{GUARDIAN_ROLE_ID}>"
    channel_tag = f"<#{TITLE_REQUESTS_CHANNEL_ID}>"

    if reminder:
        title = f"Reminder: {data.get('title_name','-')} shift starts soon!"
        content = f"{role_tag} {channel_tag} ⏰ The 3-hour shift for **{data.get('title_name','-')}** by **{data.get('in_game_name','-')}** starts in 5 minutes!"
    else:
        title = "New Title Request"
        content = f"{role_tag} {channel_tag} 👑 A new request was submitted."

    payload = {
        "content": content,
        "allowed_mentions": {"parse": ["roles", "everyone"]},
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
    if ctx.author.guild_permissions.administrator:
        return True
    guardian_role_ids = state.get('config', {}).get('guardian_roles', [])
    user_role_ids = {role.id for role in ctx.author.roles}
    return any(role_id in user_role_ids for role_id in guardian_role_ids)

# ========= Discord Log Channel helper =========
async def send_to_log_channel(bot_obj, message: str):
    channel_id = state.get('config', {}).get('log_channel')
    if not channel_id:
        return
    try:
        channel = await bot_obj.fetch_channel(channel_id)
        if channel:
            await channel.send(message)
    except Exception as e:
        logger.error(f"send_to_log_channel failed: {e}")

def fire_and_forget_to_bot_loop(coro):
    """Schedule a coroutine onto the Discord bot loop from any thread (e.g., Flask)."""
    try:
        asyncio.run_coroutine_threadsafe(coro, bot.loop)
    except Exception as e:
        logger.error(f"fire_and_forget_to_bot_loop failed: {e}")

# ========= Discord Cog =========
class TitleCog(commands.Cog, name="TitleRequest"):
    def __init__(self, bot):
        self.bot = bot
        self.title_check_loop.start()

    @tasks.loop(minutes=1)
    async def title_check_loop(self):
        await self.bot.wait_until_ready()
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
            for iso_time, ign in schedule_data.items():
                if iso_time in state['sent_reminders']:
                    continue
                shift_time = parse_iso_utc(iso_time) if ("+" in iso_time) else (
                    parse_iso_utc(iso_time + "+00:00") if len(iso_time) == 19 else parse_iso_utc(iso_time)
                )
                reminder_time = shift_time - timedelta(minutes=5)
                if reminder_time <= now < shift_time:
                    try:
                        csv_data = {
                            "timestamp": now_utc().isoformat(),
                            "title_name": title_name,
                            "in_game_name": ign if isinstance(ign, str) else str(ign),
                            "coordinates": "-",
                            "discord_user": "Scheduler"
                        }
                        send_webhook_notification(csv_data, reminder=True)
                        state['sent_reminders'].append(iso_time)
                    except Exception as e:
                        logger.error(f"Could not send shift reminder: {e}")

        # Auto-assign reserved slots at slot start if vacant
        for title_name, schedule_data in state.get('schedules', {}).items():
            for iso_time, reserver_ign in list(schedule_data.items()):
                try:
                    slot_start = parse_iso_utc(iso_time) if len(iso_time) >= 19 else datetime.fromisoformat(iso_time).replace(tzinfo=UTC)
                    slot_end = slot_start + timedelta(hours=SHIFT_HOURS)
                    if slot_start <= now < slot_end and not is_activated(title_name, iso_time):
                        if title_is_vacant_now(title_name):
                            state['titles'].setdefault(title_name, {})
                            state['titles'][title_name].update({
                                'holder': {'name': reserver_ign, 'coords': '-', 'discord_id': None},
                                'claim_date': slot_start.isoformat(),
                                'expiry_date': slot_end.isoformat(),
                                'pending_claimant': None
                            })
                            mark_activated(title_name, iso_time)
                            try:
                                await self.announce(
                                    f"✅ Scheduled handoff: {title_name} is now assigned to {reserver_ign} "
                                    f"({slot_start.strftime('%H:%M')}–{slot_end.strftime('%H:%M')} UTC)."
                                )
                                await send_to_log_channel(self.bot,
                                    f"[AUTO-ASSIGN] {title_name} -> {reserver_ign} at {slot_start.strftime('%Y-%m-%d %H:%M')}Z")
                            except Exception:
                                pass
                except Exception as e:
                    logger.error(f"Auto-assign-from-schedule failed for {title_name} {iso_time}: {e}")

        if titles_to_release:
            await save_state()

    async def handle_claim_request(self, guild, title_name, ign, coords, author):
        if title_name not in REQUESTABLE:
            logger.warning(f"Attempt to claim non-requestable title: {title_name}")
            return

        if any(t.get('holder') and t['holder']['name'] == ign for t in state['titles'].values()):
            if isinstance(author, discord.Member):
                await author.send("You already hold a title.")
            return

        title = state['titles'][title_name]
        claimant_data = {'name': ign, 'coords': coords, 'discord_id': author.id if author else 0}

        if not title.get('holder'):
            title['pending_claimant'] = claimant_data
            timestamp = now_utc().isoformat()
            discord_user = f"{author.name} ({author.id})" if author else "Web Form"

            log_action('claim_request', author.id if author else 0, {'title': title_name, 'ign': ign, 'coords': coords})
            csv_data = {'timestamp': timestamp, 'title_name': title_name, 'in_game_name': ign, 'coordinates': coords, 'discord_user': discord_user}
            log_to_csv(csv_data)
            send_webhook_notification(csv_data, reminder=False)

            guardian_message = (f"👑 **Title Request:** Player **{ign}** ({coords}) has requested **'{title_name}'**. "
                                f"Approve with `!assign {title_name} | {ign}`.")
            await self.notify_guardians(guild, title_name, guardian_message)
            if isinstance(author, discord.Member):
                await author.send(f"Your request for '{title_name}' for player **{ign}** has been submitted.")
        else:
            title.setdefault('queue', []).append(claimant_data)
            log_action('queue_join', author.id if author else 0, {'title': title_name, 'ign': ign})
            if isinstance(author, discord.Member):
                await author.send(f"Player **{ign}** has been added to the queue for '{title_name}'.")
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
                expiry = parse_iso_utc(data['expiry_date'])
                remaining = expiry - now_utc()
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
        now = now_utc()
        expiry_date = now + timedelta(hours=min_hold_hours)
        title.update({
            'holder': pending_claimant,
            'claim_date': now.isoformat(),
            'expiry_date': expiry_date.isoformat(),
            'pending_claimant': None
        })
        log_action('assign', ctx.author.id, {'title': title_name, 'ign': ign})
        await save_state()
        user_mention = f"<@{title['holder']['discord_id']}>"
        await self.announce(f"🎉 SHIFT CHANGE: {user_mention}, player **{ign}** has been granted **'{title_name}'**.")
        await ctx.send(f"Successfully assigned '{title_name}' to player **{ign}**.")
        await send_to_log_channel(self.bot, f"[ASSIGN] {ctx.author.display_name} assigned {title_name} -> {ign}")

    @commands.command(help="Set the announcement channel. Usage: !set_announce <#channel>")
    @commands.has_permissions(administrator=True)
    async def set_announce(self, ctx, channel: discord.TextChannel):
        state.setdefault('config', {})['announcement_channel'] = channel.id
        await save_state()
        await ctx.send(f"Announcement channel set to {channel.mention}.")

    @commands.command(help="Set the log channel. Usage: !set_log <#channel>")
    @commands.has_permissions(administrator=True)
    async def set_log(self, ctx, channel: discord.TextChannel):
        state.setdefault('config', {})['log_channel'] = channel.id
        await save_state()
        await ctx.send(f"Log channel set to {channel.mention}.")

    @commands.command(help="Book a 3-hour time slot. Usage: !schedule <Title Name> | <In-Game Name> | <YYYY-MM-DD> | <HH:00>")
    async def schedule(self, ctx, *, full_argument: str):
        # 1) Parse inputs
        try:
            title_name, ign, date_str, time_str = [p.strip() for p in full_argument.split('|')]
        except ValueError:
            await ctx.send("Invalid format. Use `!schedule <Title Name> | <In-Game Name> | <YYYY-MM-DD> | <HH:00>`")
            return

        if title_name not in state['titles']:
            await ctx.send(f"Title '{title_name}' not found.")
            return

        # 2) Parse requested time
        try:
            schedule_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
            if schedule_time.minute != 0 or schedule_time.hour % 3 != 0:
                raise ValueError
        except ValueError:
            await ctx.send("Invalid time. Use a 3-hour increment (00:00, 03:00, 06:00, etc.).")
            return

        if schedule_time < now_utc():
            await ctx.send("Cannot schedule a time in the past.")
            return

        # 3) Slot key
        schedule_key = iso_slot_key_naive(schedule_time)

        # 4) ONLY block same title + same slot
        schedules = state['schedules'].setdefault(title_name, {})
        if schedule_key in schedules:
            await ctx.send(f"This slot is already booked by **{schedules[schedule_key]}**.")
            return

        # 5) Reserve
        schedules[schedule_key] = ign
        log_action('schedule_book', ctx.author.id, {'title': title_name, 'time': schedule_key, 'ign': ign})

        # 6) Auto-assign now if current slot & vacant
        try:
            if (schedule_time <= now_utc() < schedule_time + timedelta(hours=SHIFT_HOURS)) and title_is_vacant_now(title_name):
                end = schedule_time + timedelta(hours=SHIFT_HOURS)
                state['titles'][title_name].update({
                    'holder': {'name': ign, 'coords': '-', 'discord_id': None},
                    'claim_date': schedule_time.isoformat(),
                    'expiry_date': end.isoformat(),
                    'pending_claimant': None
                })
                log_action('auto_assign_now', ctx.author.id, {
                    'title': title_name, 'ign': ign, 'start': schedule_time.isoformat()
                })
                await self.announce(
                    f"⚡ Auto-assigned **{title_name}** to **{ign}** for the current slot "
                    f"({schedule_time.strftime('%H:%M')}–{(schedule_time+timedelta(hours=SHIFT_HOURS)).strftime('%H:%M')} UTC)."
                )
        except Exception as e:
            logger.error(f"Auto-assign-now failed: {e}")

        await save_state()
        await ctx.send(f"Booked '{title_name}' for **{ign}** on {date_str} at {time_str} UTC.")
        await self.announce(
            f"🗓️ SCHEDULE UPDATE: A 3-hour slot for **'{title_name}'** was booked by **{ign}** "
            f"for {date_str} at {time_str} UTC."
        )
        await send_to_log_channel(self.bot,
            f"[SCHEDULE] {ctx.author.display_name} reserved {title_name} for {ign} @ {date_str} {time_str} UTC")

    @commands.command(name="unschedule", help='Unschedule a reservation. Usage: !unschedule "Title Name" 2025-08-30T15:00:00')
    async def unschedule(self, ctx, title_name: str, slot_iso: str):
        """Remove a reservation. Only the reserver (by IGN) or an admin can cancel."""
        try:
            title_name = title_name.strip()
            slot_key = slot_iso.strip()
            schedules = state.get('schedules', {}).get(title_name, {})
            if not schedules:
                await ctx.send(f"No schedule exists for: {title_name}")
                return

            reserver_ign = schedules.get(slot_key)
            if not reserver_ign:
                await ctx.send(f"No reservation found at {slot_key} for {title_name}.")
                return

            caller_ign = (state.get('users', {}).get(str(ctx.author.id), {}) or {}).get('ign') or ctx.author.display_name
            perms = getattr(ctx.author, "guild_permissions", None)
            caller_is_admin = bool(perms and (perms.manage_guild or perms.administrator))

            if caller_ign.lower() != reserver_ign.lower() and not caller_is_admin:
                await ctx.send("Only the reserver or an admin can cancel this slot.")
                return

            del schedules[slot_key]
            acts = state.get('activated_slots', {}).get(title_name, [])
            if slot_key in acts:
                acts.remove(slot_key)

            await save_state()
            await ctx.send(f"Cancelled: {title_name} @ {slot_key} (was reserved by {reserver_ign}).")
            await send_to_log_channel(self.bot,
                f"[UNSCHEDULE] {ctx.author.display_name} cancelled {title_name} @ {slot_key} (was {reserver_ign})")

        except Exception as e:
            await ctx.send(f"Sorry, something went wrong cancelling that reservation: {e}")

    async def force_release_logic(self, title_name, actor_id, reason):
        if title_name not in state['titles'] or not state['titles'][title_name].get('holder'):
            return
        holder_info = state['titles'][title_name]['holder']
        log_action('force_release', actor_id, {'title': title_name, 'ign': holder_info['name'], 'reason': reason})
        state['titles'][title_name].update({'holder': None, 'claim_date': None, 'expiry_date': None})
        # simple announce; queue handling omitted here as it's unchanged
        await save_state()
        await self.announce(f"👑 The title **'{title_name}'** held by **{holder_info['name']}** has automatically expired.")
        await send_to_log_channel(self.bot, f"[EXPIRE] {title_name} released from {holder_info['name']}")

    async def announce(self, message):
        channel_id = state.get('config', {}).get('announcement_channel')
        if channel_id:
            try:
                channel = await self.bot.fetch_channel(channel_id)
                if channel:
                    await channel.send(message)
            except (discord.NotFound, discord.Forbidden) as e:
                logger.error(f"Could not send to announcement channel {channel_id}: {e}")

    async def notify_guardians(self, guild, title_name, message):
        await self.announce(message)

# ========= Flask App =========
ensure_icons_cached()
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret")  # needed for flash()

def get_bot_state():
    return state

def compute_next_reservation_for_title(title_name: str):
    """Return (slot_key, ign) for the next upcoming reservation for a title, or (None, None)."""
    schedules = state.get('schedules', {}).get(title_name, {})
    if not schedules:
        return (None, None)
    future = []
    for k, ign in schedules.items():
        try:
            dt = parse_iso_utc(k) if ("+" in k) else datetime.fromisoformat(k).replace(tzinfo=UTC)
            if dt >= now_utc():
                future.append((dt, k, ign))
        except Exception:
            continue
    if not future:
        return (None, None)
    future.sort(key=lambda x: x[0])
    _, k, ign = future[0]
    return (k, ign)

def get_all_upcoming_reservations():
    """Return a list of dicts for all future reservations across titles."""
    items = []
    for title_name, sched in state.get('schedules', {}).items():
        for slot_key, ign in sched.items():
            try:
                dt = parse_iso_utc(slot_key) if ("+" in slot_key) else datetime.fromisoformat(slot_key).replace(tzinfo=UTC)
            except Exception:
                continue
            if dt >= now_utc():
                items.append({
                    "title": title_name,
                    "slot_iso": slot_key,
                    "slot_dt": dt,
                    "ign": ign
                })
    # sort by datetime
    items.sort(key=lambda x: x["slot_dt"])
    return items

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
            expiry = parse_iso_utc(data['expiry_date'])
            delta = expiry - now_utc()
            remaining = str(timedelta(seconds=int(delta.total_seconds()))) if delta.total_seconds() > 0 else "Expired"

        next_slot_key, next_ign = compute_next_reservation_for_title(title_name)
        next_res_text = "—"
        if next_slot_key and next_ign:
            # visible confirmation that a future slot is RESERVED
            next_res_text = f"{next_slot_key} by {next_ign}"

        local_icon = url_for('static', filename=f"icons/{ICON_FILES[title_name]}")
        titles_data.append({
            'name': title_name,
            'holder': holder_info,
            'expires_in': remaining,
            'icon': local_icon,
            'buffs': TITLES_CATALOG[title_name]['effects'],
            'next_reserved': next_res_text
        })

    today = now_utc().date()
    days = [(today + timedelta(days=i)) for i in range(7)]
    hours = [f"{h:02d}:00" for h in range(0, 24, 3)]
    schedules = bot_state.get('schedules', {})
    requestable_titles = REQUESTABLE

    return render_template(
        'dashboard.html',
        titles=titles_data,
        days=days,
        hours=hours,
        schedules=schedules,
        today=today.strftime('%Y-%m-%d'),
        requestable_titles=requestable_titles
    )

@app.route("/log")
def view_log():
    log_data = []
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                log_data.append(row)
    return render_template('log.html', logs=reversed(log_data))

@app.route("/admin", methods=["GET"])
def admin_home():
    """
    SUPER SIMPLE ADMIN WIREFRAME (read-only for now).
    Access: /admin?pin=YOURPIN  (default pin: ARC1041)
    """
    pin = request.args.get("pin", "")
    if pin != ADMIN_PIN:
        return "Forbidden (bad or missing pin). Add ?pin=YOURPIN", 403
    
from flask import request, redirect, url_for

# --- ADMIN ACTIONS ---

@app.post("/admin/force-release")
def admin_force_release():
    pin = request.args.get("pin", "")
    if pin != ADMIN_PIN:
        return "Forbidden", 403
    title = request.form.get("title", "").strip()
    if not title or title not in state["titles"]:
        flash("Bad title")
        return redirect(url_for("admin_home", pin=pin))

    # clear holder
    state["titles"][title].update({
        "holder": None,
        "claim_date": None,
        "expiry_date": None,
        "pending_claimant": None
    })
    asyncio.run(save_state())
    flash(f"Force released {title}")
    return redirect(url_for("admin_home", pin=pin))


@app.post("/admin/cancel")
def admin_cancel():
    pin = request.args.get("pin", "")
    if pin != ADMIN_PIN:
        return "Forbidden", 403
    title = request.form.get("title", "").strip()
    slot = request.form.get("slot", "").strip()
    sched = state.get("schedules", {}).get(title, {})
    if not (title and slot and slot in sched):
        flash("Reservation not found")
        return redirect(url_for("admin_home", pin=pin))

    ign = sched[slot]
    del sched[slot]
    asyncio.run(save_state())
    flash(f"Cancelled {title} @ {slot} (was {ign})")
    return redirect(url_for("admin_home", pin=pin))


@app.post("/admin/assign-now")
def admin_assign_now():
    pin = request.args.get("pin", "")
    if pin != ADMIN_PIN:
        return "Forbidden", 403
    title = request.form.get("title", "").strip()
    ign = request.form.get("ign", "").strip()
    slot = request.form.get("slot", "").strip()
    if not (title and ign and title in state["titles"]):
        flash("Bad assign request")
        return redirect(url_for("admin_home", pin=pin))

    # assign immediately
    now = now_utc()
    end = now + timedelta(hours=SHIFT_HOURS)
    state["titles"][title].update({
        "holder": {"name": ign, "coords": "-", "discord_id": 0},
        "claim_date": now.isoformat(),
        "expiry_date": end.isoformat(),
        "pending_claimant": None
    })
    asyncio.run(save_state())
    flash(f"Assigned {title} immediately to {ign}")
    return redirect(url_for("admin_home", pin=pin))


@app.post("/admin/move")
def admin_move():
    pin = request.args.get("pin", "")
    if pin != ADMIN_PIN:
        return "Forbidden", 403
    title = request.form.get("title", "").strip()
    slot = request.form.get("slot", "").strip()
    new_title = request.form.get("new_title", "").strip()
    new_slot = request.form.get("new_slot", "").strip()
    if not (title and slot and new_title and new_slot):
        flash("Missing info")
        return redirect(url_for("admin_home", pin=pin))

    sched = state.get("schedules", {}).get(title, {})
    if slot not in sched:
        flash("Original reservation not found")
        return redirect(url_for("admin_home", pin=pin))

    ign = sched[slot]
    del sched[slot]
    state.setdefault("schedules", {}).setdefault(new_title, {})[new_slot] = ign
    asyncio.run(save_state())
    flash(f"Moved {ign} from {title}@{slot} → {new_title}@{new_slot}")
    return redirect(url_for("admin_home", pin=pin))


@app.post("/admin/settings")
def admin_settings():
    pin = request.args.get("pin", "")
    if pin != ADMIN_PIN:
        return "Forbidden", 403
    announce = request.form.get("announce_channel")
    log = request.form.get("log_channel")
    shift = request.form.get("shift_hours")

    cfg = state.setdefault("config", {})
    if announce: cfg["announcement_channel"] = int(announce)
    if log: cfg["log_channel"] = int(log)
    if shift:
        try:
            global SHIFT_HOURS
            SHIFT_HOURS = int(shift)
        except ValueError:
            flash("Invalid shift hour")
    asyncio.run(save_state())
    flash("Settings updated")
    return redirect(url_for("admin_home", pin=pin))

    # Active titles
    active = []
    for title_name in ORDERED_TITLES:
        t = state.get('titles', {}).get(title_name, {})
        if t and t.get("holder"):
            exp = parse_iso_utc(t["expiry_date"]) if t.get("expiry_date") else None
            active.append({
                "title": title_name,
                "holder": t["holder"]["name"],
                "coords": t["holder"].get("coords", "-"),
                "expires": exp.isoformat() if exp else "-"
            })

    # Upcoming reservations (future only)
    upcoming = get_all_upcoming_reservations()

    # Recent CSV log (limit 200 rows)
    logs = []
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                logs.append(row)
    logs = logs[-200:]  # last 200

    # Settings (current)
    cfg = state.get('config', {})
    current_settings = {
        "announcement_channel": cfg.get("announcement_channel"),
        "log_channel": cfg.get("log_channel"),
        "shift_hours": SHIFT_HOURS,
    }

    return render_template(
        "admin.html",
        active_titles=active,
        upcoming=upcoming,
        logs=reversed(logs),
        settings=current_settings
    )

@app.post("/cancel")
def cancel_schedule():
    """
    Web cancel for a reservation.
    NOTE: No IGN required (per your request).
    """
    title_name = request.form.get("title", "").strip()
    slot_key   = request.form.get("slot", "").strip()

    if not title_name or not slot_key:
        flash("Missing info to cancel.")
        return redirect(url_for("dashboard"))

    schedule = state.get('schedules', {}).get(title_name, {})
    reserver_ign = schedule.get(slot_key)
    if not reserver_ign:
        flash("No reservation found for that slot.")
        return redirect(url_for("dashboard"))

    try:
        del schedule[slot_key]
    except KeyError:
        pass

    acts = state.get('activated_slots', {}).get(title_name, [])
    if slot_key in acts:
        acts.remove(slot_key)

    # Persist + log to Discord log channel (through bot loop)
    try:
        fire_and_forget_to_bot_loop(save_state())
        fire_and_forget_to_bot_loop(send_to_log_channel(bot,
            f"[UNSCHEDULE:WEB] cancelled {title_name} @ {slot_key} (was {reserver_ign})"))
    except Exception:
        pass

    flash(f"Cancelled reservation for {title_name} @ {slot_key}.")
    return redirect(url_for("dashboard"))

@app.route("/book-slot", methods=['POST'])
def book_slot():
    # Read form inputs (coords can be blank)
    title_name = (request.form.get('title') or '').strip()
    ign        = (request.form.get('ign') or '').strip()
    coords     = (request.form.get('coords') or '').strip()
    date_str   = (request.form.get('date') or '').strip()
    time_str   = (request.form.get('time') or '').strip()

    # Validate
    if not title_name or not ign or not date_str or not time_str:
        flash("Missing form data: title, IGN, date, and time are required.")
        return redirect(url_for("dashboard"))
    if title_name not in REQUESTABLE:
        flash("This title cannot be requested.")
        return redirect(url_for("dashboard"))

    # Parse requested slot (UTC)
    try:
        schedule_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
    except ValueError:
        flash("Time must be HH:MM (24h), e.g., 15:00.")
        return redirect(url_for("dashboard"))
    if schedule_time < now_utc():
        flash("Cannot schedule a time in the past.")
        return redirect(url_for("dashboard"))

    # Slot key
    schedule_key = iso_slot_key_naive(schedule_time)

    # ONLY block same title + same slot (no cross-title/global bans)
    schedules_for_title = state.setdefault('schedules', {}).setdefault(title_name, {})
    if schedule_key in schedules_for_title:
        flash(f"That slot for {title_name} is already reserved by {schedules_for_title[schedule_key]}.")
        return redirect(url_for("dashboard"))

    # Reserve immediately so it shows as taken on the grid
    schedules_for_title[schedule_key] = ign
    log_action('schedule_book_web', 0, {'title': title_name, 'time': schedule_key, 'ign': ign})

    # If this is the current slot and the title is vacant, grant immediately
    try:
        if (schedule_time <= now_utc() < schedule_time + timedelta(hours=SHIFT_HOURS)) and title_is_vacant_now(title_name):
            end = schedule_time + timedelta(hours=SHIFT_HOURS)
            state['titles'].setdefault(title_name, {})
            state['titles'][title_name].update({
                'holder': {'name': ign, 'coords': coords or '-', 'discord_id': 0},
                'claim_date': schedule_time.isoformat(),
                'expiry_date': end.isoformat(),
                'pending_claimant': None
            })
            log_action('auto_assign_now', 0, {'title': title_name, 'ign': ign, 'start': schedule_time.isoformat()})
    except Exception as e:
        logger.error(f"Auto-assign-now (web) failed: {e}")

    # CSV + webhook
    csv_data = {
        "timestamp": now_utc().isoformat(),
        "title_name": title_name,
        "in_game_name": ign,
        "coordinates": coords or "-",
        "discord_user": "Web Form"
    }
    log_to_csv(csv_data)
    try:
        send_webhook_notification(csv_data, reminder=False)
    except Exception as e:
        logger.error(f"Webhook send failed after web booking: {e}")

    # Persist and log to Discord log channel (through bot loop)
    try:
        fire_and_forget_to_bot_loop(save_state())
        fire_and_forget_to_bot_loop(send_to_log_channel(bot,
            f"[SCHEDULE:WEB] reserved {title_name} for {ign} @ {date_str} {time_str} UTC"))
    except Exception:
        pass

    flash(f"Reserved {title_name} for {ign} on {date_str} at {time_str} UTC.")
    return redirect(url_for("dashboard"))

def run_flask_app():
    serve(app, host='0.0.0.0', port=8080)

# ========= Templates (written if missing) =========
if not os.path.exists('templates'):
    os.makedirs('templates')

# Dashboard
with open('templates/dashboard.html', 'w') as f:
    f.write("""<!DOCTYPE html>
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
            <h2>Reserve a 3-hour Slot</h2>
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
                                {% set who = schedule_data.get(slot_time) %}
                                {% if who %}
                                    <div class="cell-booking">
                                        <strong>{{ title_name }}</strong>
                                        <span class="badge">Reserved</span><br>
                                        {{ who }}<br>
                                        <form method="post" action="{{ url_for('cancel_schedule') }}" class="small" style="margin-top:6px;">
                                          <input type="hidden" name="title" value="{{ title_name }}">
                                          <input type="hidden" name="slot" value="{{ slot_time }}">
                                          <button type="submit">Cancel</button>
                                        </form>
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

        <p style="text-align: center; margin-top: 2em;"><a href="/log">View Full Request Log</a></p>
    </div>
</body>
</html>
""")

# Log page
with open('templates/log.html', 'w') as f:
    f.write("""<!DOCTYPE html>
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

# Admin wireframe page
with open('templates/admin.html', 'w') as f:
    f.write("""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Admin · Title Requestor</title>
  <link rel="icon" type="image/png" href="{{ url_for('static', filename='icons/title-requestor.png') }}">
  <style>
    body { font-family: sans-serif; background:#121417; color:#e7e9ea; margin: 2rem; }
    .container { max-width: 1400px; margin: 0 auto; }
    h1, h2 { margin: 0 0 10px 0; }
    .grid { display:grid; grid-template-columns: 1fr 1fr; gap: 20px; }
    .card { background:#1b1f24; border:1px solid #2a2f36; border-radius:10px; padding:16px; }
    table { width:100%; border-collapse: collapse; margin-top:8px; }
    th, td { border:1px solid #2a2f36; padding:8px; text-align:left; }
    th { background:#20252b; }
    .pill { display:inline-block; font-size:12px; padding:2px 6px; border-radius:999px; background:#2f6feb; color:#fff; }
    .row { display:flex; gap:10px; align-items:center; }
    input, select, button { padding:8px; border-radius:6px; border:1px solid #2a2f36; background:#0f1114; color:#e7e9ea; }
    button { background:#2f6feb; cursor:pointer; font-weight:600; }
    a { color:#8ab4f8; }
    .muted { opacity:.75; font-size:12px; }
  </style>
</head>
<body>
  <div class="container">
    <h1>Admin Dashboard</h1>
    <p class="muted">This is a wireframe (read-only). Buttons are placeholders until backend endpoints are added.</p>

    <div class="grid">
      <!-- Active Titles -->
      <div class="card">
        <h2>Active Titles <span class="pill">{{ active_titles|length }}</span></h2>
        <table>
          <thead>
            <tr>
              <th>Title</th><th>Holder</th><th>Coords</th><th>Expires</th><th>Action</th>
            </tr>
          </thead>
          <tbody>
            {% for t in active_titles %}
            <tr>
              <td>{{ t.title }}</td>
              <td>{{ t.holder }}</td>
              <td>{{ t.coords }}</td>
              <td>{{ t.expires }}</td>
              <td class="actions">
                <form method="post" action="{{ url_for('admin_force_release', pin=request.args.get('pin')) }}">
                    <input type="hidden" name="title" value="{{ t.title }}">
                    <button type="submit" title="Immediately free this title">Force Release</button>
                </form>
            </td>
            </tr>
            {% endfor %}
            {% if active_titles|length == 0 %}
            <tr><td colspan="5" class="muted">No active titles.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>

      <!-- Upcoming Reservations -->
      <div class="card">
        <h2>Upcoming Reservations</h2>
        <table>
          <thead>
            <tr>
              <th>Start (UTC)</th><th>Title</th><th>IGN</th><th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {% for r in upcoming %}
            <tr>
              <td>{{ r.slot_iso }}</td>
              <td>{{ r.title }}</td>
              <td>{{ r.ign }}</td>
              <td class="actions">
                <!-- Cancel -->
                <form method="post" action="{{ url_for('admin_cancel', pin=request.args.get('pin')) }}">
                    <input type="hidden" name="title" value="{{ r.title }}">
                    <input type="hidden" name="slot" value="{{ r.slot_iso }}">
                    <button type="submit">Cancel</button>
                </form>

                <!-- Assign Now -->
                <form method="post" action="{{ url_for('admin_assign_now', pin=request.args.get('pin')) }}">
                    <input type="hidden" name="title" value="{{ r.title }}">
                    <input type="hidden" name="ign" value="{{ r.ign }}">
                    <input type="hidden" name="slot" value="{{ r.slot_iso }}">
                    <button type="submit" title="Override: assign immediately">Assign Now</button>
                </form>

                <!-- Move -->
                <form method="post" action="{{ url_for('admin_move', pin=request.args.get('pin')) }}">
                    <input type="hidden" name="title" value="{{ r.title }}">
                    <input type="hidden" name="slot" value="{{ r.slot_iso }}">
                    <input type="text" name="new_title" placeholder="New Title (e.g., Architect)" required>
                    <input type="text" name="new_slot" placeholder="YYYY-MM-DDTHH:MM:00" required>
                    <button type="submit">Move</button>
                </form>
            </td>
            </tr>
            {% endfor %}
            {% if upcoming|length == 0 %}
            <tr><td colspan="4" class="muted">No future reservations.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>

      <!-- Logs -->
      <div class="card">
        <h2>Recent Activity (CSV)</h2>
        <table>
          <thead>
            <tr>
              <th>Time</th><th>Title</th><th>IGN</th><th>Coords</th><th>Source</th>
            </tr>
          </thead>
          <tbody>
            {% for row in logs %}
            <tr>
              <td>{{ row.timestamp }}</td>
              <td>{{ row.title_name }}</td>
              <td>{{ row.in_game_name }}</td>
              <td>{{ row.coordinates }}</td>
              <td>{{ row.discord_user }}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
        <p class="muted">This reads the last 200 lines from <code>data/requests.csv</code>.</p>
      </div>

      <!-- Settings -->
      <div class="card">
        <h2>Settings</h2>
        <form method="post" action="{{ url_for('admin_settings', pin=request.args.get('pin')) }}">
          <div class="row">
            <label>Announcement Channel ID:</label>
            <input type="text" name="announce_channel" value="{{ settings.announcement_channel or '' }}">
          </div>
          <div class="row" style="margin-top:6px;">
            <label>Log Channel ID:</label>
            <input type="text" name="log_channel" value="{{ settings.log_channel or '' }}">
          </div>
          <div class="row" style="margin-top:6px;">
            <label>Shift Duration (hrs):</label>
            <input type="number" name="shift_hours" value="{{ settings.shift_hours }}" min="1" max="24">
          </div>
          <div class="row" style="margin-top:10px;">
            <button type="submit">Save Settings</button>
          </div>
        </form>
        <p class="muted" style="margin-top:10px;">
          You can also set channels via Discord commands:<br>
          <code>!set_announce #channel</code> and <code>!set_log #channel</code>.
        </p>
      </div>
    </div>

    <p style="margin-top:16px;"><a href="/">← Back to Dashboard</a></p>
  </div>
</body>
</html>
""")

# ========= Discord lifecycle =========
@bot.event
async def on_ready():
    await load_state()
    await initialize_titles()
    await rebuild_schedules_from_log()

    await bot.add_cog(TitleCog(bot))
    try:
        await bot.tree.sync()
    except Exception as e:
        logger.error(f"Slash sync failed: {e}")

    logger.info(f'{bot.user.name} has connected!')
    Thread(target=run_flask_app, daemon=True).start()

# ========= Entry =========
if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("Error: DISCORD_TOKEN environment variable not set.")
    else:
        bot.run(token)