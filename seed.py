# seed.py — safe, standalone seeder (no import of main.py)

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from flask import Flask
from dotenv import load_dotenv
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError

load_dotenv()

# ---------- Tiny app just for DB seeding ----------
app = Flask(__name__)

# Ensure ./instance exists for sqlite fallback
repo_root = Path(__file__).resolve().parent
instance_dir = repo_root / "instance"
instance_dir.mkdir(parents=True, exist_ok=True)

def _normalize_db_uri(raw: str | None) -> str:
    """
    - Accepts postgres:// or postgresql:// and upgrades to postgresql+psycopg2://
    - Adds sslmode=require for managed PG (Supabase etc.)
    - Falls back to a sqlite DB in ./instance/app.db
    """
    if not raw:
        sqlite_path = instance_dir / "app.db"
        if os.name == "nt":
            return f"sqlite:///{sqlite_path}"
        return f"sqlite:////{sqlite_path}"

    uri = raw.strip()
    if uri.startswith("postgres://"):
        uri = "postgresql+psycopg2://" + uri[len("postgres://"):]
    elif uri.startswith("postgresql://"):
        uri = "postgresql+psycopg2://" + uri[len("postgresql://"):]

    parsed = urlparse(uri)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if parsed.scheme.startswith("postgresql+psycopg2"):
        query.setdefault("sslmode", "require")
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query), parsed.fragment))

env_db_url = (os.getenv("DATABASE_URL") or "").strip()
app.config["SQLALCHEMY_DATABASE_URI"] = _normalize_db_uri(env_db_url)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET", "dev-seed-secret")

# ---------- Models / DB ----------
from models import db, Title, Setting  # noqa: E402

db.init_app(app)

def _ensure_sqlite_dir(sqlite_uri: str) -> None:
    if not sqlite_uri.startswith("sqlite:"):
        return
    # sqlite:////abs/path.db  or sqlite:///rel/path.db
    path_part = sqlite_uri.replace("sqlite:///", "", 1)
    is_abs = sqlite_uri.startswith("sqlite:////")
    if is_abs:
        path_part = "/" + path_part
    db_dir = os.path.dirname(os.path.abspath(path_part))
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

def _sqlite_pragmas(dbapi_connection, connection_record):
    # Make sqlite a bit more sturdy
    try:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.close()
    except Exception:
        pass

uri = app.config["SQLALCHEMY_DATABASE_URI"]
_ensure_sqlite_dir(uri)

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

DEFAULT_SETTINGS = {"shift_hours": "12"}

def upsert_title(name: str, icon_url: str | None, requestable: bool) -> bool:
    """
    Create the title if missing; otherwise update changed fields.
    Returns True if the DB row was added/modified.
    """
    t = Title.query.filter_by(name=name).first()
    if t:
        changed = False
        if icon_url is not None and (t.icon_url or "") != icon_url:
            t.icon_url = icon_url
            changed = True
        req_bool = bool(requestable)
        if t.requestable is None or bool(t.requestable) != req_bool:
            t.requestable = req_bool
            changed = True
        return changed
    else:
        db.session.add(Title(name=name, icon_url=icon_url, requestable=bool(requestable)))
        return True

def upsert_setting(key: str, value: str) -> bool:
    """
    Create the setting if missing; otherwise update when value changes.
    Returns True if the DB row was added/modified.
    """
    row = db.session.get(Setting, key)
    if row:
        if row.value != value:
            row.value = value
            return True
        return False
    else:
        db.session.add(Setting(key=key, value=value))
        return True

def _mask_db_uri(u: str) -> str:
    try:
        p = urlparse(u)
        if p.password:
            netloc = p.netloc.replace(f":{p.password}@", ":***@")
        else:
            netloc = p.netloc
        return urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))
    except Exception:
        return "(unavailable)"

if __name__ == "__main__":
    with app.app_context():
        # For sqlite, add pragmatic settings
        if uri.startswith("sqlite:"):
            event.listen(db.engine, "connect", _sqlite_pragmas)

        # Create tables if they don't exist
        try:
            db.create_all()
        except SQLAlchemyError as e:
            print("Create tables failed.", file=sys.stderr)
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        changed_titles = 0
        for t in DEFAULT_TITLES:
            if upsert_title(t["name"], t["icon_url"], t["requestable"]):
                changed_titles += 1

        changed_settings = 0
        for k, v in DEFAULT_SETTINGS.items():
            if upsert_setting(k, v):
                changed_settings += 1

        try:
            db.session.commit()
        except SQLAlchemyError as e:
            db.session.rollback()
            print("Seed failed. Rolling back.", file=sys.stderr)
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        print(
            "Seed complete.\n"
            f"- Titles added/updated: {changed_titles}\n"
            f"- Settings added/updated: {changed_settings}\n"
            f"- DB URI: {_mask_db_uri(uri)}"
        )