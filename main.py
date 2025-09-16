# main.py — unified Flask app + APScheduler reminders + Discord bot
# Single source of truth. Non-blocking. Idempotent. UTC-safe.

from __future__ import annotations

import os
import csv
import json
import logging
import asyncio
import re
import atexit
import secrets
import time
from threading import Thread, RLock
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from contextlib import contextmanager

import requests
import discord
from discord.ext import commands, tasks
from discord.errors import LoginFailure
from discord import app_commands

from flask import Flask
from waitress import serve

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from dotenv import load_dotenv
from sqlalchemy import event, text, inspect
from sqlalchemy.exc import OperationalError

from models import db, Title, Reservation, ActiveTitle, RequestLog, Setting, ServerConfig
from db_utils import (
    get_shift_hours as db_get_shift_hours,
    set_shift_hours as db_set_shift_hours,
    compute_slots,
    requestable_title_names,
    title_status_cards,
    schedules_by_title,
    schedule_lookup,
)

# ---- web routes (optional; shim if file missing) -----------------
try:
    from web_routes import register_routes as _register_routes
except Exception:
    _register_routes = None

from admin_routes import register_admin

# ===== Airtable (optional; safe import) =====
try:
    from pyairtable import Api
except Exception:
    Api = None
    ApiError = Exception

def airtable_upsert(kind, data):
    """Stub for Airtable upsert; does nothing if not configured."""
    if not airtable_table:
        logger.debug("airtable_upsert skipped (%s): integration not configured", kind)
        return
    # Implement actual upsert logic if/when needed.

load_dotenv()

# -------------------- Constants & Globals --------------------
UTC = timezone.utc
SHIFT_HOURS = 12  # default shift window

BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
STATIC_DIR = os.path.join(BASE_DIR, "static", "icons")
os.makedirs(STATIC_DIR, exist_ok=True)

STATE_FILE = os.path.join(DATA_DIR, "titles_state.json")
CSV_FILE   = os.path.join(DATA_DIR, "requests.csv")

state: dict = {}
state_lock = RLock()

# Global scheduler handle
scheduler: Optional[BackgroundScheduler] = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s:%(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger("app")

# Keep a module-level handle to the Flask app so background threads can open an app context safely
APP: Optional[Flask] = None

# Public base URL used for cancel/manage links in Discord webhooks
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/") or None

def build_public_url(path: str) -> str:
    """Build a full public URL for the given path."""
    if not PUBLIC_BASE_URL:
        return path
    path = path if path.startswith("/") else f"/{path}"
    return f"{PUBLIC_BASE_URL}{path}"

# Discord / admin env
ADMIN_PIN = os.getenv("ADMIN_PIN", "letmein")
DISCORD_TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip()

# Airtable setup (optional)
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE   = os.getenv("AIRTABLE_TABLE", "TitleLog")
airtable_table = None
if Api and AIRTABLE_API_KEY and AIRTABLE_BASE_ID:
    try:
        api = Api(AIRTABLE_API_KEY)
        airtable_table = api.table(AIRTABLE_BASE_ID, AIRTABLE_TABLE)
    except Exception as e:
        logger.warning("Airtable not configured: %s", e)

# Multiserver cache (guild_id -> {"webhook": str, "guardian_role_id": Optional[int]})
SERVER_CONFIGS: dict[int, dict] = {}

# -------------------- Titles Catalog --------------------
TITLES_CATALOG = {
    "Guardian of Harmony": {
        "effects": "All benders' ATK +5%, All benders' DEF +5%, All Benders' recruiting speed +15%",
        "image": "/static/icons/guardian_harmony.png"
    },
    "Guardian of Water": {
        "effects": "All Benders' recruiting speed +15%",
        "image": "/static/icons/guardian_water.png"
    },
    "Guardian of Earth": {
        "effects": "Construction Speed +10%, Research Speed +10%",
        "image": "/static/icons/guardian_earth.png"
    },
    "Guardian of Fire": {
        "effects": "All benders' ATK +5%, All benders' DEF +5%",
        "image": "/static/icons/guardian_fire.png"
    },
    "Guardian of Air": {
        "effects": "All Resource Gathering Speed +20%, All Resource Production +20%",
        "image": "/static/icons/guardian_air.png"
    },
    "Architect": {"effects": "Construction Speed +10%", "image": "/static/icons/architect.png"},
    "General":   {"effects": "All benders' ATK +5%",    "image": "/static/icons/general.png"},
    "Governor":  {"effects": "All Benders' recruiting speed +10%", "image": "/static/icons/governor.png"},
    "Prefect":   {"effects": "Research Speed +10%",     "image": "/static/icons/prefect.png"}
}
ORDERED_TITLES = list(TITLES_CATALOG.keys())
REQUESTABLE = {t for t in ORDERED_TITLES if t != "Guardian of Harmony"}
ICON_FILES = {name: data.get('image') for name, data in TITLES_CATALOG.items()}

# -------------------- Time + helpers --------------------
def now_utc() -> datetime:
    return datetime.now(UTC)

def parse_iso_utc(s: str | None) -> Optional[datetime]:
    """Parse ISO and return UTC-aware dt or None; tolerant of naive inputs."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None

def normalize_slot_dt(dt: datetime) -> datetime:
    """Normalize a slot start to a zeroed-seconds UTC timestamp."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    return dt.replace(second=0, microsecond=0)

def iso_slot_key_naive(dt: datetime) -> str:
    """Legacy key used by JSON state."""
    return normalize_slot_dt(dt).strftime("%Y-%m-%dT%H:%M:%S")

# -------------------- DB URL normalization --------------------
def _normalize_db_uri(raw: str | None) -> str:
    if not raw:
        return "sqlite:///instance/app.db"
    uri = raw.strip()
    if uri.startswith("postgres://"):
        uri = "postgresql+psycopg2://" + uri[len("postgres://"):]
    elif uri.startswith("postgresql://"):
        uri = "postgresql+psycopg2://" + uri[len("postgresql://") :]
    parsed = urlparse(uri)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if parsed.scheme.startswith("postgresql+psycopg2"):
        query.setdefault("sslmode", "require")
    new_query = urlencode(query)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))

# -------------------- State I/O --------------------
def initialize_state():
    with state_lock:
        state.clear()
        state.update({
            'titles': {}, 'config': {}, 'schedules': {},
            'activated_slots': {}, 'sent_reminders': []
        })
    _save_state_unlocked()

def load_state():
    global state
    with state_lock:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    state = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.error("Error loading state file: %s. Re-initializing.", e)
                initialize_state()
        else:
            initialize_state()
        # Backward-compat defaults
        state.setdefault('titles', {})
        state.setdefault('config', {})
        state.setdefault('schedules', {})
        state.setdefault('activated_slots', {})
        state.setdefault('sent_reminders', [])

def _save_state_unlocked():
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, STATE_FILE)
    except IOError as e:
        logger.error("Error saving state file: %s", e)

def save_state():
    with state_lock:
        _save_state_unlocked()

async def save_state_async():
    await asyncio.to_thread(save_state)

def log_to_csv(request_data: dict):
    file_exists = os.path.isfile(CSV_FILE)
    try:
        with open(CSV_FILE, 'a', newline='', encoding='utf-8') as csvfile:
            fieldnames = ['timestamp', 'title_name', 'in_game_name', 'coordinates', 'discord_user']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow({
                'timestamp': request_data.get('timestamp'),
                'title_name': request_data.get('title_name'),
                'in_game_name': request_data.get('in_game_name'),
                'coordinates': request_data.get('coordinates'),
                'discord_user': request_data.get('discord_user'),
            })
    except IOError as e:
        logger.error("Error writing to CSV: %s", e)

def log_action(action: str, **fields):
    try:
        logger.info("[WEB_ACTION] %s %s", action, json.dumps(fields, default=str))
    except Exception:
        logger.info("[WEB_ACTION] %s %s", action, fields)

# -------------------- App-context helper --------------------
@contextmanager
def ensure_app_context():
    """Yield inside a Flask app context if one isn’t already active."""
    try:
        from flask import current_app
        _ = current_app.name
        yield
        return
    except Exception:
        pass
    if APP is not None:
        with APP.app_context():
            yield
    else:
        yield

# -------------------- Multiserver helpers --------------------
def _parse_multi_server_configs() -> dict[int, dict]:
    gids  = (os.getenv("MULTI_GUILD_IDS") or "").strip()
    whs   = (os.getenv("MULTI_WEBHOOK_URLS") or "").strip()
    roles = (os.getenv("MULTI_GUARDIAN_ROLE_IDS") or "").strip()

    out: dict[int, dict] = {}
    if not gids or not whs:
        return out

    gid_list  = [g.strip() for g in gids.split(",") if g.strip()]
    wh_list   = [w.strip() for w in whs.split(",") if w.strip()]
    role_list = [r.strip() for r in roles.split(",")] if roles else []

    if len(wh_list) != len(gid_list):
        logger.warning("MULTI_WEBHOOK_URLS length doesn't match MULTI_GUILD_IDS; ignoring multi-server envs.")
        return out

    for idx, gid_s in enumerate(gid_list):
        try:
            gid = int(gid_s)
        except ValueError:
            logger.warning("Ignoring invalid guild id: %s", gid_s)
            continue
        role_id = None
        if idx < len(role_list) and role_list[idx]:
            try:
                role_id = int(role_list[idx])
            except ValueError:
                logger.warning("Ignoring invalid guardian role id #%d: %s", idx, role_list[idx])
        out[gid] = {"webhook": wh_list[idx], "guardian_role_id": role_id}
    return out

def get_default_guild_id(app: Flask | None = None) -> Optional[int]:
    try:
        with ensure_app_context():
            r = ServerConfig.query.filter_by(is_default=True).first()
            if r:
                return int(r.guild_id)
    except Exception:
        pass
    v = os.getenv("DEFAULT_GUILD_ID")
    if v and v.isdigit():
        return int(v)
    if len(SERVER_CONFIGS) == 1:
        return next(iter(SERVER_CONFIGS.keys()))
    return None

def _choose_server_config(guild_id: int | None):
    if guild_id and guild_id in SERVER_CONFIGS:
        cfg = SERVER_CONFIGS[guild_id]
        return cfg.get("webhook"), cfg.get("guardian_role_id")

    dg = get_default_guild_id()
    if dg and dg in SERVER_CONFIGS:
        cfg = SERVER_CONFIGS[dg]
        return cfg.get("webhook"), cfg.get("guardian_role_id")

    if len(SERVER_CONFIGS) == 1:
        cfg = list(SERVER_CONFIGS.values())[0]
        return cfg.get("webhook"), cfg.get("guardian_role_id")

    # Final single-server env fallback
    env_webhook = os.getenv("WEBHOOK_URL")
    role = None
    rid = os.getenv("GUARDIAN_ROLE_ID")
    if rid:
        try:
            role = int(rid)
        except ValueError:
            logger.warning("GUARDIAN_ROLE_ID is not a valid integer; no role ping.")
    return env_webhook, role


def send_webhook_notification(data, reminder: bool = False, guild_id: int | None = None):
    webhook_url, role_id = _choose_server_config(guild_id)
    if not webhook_url:
        logger.warning("No webhook configured; skipping notification.")
        return

    role_tag = f"<@&{role_id}>" if role_id else ""

    if reminder:
        lead = get_notify_lead_minutes()
        title = f"Reminder: {data.get('title_name','-')} shift starts soon!"
        content = (
            f"{role_tag} The shift for **{data.get('title_name','-')}** "
            f"by **{data.get('in_game_name','-')}** starts in **{lead} minutes**!"
        )
    else:
        title = "New Title Reservation"
        content = f"{role_tag} A new title was reserved via the web form."

    fields = [
        {"name": "Title", "value": data.get('title_name','-'), "inline": True},
        {"name": "In-Game Name", "value": data.get('in_game_name','-'), "inline": True},
        {"name": "Coordinates", "value": data.get('coordinates','-'), "inline": True},
    ]
    if data.get("start_utc"):
        fields.append({"name": "Start (UTC)", "value": data["start_utc"], "inline": True})
    if data.get("end_utc"):
        fields.append({"name": "Ends (UTC)", "value": data["end_utc"], "inline": True})
    fields.append({"name": "Submitted By", "value": data.get('discord_user','Web Form'), "inline": False})
    if data.get("manage_url"):
        fields.append({"name": "Manage", "value": f"[Cancel reservation]({data['manage_url']})", "inline": False})

    payload = {
        "content": content,
        "allowed_mentions": {"parse": ["roles"]},
        "embeds": [{
            "title": title,
            "color": 5814783,
            "fields": fields,
            "timestamp": data.get('timestamp')
        }]
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=8)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error("Webhook send failed: %s", e)

# -------------------- Legacy helpers (DB-mirrored) --------------------
def title_is_vacant_now(title_name: str) -> bool:
    with state_lock:
        t = state.get('titles', {}).get(title_name, {})
        if not t.get('holder'):
            return True
        exp_str = t.get('expiry_date')
    if not exp_str:
        return False
    expiry_dt = parse_iso_utc(exp_str)
    return bool(expiry_dt and now_utc() >= expiry_dt)

def _db_upsert_active_title(title_name: str, ign: str, start_dt: datetime, end_dt: Optional[datetime]):
    if start_dt.tzinfo is None: start_dt = start_dt.replace(tzinfo=UTC)
    if end_dt and end_dt.tzinfo is None: end_dt = end_dt.replace(tzinfo=UTC)
    with ensure_app_context():
        row = ActiveTitle.query.filter_by(title_name=title_name).first()
        if not row:
            row = ActiveTitle(title_name=title_name, holder=ign, claim_at=start_dt, expiry_at=end_dt)
            db.session.add(row)
        else:
            row.holder, row.claim_at, row.expiry_at = ign, start_dt, end_dt
        db.session.commit()

def _db_delete_active_title(title_name: str):
    with ensure_app_context():
        row = ActiveTitle.query.filter_by(title_name=title_name).first()
        if row:
            db.session.delete(row)
            db.session.commit()

# -------------------- Safe shift-hours accessor (works outside request context) --------------------
def _safe_shift_hours(default: int = 12) -> int:
    try:
        with ensure_app_context():
            return int(db_get_shift_hours())
    except Exception:
        return default

def activate_slot(title_name: str, ign: str, start_dt: datetime):
    end_dt = None if title_name == "Guardian of Harmony" else start_dt + timedelta(hours=_safe_shift_hours())
    with state_lock:
        state['titles'].setdefault(title_name, {})
        state['titles'][title_name].update({
            'holder': {'name': ign, 'coords': '-', 'discord_id': 0},
            'claim_date': normalize_slot_dt(start_dt).isoformat(),
            'expiry_date': (None if end_dt is None else normalize_slot_dt(end_dt).isoformat()),
        })
        activated = state.setdefault('activated_slots', {})
        slot_key = iso_slot_key_naive(start_dt)
        activated.setdefault(title_name, {})[slot_key] = True
        _save_state_unlocked()
    try:
        _db_upsert_active_title(title_name, ign, start_dt, end_dt)
    except Exception as e:
        logger.exception("DB upsert ActiveTitle failed: %s", e)

def _scan_expired_titles(now_dt: datetime) -> list[str]:
    expired = []
    with state_lock:
        for title_name, data in state.get('titles', {}).items():
            exp = data.get('expiry_date')
            if data.get('holder') and exp:
                exp_dt = parse_iso_utc(exp)
                if exp_dt and now_dt >= exp_dt:
                    expired.append(title_name)
    return expired

def _release_title_blocking(title_name: str) -> bool:
    with state_lock:
        titles = state.get('titles', {})
        if title_name not in titles:
            return False
        titles[title_name].update({'holder': None, 'claim_date': None, 'expiry_date': None})
        _save_state_unlocked()
    try:
        _db_delete_active_title(title_name)
    except Exception as e:
        logger.exception("DB delete ActiveTitle failed: %s", e)
    return True

# -------------------- Notification settings helpers --------------------
DEFAULT_NOTIFY_TITLES = ["Architect", "General", "Governor", "Prefect"]

def _get_setting_value(key: str, default: str) -> str:
    try:
        with ensure_app_context():
            row = db.session.get(Setting, key)
            if row and (row.value is not None):
                return row.value
    except Exception:
        pass
    return default

def get_notify_enabled() -> bool:
    return _get_setting_value("notify_enabled", "1") in ("1", "true", "True", "yes", "on")

def get_notify_lead_minutes() -> int:
    try:
        return int(_get_setting_value("notify_lead_minutes", "15"))
    except Exception:
        return 15

def get_notify_titles() -> list[str]:
    csv = _get_setting_value("notify_titles", ",".join(DEFAULT_NOTIFY_TITLES))
    titles = [t.strip() for t in csv.split(",") if t.strip()]
    return titles or DEFAULT_NOTIFY_TITLES

# -------------------- Discord (no reminder loop; APS handles reminders) --------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

def snapshot_titles_for_embed():
    with state_lock:
        titles_snapshot = {k: dict(v) for k, v in state.get('titles', {}).items()}
    rows = []
    for title_name in ORDERED_TITLES:
        data = titles_snapshot.get(title_name, {}) or {}
        holder = (data.get('holder') or {}).get('name') or None
        expires_txt = "Never" if (holder and title_name == "Guardian of Harmony") else "—"
        exp_str = data.get('expiry_date')
        if exp_str:
            expiry_dt = parse_iso_utc(exp_str)
            if expiry_dt:
                delta = expiry_dt - now_utc()
                expires_txt = "Expired" if delta.total_seconds() <= 0 else str(timedelta(seconds=int(delta.total_seconds())))
            else:
                expires_txt = "Invalid"
        rows.append((title_name, holder, expires_txt))
    return rows

# Core reservation (used by web + Discord)
def _reserve_slot_core(title_name: str, ign: str, coords: str, start_dt: datetime, source: str, who: str, guild_id: int | None = None):
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=UTC)
    if start_dt <= now_utc():
        raise ValueError("The chosen time is in the past.")
    allowed = set(compute_slots(_safe_shift_hours()))
    if start_dt.strftime("%H:%M") not in allowed:
        raise ValueError(f"Time must be one of {sorted(allowed)} UTC.")
    coords = (coords or "-").strip()
    if coords != "-" and not re.fullmatch(r"\s*\d+\s*:\s*\d+\s*", coords):
        raise ValueError("Coordinates must be like 123:456.")
    slot_dt = normalize_slot_dt(start_dt)
    slot_ts = slot_dt.strftime("%Y-%m-%dT%H:%M:%S")
    slot_key = iso_slot_key_naive(slot_dt)

    with ensure_app_context():
        res = Reservation.query.filter_by(title_name=title_name, slot_dt=slot_dt).first()
        if res:
            if res.ign != ign or ((coords or "-") != (res.coords or "-")):
                raise ValueError(f"Slot already reserved by {res.ign}.")
            if not res.cancel_token:
                res.cancel_token = secrets.token_urlsafe(32)
                db.session.flush()
            cancel_token_value = res.cancel_token
            db.session.commit()
        else:
            cancel_token_value = secrets.token_urlsafe(32)
            res = Reservation(
                title_name=title_name, ign=ign, coords=(coords or "-"),
                slot_dt=slot_dt, slot_ts=slot_ts, cancel_token=cancel_token_value
            )
            db.session.add(res)
            db.session.add(RequestLog(
                timestamp=now_utc().strftime("%Y-%m-%d %H:%M:%S"),
                title_name=title_name, in_game_name=ign,
                coordinates=(coords or "-"), discord_user=who or source
            ))
            db.session.commit()

    manage_url = build_public_url(f"/cancel/{cancel_token_value}") if cancel_token_value else None

    with state_lock:
        sched = state.setdefault("schedules", {}).setdefault(title_name, {})
        if slot_key in sched:
            ex = sched[slot_key]
            ex_ign = ex["ign"] if isinstance(ex, dict) else str(ex)
            if ex_ign != ign:
                raise ValueError(f"Slot already reserved by {ex_ign}.")
        sched[slot_key] = {"ign": ign, "coords": (coords or "-")}
        _save_state_unlocked()

    try:
        end_dt = slot_dt + timedelta(hours=_safe_shift_hours())
        send_webhook_notification({
            "title_name": title_name,
            "in_game_name": ign,
            "coordinates": (coords or "-"),
            "timestamp": now_utc().isoformat(),
            "discord_user": who or source,
            "manage_url": manage_url,
            "start_utc": slot_dt.strftime("%Y-%m-%d %H:%M"),
            "end_utc":   end_dt.strftime("%Y-%m-%d %H:%M"),
        }, reminder=False, guild_id=guild_id)
    except Exception as e:
        logger.error("Immediate notification failed: %s", e)

    try:
        airtable_upsert("reservation", {
            "Title": title_name, "IGN": ign, "Coordinates": (coords or "-"),
            "SlotStartUTC": slot_dt, "SlotEndUTC": end_dt,
            "Source": source, "DiscordUser": who or source,
        })
    except Exception as e:
        logger.error("Airtable upsert failed: %s", e)

# --- Discord commands/cog (no reminder loop) ---
def is_admin_or_manager():
    def predicate(inter: discord.Interaction) -> bool:
        p = inter.user.guild_permissions
        return bool(p.administrator or p.manage_guild)
    return app_commands.check(predicate)

class TitleCog(commands.Cog, name="TitleManager"):
    def __init__(self, bot_instance):
        self.bot = bot_instance
        self.title_check_loop.start()

    async def announce(self, message: str):
        with state_lock:
            channel_id = state.get('config', {}).get('announcement_channel')
        if not channel_id:
            return
        try:
            channel = await self.bot.fetch_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                await channel.send(message)
        except Exception as e:
            logger.error("announce failed: %s", e)

    async def force_release_logic(self, title_name: str, reason: str):
        ok = await asyncio.to_thread(_release_title_blocking, title_name)
        if not ok:
            return
        await self.announce(f"TITLE RELEASED: **'{title_name}'** is now available. Reason: {reason}")
        logger.info("[RELEASE] %s released. Reason: %s", title_name, reason)

    @tasks.loop(seconds=60)
    async def title_check_loop(self):
        now = now_utc()
        to_release = await asyncio.to_thread(_scan_expired_titles, now)
        for title_name in to_release:
            await self.force_release_logic(title_name, "Title expired.")

    @title_check_loop.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

# -------------------- APScheduler job --------------------
def discord_reminder_job():
    """
    Runs every 30s. Sends Discord reminders X minutes before reservations start,
    using notify_* settings. De-dupes via state['sent_reminders'] persisted to disk.
    UTC-safe. Logs errors; never raises.
    """
    try:
        if not get_notify_enabled():
            logger.debug("reminder: disabled")
            return
        lead = get_notify_lead_minutes()
        titles = set(get_notify_titles())
        if not titles:
            logger.debug("reminder: empty titles")
            return

        now = now_utc()
        window_start, window_end = now, now + timedelta(minutes=lead)

        with ensure_app_context():
            rows = (
                Reservation.query
                .filter(Reservation.title_name.in_(list(titles)))
                .filter(Reservation.slot_dt.isnot(None))
                .filter(Reservation.slot_dt > window_start)
                .filter(Reservation.slot_dt <= window_end)
                .order_by(Reservation.slot_dt.asc())
                .all()
            )
        logger.debug("reminder: %d row(s) in window %s..%s for titles=%s",
                     len(rows), window_start.isoformat(), window_end.isoformat(), sorted(titles))

        with state_lock:
            sent_keys = set(state.setdefault('sent_reminders', []))
        to_send = []
        for r in rows:
            slot_dt = r.slot_dt.replace(tzinfo=UTC) if r.slot_dt.tzinfo is None else r.slot_dt.astimezone(UTC)
            key = f"{r.title_name}|{slot_dt.isoformat()}"
            if key in sent_keys:
                continue
            to_send.append((r, key, slot_dt))

        logger.debug("reminder: %d unsent row(s) after de-dupe", len(to_send))
        if not to_send:
            return

        for r, key, slot_dt in to_send:
            send_webhook_notification(
                {
                    "title_name": r.title_name,
                    "in_game_name": r.ign or "-",
                    "coordinates": r.coords or "-",
                    "timestamp": now.isoformat(),
                    "discord_user": "Reminder",
                    "start_utc": slot_dt.strftime("%Y-%m-%d %H:%M"),
                },
                reminder=True,
                guild_id=None
            )
            with state_lock:
                state['sent_reminders'].append(key)
                _save_state_unlocked()
            logger.info("reminder: sent %s", key)
    except Exception as e:
        logger.error("discord_reminder_job failed: %s", e)

# -------------------- Startup plumbing --------------------
def _create_all_with_retry(logger: logging.Logger, attempts: int = 8) -> None:
    delay = 1.0
    for i in range(1, attempts + 1):
        try:
            db.create_all()
            return
        except OperationalError as e:
            logger.error("DB connect/create_all failed (attempt %d/%d): %s", i, attempts, e)
            if i == attempts:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 30)

def _ensure_sqlite_dir(sqlite_uri: str) -> None:
    if not sqlite_uri.startswith("sqlite:"):
        return
    path_part = sqlite_uri.replace("sqlite:///", "", 1)
    is_abs = sqlite_uri.startswith("sqlite:////")
    if is_abs:
        path_part = "/" + path_part
    db_dir = os.path.dirname(os.path.abspath(path_part))
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

def _sqlite_pragmas(dbapi_connection, connection_record):
    try:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.close()
    except Exception:
        pass

def _rehydrate_state_from_db_actives():
    with ensure_app_context():
        rows = ActiveTitle.query.all()
    with state_lock:
        titles = state.setdefault('titles', {})
        for row in rows:
            start = row.claim_at if row.claim_at.tzinfo else row.claim_at.replace(tzinfo=UTC)
            exp = row.expiry_at
            if exp and exp.tzinfo is None:
                exp = exp.replace(tzinfo=UTC)
            titles.setdefault(row.title_name, {})
            titles[row.title_name].update({
                'holder': {'name': row.holder, 'coords': '-', 'discord_id': 0},
                'claim_date': start.isoformat(),
                'expiry_date': (exp.isoformat() if exp else None),
            })
    save_state()

# -------------------- Flask factory --------------------
def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.getenv("FLASK_SECRET", "a-strong-dev-secret-key")
    if app.secret_key == "a-strong-dev-secret-key":
        logger.warning("FLASK_SECRET is using the default dev key. Set FLASK_SECRET for production.")
    if ADMIN_PIN == "letmein":
        logger.warning("ADMIN_PIN is using the default value. Set a secure ADMIN_PIN for production.")

    raw_uri = os.getenv("DATABASE_URL")
    normalized = _normalize_db_uri(raw_uri)
    app.config["SQLALCHEMY_DATABASE_URI"] = normalized
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True, "pool_recycle": 300,
        "pool_size": int(os.getenv("DB_POOL_SIZE", "5")),
        "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "5")),
    }

    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except Exception:
        pass
    if normalized.startswith("sqlite:"):
        _ensure_sqlite_dir(normalized)

    db.init_app(app)

    with app.app_context():
        # register SQLite PRAGMAs only when using SQLite engine
        if db.engine.url.get_backend_name() == "sqlite":
            event.listen(db.engine, "connect", _sqlite_pragmas)

        _create_all_with_retry(logger)

        # Migrations/backfills (idempotent-lite)
        insp = inspect(db.engine)

        try:
            for t in Title.query.all():
                if t.name == "Guardian of Harmony":
                    t.requestable = False
                elif t.requestable is None:
                    t.requestable = True
            db.session.commit()
        except Exception:
            db.session.rollback()

        try:
            cols = [c["name"] for c in insp.get_columns("reservation")]
            if "slot_dt" not in cols:
                db.session.execute(text("ALTER TABLE reservation ADD COLUMN slot_dt TIMESTAMP"))
                db.session.commit()
            db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reservation_slot_dt ON reservation(slot_dt)"))
            db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reservation_title ON reservation(title_name)"))
            db.session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uix_reservation_title_slotdt ON reservation(title_name, slot_dt)"))
            db.session.commit()
        except Exception:
            db.session.rollback()

        try:
            missing = Reservation.query.filter(Reservation.cancel_token.is_(None) | (Reservation.cancel_token == "")).all()
            for r in missing:
                r.cancel_token = secrets.token_urlsafe(32)
            if missing:
                db.session.commit()
        except Exception:
            db.session.rollback()

        try:
            changed = False
            if db.session.get(Setting, "notify_enabled") is None:
                db.session.add(Setting(key="notify_enabled", value="1")); changed = True
            if db.session.get(Setting, "notify_lead_minutes") is None:
                db.session.add(Setting(key="notify_lead_minutes", value="15")); changed = True
            if db.session.get(Setting, "notify_titles") is None:
                db.session.add(Setting(key="notify_titles", value="Architect,General,Governor,Prefect")); changed = True
            if changed:
                db.session.commit()
        except Exception:
            db.session.rollback()

        # Load multi-server configs from DB or env
        try:
            rows = ServerConfig.query.all()
            cfg = {}
            for r in rows:
                try:
                    gid = int(r.guild_id)
                except Exception:
                    continue
                rid = None
                if r.guardian_role_id:
                    try:
                        rid = int(r.guardian_role_id)
                    except Exception:
                        rid = None
                cfg[gid] = {"webhook": r.webhook_url, "guardian_role_id": rid}
            SERVER_CONFIGS.update(cfg or _parse_multi_server_configs())
        except Exception as e:
            SERVER_CONFIGS.update(_parse_multi_server_configs())
            logger.warning("ServerConfig load fallback: %s", e)

        # Register web routes
        if _register_routes is not None:
            _register_routes(
                app=app,
                deps=dict(
                    ORDERED_TITLES=ORDERED_TITLES, TITLES_CATALOG=TITLES_CATALOG,
                    REQUESTABLE=REQUESTABLE, ADMIN_PIN=ADMIN_PIN,
                    ICON_FILES=ICON_FILES,
                    state=state, save_state=save_state_async,
                    log_to_csv=log_to_csv, log_action=log_action,
                    parse_iso_utc=parse_iso_utc, now_utc=now_utc,
                    iso_slot_key_naive=iso_slot_key_naive,
                    title_is_vacant_now=title_is_vacant_now,
                    get_shift_hours=_safe_shift_hours,  # safe accessor
                    bot=bot, state_lock=state_lock,
                    send_webhook_notification=send_webhook_notification,
                    db=db,
                    models=dict(Title=Title, Reservation=Reservation, ActiveTitle=ActiveTitle, RequestLog=RequestLog, Setting=Setting),
                    db_helpers=dict(
                        compute_slots=compute_slots,
                        requestable_title_names=requestable_title_names,
                        title_status_cards=title_status_cards,
                        schedules_by_title=schedules_by_title,
                        set_shift_hours=db_set_shift_hours,
                        schedule_lookup=schedule_lookup,
                    ),
                    reserve_slot_core=_reserve_slot_core,
                    airtable_upsert=airtable_upsert,
                )
            )
        else:
            logger.warning("web_routes.py not found. Public routes were not registered.")

        def schedule_all_upcoming_reminders():
            logger.info("schedule_all_upcoming_reminders placeholder executed (APScheduler runs continuously).")

        register_admin(
            app,
            deps=dict(
                ADMIN_PIN=ADMIN_PIN,
                get_shift_hours=_safe_shift_hours,   # safe accessor
                db_set_shift_hours=db_set_shift_hours,
                send_webhook_notification=send_webhook_notification,
                SERVER_CONFIGS=SERVER_CONFIGS,
                db=db,
                models=dict(
                    Title=Title, Reservation=Reservation, ActiveTitle=ActiveTitle,
                    RequestLog=RequestLog, Setting=Setting, ServerConfig=ServerConfig,
                ),
                db_helpers=dict(
                    compute_slots=compute_slots,
                    requestable_title_names=requestable_title_names,
                    schedule_lookup=schedule_lookup,
                    title_status_cards=title_status_cards,
                ),
                airtable_upsert=airtable_upsert,
            )
        )

        # Lightweight health
        @app.get("/health")
        def health():
            try:
                sched_ok = bool(scheduler and getattr(scheduler, "state", None) == 1)  # 1 == STATE_RUNNING
            except Exception:
                sched_ok = False

            next_run = None
            try:
                job = scheduler.get_job("discord_reminder_job") if scheduler else None
                next_run = job.next_run_time.isoformat() if job and job.next_run_time else None
            except Exception:
                pass

            return {
                "ok": True,
                "ts": now_utc().isoformat(),
                "db": db.engine.url.get_backend_name(),
                "servers": len(SERVER_CONFIGS),
                "server_ids": list(SERVER_CONFIGS.keys()),
                "scheduler_running": sched_ok,
                "reminder_next_run": next_run,
            }, 200

        # --- APScheduler boot (non-blocking) ---
        global scheduler
        if scheduler is None:
            scheduler = BackgroundScheduler(timezone="UTC")
            scheduler.add_job(
                discord_reminder_job,
                trigger=IntervalTrigger(seconds=30),
                id="discord_reminder_job",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
            scheduler.start()
            atexit.register(lambda: scheduler.shutdown(wait=False))
            logger.info("Started APScheduler Discord reminder job (every 30s, UTC)")

    global APP
    APP = app
    return app

# -------------------- Discord lifecycle --------------------
@bot.event
async def on_ready():
    load_state()
    try:
        _rehydrate_state_from_db_actives()
    except Exception as e:
        logger.exception("Rehydrate from DB ActiveTitle failed: %s", e)

    if not bot.get_cog("TitleManager"):
        await bot.add_cog(TitleCog(bot))

    try:
        # Keep slash command tree minimal for stability; add more as needed
        await bot.tree.sync()
    except Exception as e:
        logger.error("Slash sync failed: %s", e)

    logger.info('%s connected to Discord', bot.user.name)

# -------------------- Entrypoint --------------------
def run_flask_app(app: Flask):
    port = int(os.getenv("PORT", "10000"))
    threads = int(os.getenv("WAITRESS_THREADS", "8"))
    logger.info("Starting Flask server on port %d with %d threads", port, threads)
    serve(app, host='0.0.0.0', port=port, threads=threads)

if __name__ == "__main__":
    app = create_app()

    token = DISCORD_TOKEN
    if token:
        def _run_bot():
            try:
                bot.run(token)
            except LoginFailure:
                logger.exception("Discord login failed (bad token). Web continues.")
            except Exception:
                logger.exception("Discord bot crashed unexpectedly. Web continues.")
        Thread(target=_run_bot, daemon=True).start()
    else:
        logger.warning("DISCORD_TOKEN missing; running web only.")

    run_flask_app(app)