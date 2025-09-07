# main.py — CORE + DISCORD BOT + APP SETUP (routes registered from web_routes.py)

from __future__ import annotations

import os
import csv
import json
import logging
import asyncio
import re
import requests
import secrets
import time
from threading import Thread, RLock
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from flask import Flask
from waitress import serve

import discord
from discord.ext import commands, tasks
from discord.errors import LoginFailure
from discord import app_commands

from dotenv import load_dotenv
load_dotenv()

# ===== Optional Airtable (safe import) =====
try:
    from pyairtable import Api
except Exception:
    Api = None

# ===== Models / DB helpers =====
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

from web_routes import register_routes
from admin_routes import register_admin

# ========= Logging =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s:%(levelname)s:%(name)s: %(message)s")
logger = logging.getLogger("app")

# ========= UTC helpers & constants =========
UTC = timezone.utc
SHIFT_HOURS = 12  # default shift window

def now_utc() -> datetime:
    return datetime.now(UTC)

def parse_iso_utc(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None

def iso_slot_key_naive(dt: datetime) -> str:
    """Naive ISO key 'YYYY-MM-DDTHH:MM:SS' in UTC (no tzinfo, :00 seconds)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt.replace(second=0, microsecond=0).isoformat()

def to_iso_utc(val) -> str:
    if isinstance(val, datetime):
        dt = val
    else:
        dt = parse_iso_utc(str(val)) or now_utc()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()

def normalize_slot_dt(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(second=0, microsecond=0)

# ========= Static Titles (local icons) =========
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
    "Architect": {
        "effects": "Construction Speed +10%",
        "image": "/static/icons/architect.png"
    },
    "General": {
        "effects": "All benders' ATK +5%",
        "image": "/static/icons/general.png"
    },
    "Governor": {
        "effects": "All Benders' recruiting speed +10%",
        "image": "/static/icons/governor.png"
    },
    "Prefect": {
        "effects": "Research Speed +10%",
        "image": "/static/icons/prefect.png"
    }
}

ORDERED_TITLES = list(TITLES_CATALOG.keys())
REQUESTABLE = {t for t in ORDERED_TITLES if t != "Guardian of Harmony"}

# ========= Environment & Config =========
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
ADMIN_PIN = os.getenv("ADMIN_PIN", "letmein")
DISCORD_TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip()
FLASK_SECRET = os.getenv("FLASK_SECRET", "a-strong-dev-secret-key")
GUARDIAN_ROLE_ID = os.getenv("GUARDIAN_ROLE_ID")

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
if not PUBLIC_BASE_URL:
    logger.info("PUBLIC_BASE_URL not set; manage/cancel links will be omitted from notifications.")

def build_public_url(path: str) -> Optional[str]:
    if not PUBLIC_BASE_URL:
        return None
    return f"{PUBLIC_BASE_URL}{path}"

# ========= Discord setup (must exist before route registration) =========
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ========= Persistence & Thread Safety (legacy JSON/CSV) =========
BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
STATIC_DIR = os.path.join(BASE_DIR, "static", "icons")
os.makedirs(STATIC_DIR, exist_ok=True)

STATE_FILE = os.path.join(DATA_DIR, "titles_state.json")
CSV_FILE   = os.path.join(DATA_DIR, "requests.csv")

state: dict = {}
state_lock = RLock()

def initialize_state():
    global state
    state = {
        "titles": {},
        "config": {},
        "schedules": {},
        "sent_reminders": [],
        "activated_slots": {},
    }
    save_state()

def initialize_titles():
    with state_lock:
        titles = state.setdefault("titles", {})
        for title_name in TITLES_CATALOG.keys():
            titles.setdefault(title_name, {"holder": None, "claim_date": None, "expiry_date": None})
    save_state()

def load_state():
    global state
    with state_lock:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    state = json.load(f)
                state.setdefault("titles", {})
                state.setdefault("config", {})
                state.setdefault("schedules", {})
                state.setdefault("activated_slots", {})
            except Exception as e:
                logger.error("Error loading state file: %s. Re-initializing.", e)
                initialize_state()
        else:
            initialize_state()

def _save_state_unlocked():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=4)
    os.replace(tmp, STATE_FILE)

def save_state():
    with state_lock:
        try:
            _save_state_unlocked()
        except Exception as e:
            logger.error("Error saving state file: %s", e)

async def save_state_async():
    await asyncio.to_thread(save_state)

def log_to_csv(request_data: dict):
    file_exists = os.path.isfile(CSV_FILE)
    try:
        with open(CSV_FILE, "a", newline="", encoding="utf-8") as csvfile:
            fieldnames = ["timestamp", "title_name", "in_game_name", "coordinates", "discord_user"]
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow({
                "timestamp": request_data.get("timestamp"),
                "title_name": request_data.get("title_name"),
                "in_game_name": request_data.get("in_game_name"),
                "coordinates": request_data.get("coordinates"),
                "discord_user": request_data.get("discord_user"),
            })
    except IOError as e:
        logger.error("Error writing to CSV: %s", e)

def log_action(action: str, **fields):
    try:
        logger.info("[WEB_ACTION] %s %s", action, json.dumps(fields, default=str))
    except Exception:
        logger.info("[WEB_ACTION] %s %s", action, fields)

# ========= Airtable helper =========
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE = os.getenv("AIRTABLE_TABLE", "TitleLog")

airtable_table = None
if Api and AIRTABLE_API_KEY and AIRTABLE_BASE_ID:
    try:
        api = Api(AIRTABLE_API_KEY)
        airtable_table = api.table(AIRTABLE_BASE_ID, AIRTABLE_TABLE)
    except Exception as e:
        logger.warning("Airtable not configured: %s", e)

def airtable_upsert(record_type: str, payload: dict):
    if not airtable_table:
        return
    fields = {
        "Type": record_type,
        "Title": payload.get("Title"),
        "IGN": payload.get("IGN"),
        "Coordinates": payload.get("Coordinates"),
        "SlotStartUTC": None,
        "SlotEndUTC": None,
        "Source": payload.get("Source"),
        "DiscordUser": payload.get("DiscordUser"),
    }
    if payload.get("SlotStartUTC"):
        fields["SlotStartUTC"] = to_iso_utc(payload["SlotStartUTC"])
    if payload.get("SlotEndUTC"):
        fields["SlotEndUTC"] = to_iso_utc(payload["SlotEndUTC"])
    try:
        airtable_table.create(fields)
    except Exception as e:
        logger.error("Airtable create failed: %s", e)

# ========= Multi-server config =========
def _parse_multi_server_configs() -> Dict[int, Dict]:
    gids  = (os.getenv("MULTI_GUILD_IDS") or "").strip()
    whs   = (os.getenv("MULTI_WEBHOOK_URLS") or "").strip()
    roles = (os.getenv("MULTI_GUARDIAN_ROLE_IDS") or "").strip()

    server_configs: Dict[int, Dict] = {}
    if not (gids and whs):
        return server_configs

    gid_list  = [g.strip() for g in gids.split(",") if g.strip()]
    wh_list   = [w.strip() for w in whs.split(",") if w.strip()]
    role_list = [r.strip() for r in roles.split(",")] if roles else []

    if len(wh_list) != len(gid_list):
        logger.warning("MULTI_WEBHOOK_URLS length doesn't match MULTI_GUILD_IDS; ignoring multi-server envs.")
        return server_configs

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
                logger.warning("Ignoring invalid guardian role id at position %d: %s", idx, role_list[idx])
        server_configs[gid] = {"webhook": wh_list[idx], "guardian_role_id": role_id}
    return server_configs

SERVER_CONFIGS: Dict[int, Dict] = {}

# ========= Safe DB URL normalization =========
def _normalize_db_uri(raw: str | None) -> str:
    if not raw:
        return "sqlite:///instance/app.db"

    uri = raw.strip()
    if uri.startswith("postgres://"):
        uri = "postgresql+psycopg2://" + uri[len("postgres://"):]
    elif uri.startswith("postgresql://"):
        uri = "postgresql+psycopg2://" + uri[len("postgresql://"):]

    parsed = urlparse(uri)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))

    if parsed.scheme.startswith("postgresql+psycopg2"):
        query.setdefault("sslmode", "require")
        hostaddr = os.getenv("DB_HOSTADDR", "").strip()
        if hostaddr:
            query["hostaddr"] = hostaddr

    new_query = urlencode(query)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))

# ========= Flask app factory =========
def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.getenv("FLASK_SECRET", FLASK_SECRET)

    os.makedirs(app.instance_path, exist_ok=True)

    app.config["SQLALCHEMY_DATABASE_URI"] = _normalize_db_uri(os.getenv("DATABASE_URL"))
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
        "pool_size": int(os.getenv("DB_POOL_SIZE", "5")),
        "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "5")),
    }

    db.init_app(app)

    # /health — lightweight (no hard DB call)
    @app.get("/health")
    def health():
        uri = urlparse(app.config["SQLALCHEMY_DATABASE_URI"])
        return {
            "ok": True,
            "ts": now_utc().isoformat(),
            "db_scheme": uri.scheme,
            "db_has_hostaddr": "hostaddr" in dict(parse_qsl(uri.query)),
            "servers": len(SERVER_CONFIGS),
            "server_ids": list(SERVER_CONFIGS.keys()),
        }, 200

    # SQLite PRAGMAs if applicable
    def _sqlite_pragmas(dbapi_connection, _record):
        try:
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA synchronous=NORMAL;")
            cur.close()
        except Exception:
            pass

    with app.app_context():
        if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:"):
            event.listen(db.engine, "connect", _sqlite_pragmas)

        _create_all_with_retry(app.logger)

        # --- Post-create migrations/backfills (idempotent) ---
        _post_create_migrations()

        # Populate multi-server configs (DB first, then env)
        cfg = _load_server_configs_from_db()
        SERVER_CONFIGS.update(cfg if cfg else _parse_multi_server_configs())
        logger.info("Loaded %d server config(s): %s", len(SERVER_CONFIGS), list(SERVER_CONFIGS.keys()))

    # Register blueprints/routes (after DB ready)
    ICON_FILES = {name: data.get("image") for name, data in TITLES_CATALOG.items()}
    register_routes(
        app=app,
        deps=dict(
            ORDERED_TITLES=ORDERED_TITLES, TITLES_CATALOG=TITLES_CATALOG,
            REQUESTABLE=REQUESTABLE, ADMIN_PIN=ADMIN_PIN,
            ICON_FILES=ICON_FILES,
            state=state, save_state=save_state_async,
            log_to_csv=log_to_csv, log_action=log_action,
            parse_iso_utc=parse_iso_utc, now_utc=now_utc,
            iso_slot_key_naive=iso_slot_key_naive, title_is_vacant_now=title_is_vacant_now,
            get_shift_hours=_safe_shift_hours, bot=bot, state_lock=state_lock,
            send_webhook_notification=send_webhook_notification,
            db=db,
            models=dict(Title=Title, Reservation=Reservation, ActiveTitle=ActiveTitle, RequestLog=RequestLog, Setting=Setting),
            db_helpers=dict(
                compute_slots=compute_slots, requestable_title_names=requestable_title_names,
                title_status_cards=title_status_cards, schedules_by_title=schedules_by_title,
                set_shift_hours=db_set_shift_hours, schedule_lookup=schedule_lookup,
            ),
            reserve_slot_core=_reserve_slot_core, airtable_upsert=airtable_upsert,
        )
    )

    register_admin(
        app,
        deps=dict(
            ADMIN_PIN=ADMIN_PIN, get_shift_hours=_safe_shift_hours, db_set_shift_hours=db_set_shift_hours,
            send_webhook_notification=send_webhook_notification, SERVER_CONFIGS=SERVER_CONFIGS,
            log_action=log_action, db=db,
            models=dict(Title=Title, Reservation=Reservation, ActiveTitle=ActiveTitle, RequestLog=RequestLog, Setting=Setting, ServerConfig=ServerConfig),
            db_helpers=dict(compute_slots=compute_slots, requestable_title_names=requestable_title_names,
                            schedule_lookup=schedule_lookup, title_status_cards=title_status_cards),
            airtable_upsert=airtable_upsert,
        )
    )

    return app

def _create_all_with_retry(logger_: logging.Logger, attempts: int = 8) -> None:
    delay = 1.0
    for i in range(1, attempts + 1):
        try:
            db.create_all()
            return
        except OperationalError as e:
            logger_.error("DB connect/create_all failed (attempt %d/%d): %s", i, attempts, e)
            if i == attempts:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 30)

def _post_create_migrations():
    insp = inspect(db.engine)
    is_sqlite = db.engine.url.get_backend_name() == "sqlite"

    # Ensure Title.requestable
    try:
        for t in Title.query.all():
            if t.name == "Guardian of Harmony":
                t.requestable = False
            elif t.requestable is None:
                t.requestable = True
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning("Title.requestable backfill skipped: %s", e)

    # reservation.cancel_token column + index
    try:
        cols = [c["name"] for c in insp.get_columns("reservation")]
        if "cancel_token" not in cols:
            db.session.execute(text("ALTER TABLE reservation ADD COLUMN cancel_token VARCHAR(64)"))
            db.session.commit()
        db.session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uix_reservation_cancel_token ON reservation(cancel_token)"))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning("cancel_token migration skipped (non-fatal): %s", e)

    # reservation.slot_dt column + backfill + indexes
    try:
        cols = [c["name"] for c in insp.get_columns("reservation")]
        if "slot_dt" not in cols:
            db.session.execute(text("ALTER TABLE reservation ADD COLUMN slot_dt TIMESTAMP"))
            db.session.commit()

        if is_sqlite:
            db.session.execute(text("""
                UPDATE reservation
                   SET slot_dt = datetime(substr(slot_ts,1,19))
                 WHERE slot_dt IS NULL AND slot_ts IS NOT NULL
            """))
            db.session.commit()

        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reservation_slot_dt ON reservation(slot_dt)"))
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_reservation_title ON reservation(title_name)"))
        db.session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uix_reservation_title_slotdt ON reservation(title_name, slot_dt)"))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning("slot_dt migration/indexing skipped (non-fatal): %s", e)

    # Backfill missing tokens
    try:
        missing_tokens = Reservation.query.filter((Reservation.cancel_token.is_(None)) | (Reservation.cancel_token == "")).all()
        if missing_tokens:
            for r in missing_tokens:
                r.cancel_token = secrets.token_urlsafe(32)
            db.session.commit()
            logger.info("Backfilled cancel_token for %d reservation(s).", len(missing_tokens))
    except Exception as e:
        db.session.rollback()
        logger.warning("Backfill of cancel_token failed (non-fatal): %s", e)

    # One-time bootstrap if empty
    seeded = False
    if Title.query.count() == 0:
        DEFAULT_TITLES = [
            {"name": "Guardian of Harmony", "icon_url": "/static/icons/guardian_harmony.png", "requestable": False},
            {"name": "Guardian of Fire",    "icon_url": "/static/icons/guardian_fire.png",    "requestable": True},
            {"name": "Guardian of Water",   "icon_url": "/static/icons/guardian_water.png",   "requestable": True},
            {"name": "Guardian of Earth",   "icon_url": "/static/icons/guardian_earth.png",   "requestable": True},
            {"name": "Guardian of Air",     "icon_url": "/static/icons/guardian_air.png",     "requestable": True},
            {"name": "Architect",           "icon_url": "/static/icons/architect.png",        "requestable": True},
            {"name": "General",             "icon_url": "/static/icons/general.png",          "requestable": True},
            {"name": "Governor",            "icon_url": "/static/icons/governor.png",         "requestable": True},
            {"name": "Prefect",             "icon_url": "/static/icons/prefect.png",          "requestable": True},
        ]
        for t in DEFAULT_TITLES:
            db.session.add(Title(**t))
        seeded = True

    if db.session.get(Setting, "shift_hours") is None:
        db.session.add(Setting(key="shift_hours", value="12"))
        seeded = True

    if seeded:
        db.session.commit()
        logger.info("Auto-seeded defaults (titles + shift_hours).")

    # Backfill slot_dt for any residual rows
    try:
        missing = Reservation.query.filter(Reservation.slot_dt.is_(None)).all()
        fixed = 0
        for r in missing:
            if not r.slot_ts:
                continue
            dt = parse_iso_utc(r.slot_ts) or None
            if not dt:
                try:
                    dt = datetime.fromisoformat(r.slot_ts)
                except Exception:
                    dt = None
            if not dt:
                continue
            r.slot_dt = normalize_slot_dt(dt)
            r.slot_ts = r.slot_dt.strftime("%Y-%m-%dT%H:%M:%S")
            fixed += 1
        if fixed:
            db.session.commit()
            logger.info("Backfilled slot_dt for %d reservation(s).", fixed)
    except Exception as e:
        db.session.rollback()
        logger.warning("Backfill of slot_dt failed (non-fatal): %s", e)

def _load_server_configs_from_db() -> Dict[int, Dict]:
    try:
        rows = ServerConfig.query.all()
        cfg: Dict[int, Dict] = {}
        for r in rows:
            try:
                gid = int(r.guild_id)
            except Exception:
                continue
            role_id = None
            if r.guardian_role_id:
                try:
                    role_id = int(r.guardian_role_id)
                except Exception:
                    role_id = None
            cfg[gid] = {"webhook": r.webhook_url, "guardian_role_id": role_id}
        return cfg
    except Exception as e:
        logger.warning("Could not load ServerConfig from DB: %s", e)
        return {}

# ========= Runtime helpers that need app context (we reference `app` at runtime) =========
def get_default_guild_id(app: Flask) -> Optional[int]:
    try:
        with app.app_context():
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

def _choose_server_config(app: Flask, guild_id: int | None):
    if guild_id and guild_id in SERVER_CONFIGS:
        cfg = SERVER_CONFIGS[guild_id]
        return cfg.get("webhook"), cfg.get("guardian_role_id")

    dg = get_default_guild_id(app)
    if dg and dg in SERVER_CONFIGS:
        cfg = SERVER_CONFIGS[dg]
        return cfg.get("webhook"), cfg.get("guardian_role_id")
    elif dg:
        logger.debug("Default guild %s not found in SERVER_CONFIGS; continuing.", dg)

    if len(SERVER_CONFIGS) == 1:
        cfg = list(SERVER_CONFIGS.values())[0]
        return cfg.get("webhook"), cfg.get("guardian_role_id")

    if WEBHOOK_URL:
        role_id_val = None
        if GUARDIAN_ROLE_ID:
            try:
                role_id_val = int(GUARDIAN_ROLE_ID)
            except ValueError:
                logger.warning("GUARDIAN_ROLE_ID is not a valid integer; using no role ping.")
        return WEBHOOK_URL, role_id_val

    return None, None

def _safe_shift_hours(app: Flask) -> int:
    try:
        with app.app_context():
            return int(db_get_shift_hours())
    except Exception:
        return SHIFT_HOURS

def send_webhook_notification(app: Flask, data, reminder: bool = False, guild_id: int | None = None):
    webhook_url, role_id = _choose_server_config(app, guild_id)
    if not webhook_url:
        logger.warning("No webhook configured for this event; skipping notification.")
        return

    shift_hours = _safe_shift_hours(app)
    role_tag = f"<@&{role_id}>" if role_id else ""
    if reminder:
        title = f"Reminder: {data.get('title_name','-')} shift starts soon!"
        content = f"{role_tag} The {shift_hours}-hour shift for **{data.get('title_name','-')}** by **{data.get('in_game_name','-')}** starts in 5 minutes!"
    else:
        title = "New Title Reservation"
        content = f"{role_tag} A new title was reserved via the web form."

    fields = [
        {"name": "Title", "value": data.get("title_name", "-"), "inline": True},
        {"name": "In-Game Name", "value": data.get("in_game_name", "-"), "inline": True},
        {"name": "Coordinates", "value": data.get("coordinates", "-"), "inline": True},
        {"name": "Submitted By", "value": data.get("discord_user", "Web Form"), "inline": False},
    ]
    if data.get("manage_url"):
        fields.append({"name": "Manage", "value": f"[Cancel reservation]({data['manage_url']})", "inline": False})

    payload = {
        "content": content,
        "allowed_mentions": {"parse": ["roles"]},
        "embeds": [{
            "title": title,
            "color": 5814783,
            "fields": fields,
            "timestamp": data.get("timestamp")
        }]
    }
    try:
        requests.post(webhook_url, json=payload, timeout=8).raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error("Webhook send failed: %s", e)

def title_is_vacant_now(title_name: str) -> bool:
    with state_lock:
        t = state.get("titles", {}).get(title_name, {})
        if not t.get("holder"):
            return True
        exp_str = t.get("expiry_date")
    if not exp_str:
        return False
    expiry_dt = parse_iso_utc(exp_str)
    return bool(expiry_dt and now_utc() >= expiry_dt)

# ========= Activation / Release (legacy JSON path for auto-activate) =========
def activate_slot(app: Flask, title_name: str, ign: str, start_dt: datetime):
    end_dt = start_dt + timedelta(hours=_safe_shift_hours(app))
    with state_lock:
        state["titles"][title_name].update({
            "holder": {"name": ign, "coords": "-", "discord_id": 0},
            "claim_date": start_dt.isoformat(),
            "expiry_date": None if title_name == "Guardian of Harmony" else end_dt.isoformat(),
        })
        activated = state.setdefault("activated_slots", {})
        already = activated.get(title_name) or {}
        already[iso_slot_key_naive(start_dt)] = True
        activated[title_name] = already
    save_state()

    airtable_upsert("activation", {
        "Title": title_name,
        "IGN": ign,
        "Coordinates": "-",
        "SlotStartUTC": start_dt,
        "SlotEndUTC": None if title_name == "Guardian of Harmony" else end_dt,
        "Source": "Auto-Activate",
        "DiscordUser": "-"
    })

def _scan_expired_titles(now_dt: datetime) -> list[str]:
    expired = []
    with state_lock:
        for title_name, data in state.get("titles", {}).items():
            exp = data.get("expiry_date")
            if data.get("holder") and exp:
                exp_dt = parse_iso_utc(exp)
                if exp_dt and now_dt >= exp_dt:
                    expired.append(title_name)
    return expired

def _release_title_blocking(title_name: str) -> bool:
    with state_lock:
        titles = state.get("titles", {})
        if title_name not in titles:
            return False
        titles[title_name].update({"holder": None, "claim_date": None, "expiry_date": None})
    save_state()
    return True

# ========= Reservation core (DB + JSON mirror + effects) =========
def _reserve_slot_core(app: Flask, title_name: str, ign: str, coords: str, start_dt: datetime,
                       source: str, who: str, guild_id: int | None = None):
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=UTC)
    if start_dt <= now_utc():
        raise ValueError("The chosen time is in the past.")

    allowed = set(compute_slots(_safe_shift_hours(app)))
    hhmm = start_dt.strftime("%H:%M")
    if hhmm not in allowed:
        raise ValueError(f"Time must be one of {sorted(allowed)} UTC.")

    coords = (coords or "-").strip()
    if coords != "-" and not re.fullmatch(r"\s*\d+\s*:\s*\d+\s*", coords):
        raise ValueError("Coordinates must be like 123:456.")

    slot_dt = normalize_slot_dt(start_dt)
    slot_ts = slot_dt.strftime("%Y-%m-%dT%H:%M:%S")
    slot_key = iso_slot_key_naive(slot_dt)

    cancel_token_value: Optional[str] = None
    with app.app_context():
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
            new_token = secrets.token_urlsafe(32)
            res = Reservation(
                title_name=title_name,
                ign=ign,
                coords=(coords or "-"),
                slot_dt=slot_dt,
                slot_ts=slot_ts,
                cancel_token=new_token,
            )
            db.session.add(res)
            db.session.add(RequestLog(
                timestamp=now_utc().strftime("%Y-%m-%d %H:%M:%S"),
                title_name=title_name,
                in_game_name=ign,
                coordinates=(coords or "-"),
                discord_user=who or source,
            ))
            db.session.flush()
            cancel_token_value = new_token
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
    save_state()

    try:
        send_webhook_notification(app, {
            "title_name": title_name,
            "in_game_name": ign,
            "coordinates": (coords or "-"),
            "timestamp": now_utc().isoformat(),
            "discord_user": who or source,
            "manage_url": manage_url,
        }, reminder=False, guild_id=guild_id)
    except Exception:
        pass

    try:
        airtable_upsert("reservation", {
            "Title": title_name, "IGN": ign, "Coordinates": (coords or "-"),
            "SlotStartUTC": slot_dt, "SlotEndUTC": None,
            "Source": source, "DiscordUser": who or source,
        })
    except Exception:
        pass

# ========= Discord Slash UX =========
def is_admin_or_manager():
    def predicate(inter: discord.Interaction) -> bool:
        p = inter.user.guild_permissions
        return bool(p.administrator or p.manage_guild)
    return app_commands.check(predicate)

async def ac_requestable_titles(_interaction: discord.Interaction, current: str):
    try:
        text_filter = (current or "").lower()
        with APP.app_context():
            names = sorted(requestable_title_names())
        if text_filter:
            names = [t for t in names if text_filter in t.lower()]
        return [app_commands.Choice(name=n, value=n) for n in names[:25]]
    except Exception as e:
        logger.exception("autocomplete(requestable_titles) failed: %s", e)
        return []

async def ac_all_titles(_interaction: discord.Interaction, current: str):
    try:
        text_filter = (current or "").lower()
        names = sorted(ORDERED_TITLES)
        if text_filter:
            names = [t for t in names if text_filter in t.lower()]
        return [app_commands.Choice(name=n, value=n) for n in names[:25]]
    except Exception as e:
        logger.exception("autocomplete(all_titles) failed: %s", e)
        return []

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    logger.exception("App command error for %s: %s", getattr(interaction.command, "name", "?"), error)
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message("⚠️ Something went wrong running that command.", ephemeral=True)
        else:
            await interaction.followup.send("⚠️ Something went wrong running that command.", ephemeral=True)
    except Exception:
        pass

def snapshot_titles_for_embed():
    with state_lock:
        titles_snapshot = {k: dict(v) for k, v in state.get("titles", {}).items()}
    rows = []
    for title_name in ORDERED_TITLES:
        data = titles_snapshot.get(title_name, {}) or {}
        holder = data.get("holder") or {}
        holder_name = holder.get("name") or None
        expires_txt = "Never" if (holder_name and title_name == "Guardian of Harmony") else "—"
        exp_str = data.get("expiry_date")
        if exp_str:
            expiry_dt = parse_iso_utc(exp_str)
            if expiry_dt:
                delta = expiry_dt - now_utc()
                expires_txt = "Expired" if delta.total_seconds() <= 0 else str(timedelta(seconds=int(delta.total_seconds())))
            else:
                expires_txt = "Invalid"
        rows.append((title_name, holder_name, expires_txt))
    return rows

# Groups
titles_group = app_commands.Group(name="titles", description="View and manage temple titles")

@titles_group.command(name="show", description="View current title holders and expiry.")
@app_commands.describe(filter="Filter the list")
@app_commands.choices(filter=[
    app_commands.Choice(name="All", value="all"),
    app_commands.Choice(name="Only Available", value="available"),
    app_commands.Choice(name="Only Held", value="held"),
])
async def titles_show(interaction: discord.Interaction, filter: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True, thinking=True)
    rows = snapshot_titles_for_embed()
    if filter.value == "available":
        rows = [(n, h, e) for (n, h, e) in rows if not h]
    elif filter.value == "held":
        rows = [(n, h, e) for (n, h, e) in rows if h]

    embed = discord.Embed(title="Temple Title Status", color=discord.Color.blurple())
    for name, holder, expires in rows:
        value = f"**Holder:** {holder or '*Available*'}\n**Expires:** {expires}"
        embed.add_field(name=name, value=value, inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)

def _time_choices():
    return [app_commands.Choice(name="00:00 UTC", value="00:00"),
            app_commands.Choice(name="12:00 UTC", value="12:00")]

@titles_group.command(name="reserve", description="Reserve a slot for a requestable title.")
@app_commands.describe(
    title="Title to reserve",
    ign="Your in-game name",
    coords="Coordinates (X:Y)",
    date="Date in UTC (YYYY-MM-DD)",
    time="Start time (UTC): 00:00 or 12:00",
)
@app_commands.autocomplete(title=ac_requestable_titles)
@app_commands.choices(time=_time_choices())
@app_commands.checks.cooldown(1, 30.0)
async def titles_reserve(
    interaction: discord.Interaction,
    title: str,
    ign: str | None = None,
    coords: str | None = None,
    date: str | None = None,
    time: app_commands.Choice[str] | None = None,
):
    with APP.app_context():
        valid_titles = set(requestable_title_names())
    if title not in valid_titles:
        return await interaction.response.send_message("❌ That title isn't requestable.", ephemeral=True)

    if not all([ign, coords, date, time]):
        return await interaction.response.send_modal(ReserveModal(title_name=title))

    try:
        start_dt = datetime.strptime(f"{date.strip()} {time.value}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
    except ValueError:
        return await interaction.response.send_message("❌ Invalid date/time. Use YYYY-MM-DD and HH:MM.", ephemeral=True)

    try:
        _reserve_slot_core(APP, title, ign.strip(), (coords or "-").strip(), start_dt,
                           source="Discord Slash", who=str(interaction.user), guild_id=interaction.guild_id)
    except ValueError as e:
        return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
    except Exception:
        return await interaction.response.send_message("⚠️ Internal error while booking. Try again.", ephemeral=True)

    await interaction.response.send_message(
        f"✅ Reserved **{title}** for **{ign.strip()}** on **{date}** at **{time.value} UTC**.",
        ephemeral=True
    )

@titles_group.command(name="release", description="Force release a title (admin only).")
@app_commands.describe(title="Title to release immediately")
@app_commands.autocomplete(title=ac_all_titles)
@is_admin_or_manager()
async def titles_release(interaction: discord.Interaction, title: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    ok = await asyncio.to_thread(_release_title_blocking, title)
    if ok:
        await asyncio.to_thread(airtable_upsert, "release", {
            "Title": title, "Source": "Discord", "DiscordUser": str(interaction.user)
        })
    msg = "✅ Released." if ok else "⚠️ Could not release (unknown title or already free)."
    await interaction.followup.send(msg, ephemeral=True)

shift_group = app_commands.Group(name="shift", description="Manage shift settings")

@shift_group.command(name="set", description="Set shift hours (1-72). Admin only.")
@app_commands.describe(hours="Shift length in hours")
@is_admin_or_manager()
async def shift_set(interaction: discord.Interaction, hours: app_commands.Range[int, 1, 72]):
    with state_lock:
        state.setdefault("config", {})["shift_hours"] = int(hours)
    save_state()
    try:
        with APP.app_context():
            db_set_shift_hours(int(hours))
    except Exception as e:
        logger.error("DB shift set failed: %s", e)
    await interaction.response.send_message(f"🕒 Shift hours updated to **{hours}**.", ephemeral=True)

class ReserveModal(discord.ui.Modal, title="Reserve a Title"):
    def __init__(self, title_name: str):
        super().__init__(timeout=180)
        self.title_name = title_name
        self.ign = discord.ui.TextInput(label="In-Game Name", max_length=64, required=True)
        self.coords = discord.ui.TextInput(label="Coordinates (X:Y)", required=True, max_length=32, placeholder="e.g. 123:456")
        self.date = discord.ui.TextInput(label="Date (UTC) YYYY-MM-DD", required=True, placeholder="YYYY-MM-DD")
        self.time = discord.ui.TextInput(label="Time (UTC) HH:MM (00:00 or 12:00)", required=True, placeholder="00:00 or 12:00")
        self.add_item(self.ign); self.add_item(self.coords); self.add_item(self.date); self.add_item(self.time)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            start_dt = datetime.strptime(f"{self.date.value.strip()} {self.time.value.strip()}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        except ValueError:
            return await interaction.response.send_message("❌ Invalid date/time. Use YYYY-MM-DD and HH:MM.", ephemeral=True)
        try:
            _reserve_slot_core(APP, self.title_name, self.ign.value.strip(), (self.coords.value or "-").strip(),
                               start_dt, source="Discord Modal", who=str(interaction.user), guild_id=interaction.guild_id)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        except Exception:
            return await interaction.response.send_message("⚠️ Internal error while booking. Try again.", ephemeral=True)

        await interaction.response.send_message(
            f"✅ Reserved **{self.title_name}** for **{self.ign.value.strip()}** on **{self.date.value}** at **{self.time.value} UTC**.",
            ephemeral=True
        )

# ========= Bot cog / loops =========
class TitleCog(commands.Cog, name="TitleManager"):
    def __init__(self, bot_instance: commands.Bot, app: Flask):
        self.bot = bot_instance
        self.app = app
        self.title_check_loop.start()

    async def announce(self, message: str):
        with state_lock:
            channel_id = state.get("config", {}).get("announcement_channel")
        if not channel_id:
            return
        try:
            channel = await self.bot.fetch_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                await channel.send(message)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            logger.error("Could not send to announcement channel %s: %s", channel_id, e)

    async def force_release_logic(self, title_name: str, reason: str):
        ok = await asyncio.to_thread(_release_title_blocking, title_name)
        if not ok:
            return
        await self.announce(f"TITLE RELEASED: **'{title_name}'** is now available. Reason: {reason}")
        logger.info("[RELEASE] %s released. Reason: %s", title_name, reason)
        await asyncio.to_thread(airtable_upsert, "release", {"Title": title_name, "Source": "System", "DiscordUser": "-"})

    @tasks.loop(seconds=60)
    async def title_check_loop(self):
        await self.bot.wait_until_ready()
        now = now_utc()

        to_release = await asyncio.to_thread(_scan_expired_titles, now)
        for title_name in to_release:
            await self.force_release_logic(title_name, "Title expired.")

        to_activate: List[tuple[str, str, datetime]] = []
        with state_lock:
            schedules = state.get("schedules", {})
            activated = state.get("activated_slots", {})
            for title_name, slots in schedules.items():
                for slot_key, entry in slots.items():
                    start_dt = parse_iso_utc(slot_key) or datetime.fromisoformat(slot_key).replace(tzinfo=UTC)
                    if start_dt > now:
                        continue
                    if activated.get(title_name, {}).get(slot_key):
                        continue
                    ign = entry["ign"] if isinstance(entry, dict) else str(entry)
                    to_activate.append((title_name, ign, start_dt))
        for title_name, ign, start_dt in to_activate:
            activate_slot(self.app, title_name, ign, start_dt)
            await self.announce(f"AUTO-ACTIVATED: **{title_name}** → **{ign}** (slot start reached).")
            logger.info("[AUTO-ACTIVATE] %s -> %s at %s", title_name, ign, start_dt.isoformat())

    @commands.command(help="List all titles and their current status.")
    async def titles(self, ctx):
        embed = discord.Embed(title="Title Status", color=discord.Color.blue())
        with state_lock:
            for title_name in ORDERED_TITLES:
                data = state["titles"].get(title_name, {})
                status = ""
                if data.get("holder"):
                    holder_name = data["holder"].get("name", "Unknown")
                    if data.get("expiry_date"):
                        expiry = parse_iso_utc(data["expiry_date"])
                        if expiry:
                            remaining = max(0, int((expiry - now_utc()).total_seconds()))
                            status += f"**Held by:** {holder_name}\n*Expires in: {str(timedelta(seconds=int(remaining)))}*"
                        else:
                            status += f"**Held by:** {holder_name}\n*Expiry: Invalid*"
                    else:
                        status += f"**Held by:** {holder_name}\n*Expires: Never*"
                else:
                    status += "**Status:** Available"
                embed.add_field(name=title_name, value=status, inline=False)
        await ctx.send(embed=embed)

    @commands.command(help="Assign a title. Usage: !assign <Title Name> | <In-Game Name>")
    @commands.has_permissions(administrator=True)
    async def assign(self, ctx, *, args: str):
        try:
            title_name, ign = [arg.strip() for arg in args.split("|")]
        except ValueError:
            await ctx.send("Invalid format. Use `!assign <Title Name> | <In-Game Name>`")
            return
        if title_name not in ORDERED_TITLES:
            await ctx.send(f"Title '{title_name}' does not exist.")
            return

        now = now_utc()
        expiry_date_iso = None if title_name == "Guardian of Harmony" else (now + timedelta(hours=_safe_shift_hours(self.app))).isoformat()
        with state_lock:
            state["titles"][title_name].update({
                "holder": {"name": ign, "coords": "-", "discord_id": ctx.author.id},
                "claim_date": now.isoformat(),
                "expiry_date": expiry_date_iso,
            })
        save_state()

        airtable_upsert("assignment", {
            "Title": title_name,
            "IGN": ign,
            "Coordinates": "-",
            "SlotStartUTC": now,
            "SlotEndUTC": expiry_date_iso,
            "Source": "Discord Command",
            "DiscordUser": getattr(ctx.author, "display_name", str(ctx.author)),
        })
        await self.announce(f"SHIFT CHANGE: **{ign}** has been granted **'{title_name}'**.")
        logger.info("[ASSIGN] %s assigned %s -> %s", getattr(ctx.author, "display_name", "admin"), title_name, ign)

    @commands.command(help="Set the announcement channel. Usage: !set_announce <#channel>")
    @commands.has_permissions(administrator=True)
    async def set_announce(self, ctx, channel: discord.TextChannel):
        with state_lock:
            state.setdefault("config", {})["announcement_channel"] = channel.id
        save_state()
        await ctx.send(f"Announcement channel set to {channel.mention}.")

@bot.event
async def on_ready():
    """Init legacy state; add cogs; sync slash."""
    load_state()
    initialize_titles()

    if not bot.get_cog("TitleManager"):
        await bot.add_cog(TitleCog(bot, APP))

    if not any(cmd.name == "titles" for cmd in bot.tree.get_commands()):
        bot.tree.add_command(titles_group)
    if not any(cmd.name == "shift" for cmd in bot.tree.get_commands()):
        bot.tree.add_command(shift_group)

    try:
        synced = await bot.tree.sync()
        logger.info("Synced %d application commands.", len(synced))
    except Exception as e:
        logger.error("Slash sync failed: %s", e)

    logger.info("%s has connected to Discord!", bot.user.name)

# ========= App instance (global) =========
APP = create_app()

# ========= Entrypoint =========
def _start_discord_bot():
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN is empty or missing; running web only.")
        return
    try:
        bot.run(DISCORD_TOKEN)
    except LoginFailure:
        logger.exception("Discord login failed (bad token). Running web only.")
    except Exception:
        logger.exception("Discord bot crashed unexpectedly. Web continues.")

def run_flask_app(app: Flask):
    port = int(os.getenv("PORT", "10000"))
    logger.info("Starting Flask server on port %d", port)
    serve(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    # Preload legacy state files before services start
    load_state()
    initialize_titles()

    # Start Discord bot (non-blocking) if token present
    if DISCORD_TOKEN:
        Thread(target=_start_discord_bot, daemon=True).start()

    # Serve Flask (blocking) with waitress
    run_flask_app(APP)