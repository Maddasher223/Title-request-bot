# main.py — COMPLETE & FIXED

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
    Produce the normalized slot key we use everywhere in schedules:
    'YYYY-MM-DDTHH:MM:SS' with NO timezone (naive), seconds forced to :00.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt.replace(second=0, microsecond=0).isoformat()

def normalize_iso_slot_string(s: str) -> str:
    """
    Accept a stored slot string in any of these forms:
      - naive 'YYYY-MM-DDTHH:MM[:SS]'
      - TZ-aware 'YYYY-MM-DDTHH:MM[:SS]+00:00'
    Return normalized naive 'YYYY-MM-DDTHH:MM:00'.
    """
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return s  # best effort
    return iso_slot_key_naive(dt)

def in_current_slot(slot_start: datetime) -> bool:
    """Is now within [slot_start, slot_start+3h)?"""
    if slot_start.tzinfo is None:
        slot_start = slot_start.replace(tzinfo=UTC)
    end = slot_start + timedelta(hours=SHIFT_HOURS)
    return slot_start <= now_utc() < end

def normalize_slot_key(slot_key: str) -> str:
    return slot_key.strip()

def slot_is_reserved(title_name: str, slot_key: str) -> bool:
    slot_key = normalize_slot_key(slot_key)
    return slot_key in state.get('schedules', {}).get(title_name, {})

def reserve_slot(title_name: str, slot_key: str, reserver_ign: str) -> bool:
    """Write a future reservation if not already taken."""
    slot_key = normalize_slot_key(slot_key)
    schedules = state.setdefault('schedules', {}).setdefault(title_name, {})
    if slot_key in schedules:
        return False
    schedules[slot_key] = reserver_ign
    return True

def can_book_slot(title_name: str, slot_start_dt: datetime) -> tuple[bool, str]:
    """
    Returns (ok, message). A slot is bookable only if it's not already reserved.
    """
    slot_key = iso_slot_key_naive(slot_start_dt)
    if slot_is_reserved(title_name, slot_key):
        reserver = state['schedules'][title_name][slot_key]
        return (False, f"That slot is already reserved by {reserver}.")
    return (True, "OK")

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

# ========= Discord setup =========
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# ========= Persistence (keep across restarts) =========
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
        'activated_slots': {}   # remember which reservations we already auto-assigned
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
    """Remember that this specific title+slot has been auto-assigned already."""
    act = state.setdefault('activated_slots', {}).setdefault(title_name, [])
    if slot_key not in act:
        act.append(slot_key)

def is_activated(title_name: str, slot_key: str) -> bool:
    """Check if we've already auto-assigned this reservation."""
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
    """Ensure all titles exist in state with default fields."""
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

# ========= Rebuild schedules from log on restart (normalize keys) =========
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
            action = entry.get('action')
            if action not in ('schedule_book', 'schedule_book_web'):
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
            schedules_for_title = state['schedules'].setdefault(title_name, {})
            schedules_for_title.setdefault(norm_key, ign)

        await save_state()
    except Exception as e:
        logger.error(f"rebuild_schedules_from_log failed: {e}")

# ========= Icons (cache locally) =========
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
                # tolerate both naive and tz-aware strings
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
                            except Exception:
                                pass
                except Exception as e:
                    logger.error(f"Auto-assign-from-schedule failed for {title_name} {iso_time}: {e}")

        # persist any changes done above
        if titles_to_release or any(
            iso_time in state.get('sent_reminders', [])
            for schedule_data in state.get('schedules', {}).values()
            for iso_time in schedule_data
        ):
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

    @commands.command(help="Claim a title. Usage: !claim <Title Name> | <In-Game Name> | <X:Y Coords>")
    async def claim(self, ctx, *, args: str):
        try:
            title_name, ign, coords = [arg.strip() for arg in args.split('|')]
        except ValueError:
            await ctx.send("Invalid format. Use `!claim <Title Name> | <In-Game Name> | <X:Y Coords>`")
            return
        await self.handle_claim_request(ctx.guild, title_name, ign, coords, ctx.author)

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

    @commands.command(help="Set the announcement channel. Usage: !set_announce <#channel>")
    @commands.has_permissions(administrator=True)
    async def set_announce(self, ctx, channel: discord.TextChannel):
        state.setdefault('config', {})['announcement_channel'] = channel.id
        await save_state()
        await ctx.send(f"Announcement channel set to {channel.mention}.")

    @commands.command(help="Book a 3-hour time slot. Usage: !schedule <Title Name> | <In-Game Name> | <YYYY-MM-DD> | <HH:00>")
    async def schedule(self, ctx, *, full_argument: str):
        # parse inputs
        try:
            title_name, ign, date_str, time_str = [p.strip() for p in full_argument.split('|')]
        except ValueError:
            await ctx.send("Invalid format. Use `!schedule <Title Name> | <In-Game Name> | <YYYY-MM-DD> | <HH:00>`")
            return
        if title_name not in state['titles']:
            await ctx.send(f"Title '{title_name}' not found.")
            return
        try:
            schedule_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
            if schedule_time.minute != 0 or schedule_time.hour % 3 != 0:
                raise ValueError
        except ValueError:
            await ctx.send("Invalid time. Use a 3-hour increment (00:00, 03:00, etc.).")
            return
        if schedule_time < now_utc():
            await ctx.send("Cannot schedule a time in the past.")
            return

        # enforce: a person can’t hold multiple titles at the same exact slot
        schedule_key = iso_slot_key_naive(schedule_time)
        for t_name, t_sched in state.get('schedules', {}).items():
            if schedule_key in t_sched and t_sched[schedule_key] == ign:
                await ctx.send(f"**{ign}** has already booked **{t_name}** for this slot.")
                return

        # enforce: only one booking per title per slot
        schedules = state['schedules'].setdefault(title_name, {})
        if schedule_key in schedules:
            await ctx.send(f"This slot is already booked by **{schedules[schedule_key]}**.")
            return

        # book it
        schedules[schedule_key] = ign
        log_action('schedule_book', ctx.author.id, {'title': title_name, 'time': schedule_key, 'ign': ign})

        # auto-assign now if in current slot and title vacant
        try:
            if in_current_slot(schedule_time) and title_is_vacant_now(title_name):
                end = schedule_time + timedelta(hours=SHIFT_HOURS)
                state['titles'][title_name].update({
                    'holder': {'name': ign, 'coords': '-', 'discord_id': None},
                    'claim_date': schedule_time.isoformat(),
                    'expiry_date': end.isoformat(),
                    'pending_claimant': None
                })
                log_action('auto_assign_now', ctx.author.id, {'title': title_name, 'ign': ign, 'start': schedule_time.isoformat()})
                await self.announce(
                    f"⚡ Auto-assigned **{title_name}** to **{ign}** for the current slot "
                    f"({schedule_time.strftime('%H:%M')}–{end.strftime('%H:%M')} UTC)."
                )
        except Exception as e:
            logger.error(f"Auto-assign-now failed: {e}")

        await save_state()
        await ctx.send(f"Booked '{title_name}' for **{ign}** on {date_str} at {time_str} UTC.")
        await self.announce(f"🗓️ SCHEDULE UPDATE: A 3-hour slot for **'{title_name}'** was booked by **{ign}** for {date_str} at {time_str} UTC.")

    @commands.command(name="unschedule", help='Unschedule a reservation. Usage: !unschedule "Title Name" 2025-08-30T15:00:00')
    async def unschedule(self, ctx, title_name: str, slot_iso: str):
        """
        Remove a reservation. Only the reserver (by IGN) or an admin can cancel.
        """
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

            # Figure caller IGN (fallback to display_name)
            caller_ign = (state.get('users', {}).get(str(ctx.author.id), {}) or {}).get('ign') or ctx.author.display_name

            # Admin check
            perms = getattr(ctx.author, "guild_permissions", None)
            caller_is_admin = bool(perms and (perms.manage_guild or perms.administrator))

            if caller_ign.lower() != reserver_ign.lower() and not caller_is_admin:
                await ctx.send("Only the reserver or an admin can cancel this slot.")
                return

            # Delete reservation
            del schedules[slot_key]
            # Clean activation mark if present
            acts = state.get('activated_slots', {}).get(title_name, [])
            if slot_key in acts:
                acts.remove(slot_key)

            await save_state()
            await ctx.send(f"Cancelled: {title_name} @ {slot_key} (was reserved by {reserver_ign}).")

        except Exception as e:
            await ctx.send(f"Sorry, something went wrong cancelling that reservation: {e}")

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
        guardian_message = (
            f"👑 **Next in Queue:** {user_mention}, it's **{next_in_line['name']}'s** turn for **'{title_name}'**! "
            f"A guardian must use `!assign {title_name} | {next_in_line['name']}` to grant it."
        )
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

        local_icon = url_for('static', filename=f"icons/{ICON_FILES[title_name]}")
        titles_data.append({
            'name': title_name,
            'holder': holder_info,
            'expires_in': remaining,
            'icon': local_icon,
            'buffs': TITLES_CATALOG[title_name]['effects']
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

@app.post("/cancel")
def cancel_schedule():
    """
    Web cancel for a reservation. The person must enter the same IGN as the one on the reservation.
    """
    title_name = request.form.get("title", "").strip()
    slot_key   = request.form.get("slot", "").strip()
    ign_input  = request.form.get("ign", "").strip()

    if not title_name or not slot_key or not ign_input:
        flash("Missing info to cancel.")
        return redirect(url_for("dashboard"))

    schedule = state.get('schedules', {}).get(title_name, {})
    reserver_ign = schedule.get(slot_key)
    if not reserver_ign:
        flash("No reservation found for that slot.")
        return redirect(url_for("dashboard"))

    if ign_input.lower() != reserver_ign.lower():
        flash("Only the reserver can cancel this slot from the web.")
        return redirect(url_for("dashboard"))

    try:
        del schedule[slot_key]
    except KeyError:
        pass

    acts = state.get('activated_slots', {}).get(title_name, [])
    if slot_key in acts:
        acts.remove(slot_key)

    # Persist
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(save_state())
        else:
            loop.run_until_complete(save_state())
    except Exception:
        pass

    flash(f"Cancelled reservation for {title_name} @ {slot_key}.")
    return redirect(url_for("dashboard"))

@app.route("/book-slot", methods=['POST'])
def book_slot():
    title_name = request.form.get('title')
    ign = request.form.get('ign')
    coords = request.form.get('coords')
    date_str = request.form.get('date')
    time_str = request.form.get('time')

    if not all([title_name, ign, coords, date_str, time_str]):
        return "Missing form data.", 400
    if title_name not in REQUESTABLE:
        return "This title cannot be requested.", 400

    schedule_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)

    async def do_booking():
        async with state_lock:
            # enforce: a person can’t hold multiple titles at the same exact slot
            schedule_key = iso_slot_key_naive(schedule_time)
            for t_name, t_sched in state.get('schedules', {}).items():
                if schedule_key in t_sched and t_sched[schedule_key] == ign:
                    # silently ignore double-book via web
                    return

            schedules = state['schedules'].setdefault(title_name, {})
            if schedule_key not in schedules:
                schedules[schedule_key] = ign
                log_action('schedule_book_web', 0, {'title': title_name, 'time': schedule_key, 'ign': ign})

                # auto assign now if current slot and vacant
                try:
                    if in_current_slot(schedule_time) and title_is_vacant_now(title_name):
                        end = schedule_time + timedelta(hours=SHIFT_HOURS)
                        state['titles'][title_name].update({
                            'holder': {'name': ign, 'coords': coords or '-', 'discord_id': 0},
                            'claim_date': schedule_time.isoformat(),
                            'expiry_date': end.isoformat(),
                            'pending_claimant': None
                        })
                        log_action('auto_assign_now', 0, {'title': title_name, 'ign': ign, 'start': schedule_time.isoformat()})
                        try:
                            send_webhook_notification({
                                "timestamp": now_utc().isoformat(),
                                "title_name": title_name,
                                "in_game_name": ign,
                                "coordinates": coords or "-",
                                "discord_user": "Web Form (Auto-Assign)"
                            }, reminder=False)
                        except Exception as e:
                            logger.error(f"Webhook on auto-assign failed: {e}")
                except Exception as e:
                    logger.error(f"Auto-assign-now (web) failed: {e}")

                # also write a CSV "request"
                csv_data = {
                    "timestamp": now_utc().isoformat(),
                    "title_name": title_name,
                    "in_game_name": ign,
                    "coordinates": coords,
                    "discord_user": "Web Form"
                }
                log_to_csv(csv_data)
                send_webhook_notification(csv_data, reminder=False)

                await save_state()

    bot.loop.call_soon_threadsafe(asyncio.create_task, do_booking())
    return redirect(url_for('dashboard'))

def run_flask_app():
    serve(app, host='0.0.0.0', port=8080)

# ========= Templates written if missing (kept same look) =========
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
            </div>
            {% endfor %}
        </div>

        <div class="form-card">
            <h2>Claim a Temple Title</h2>
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
                                    <div style="margin-bottom:8px;">
                                        <strong>{{ title_name }}</strong><br>{{ who }}
                                        <form method="post" action="{{ url_for('cancel_schedule') }}" class="small" style="margin-top:4px;">
                                          <input type="hidden" name="title" value="{{ title_name }}">
                                          <input type="hidden" name="slot" value="{{ slot_time }}">
                                          <input type="text" name="ign" placeholder="type your IGN" required style="width:9rem;">
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