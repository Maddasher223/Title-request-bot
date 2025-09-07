# seed.py — safe, standalone seeder (no import of main.py)

import os
from pathlib import Path
from flask import Flask
from dotenv import load_dotenv

load_dotenv()

# ---- Build a tiny app just for DB seeding ----
app = Flask(__name__)

# Ensure ./instance exists, or honor DATABASE_URL if provided
repo_root = Path(__file__).resolve().parent
instance_dir = repo_root / "instance"
instance_dir.mkdir(parents=True, exist_ok=True)

# Prefer DATABASE_URL if set (e.g., sqlite:////opt/render/data/app.db on Render)
env_db_url = (os.getenv("DATABASE_URL") or "").strip()

if env_db_url:
    app.config["SQLALCHEMY_DATABASE_URI"] = env_db_url
else:
    # Absolute path is safest for SQLite
    sqlite_path = instance_dir / "app.db"
    # SQLAlchemy wants 4 slashes for absolute unix paths; on Windows 3 is correct
    if os.name == "nt":
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{sqlite_path}"
    else:
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:////{sqlite_path}"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET", "dev-seed-secret")

# Import models and bind db to this tiny app
from models import db, Title, Setting  # noqa: E402

db.init_app(app)

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
        # Update icon if provided and different
        if icon_url is not None and (t.icon_url or "") != icon_url:
            t.icon_url = icon_url
            changed = True
        # Normalize to bool and update if different (handles NULL->True/False)
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


if __name__ == "__main__":
    with app.app_context():
        # Create tables if they don't exist
        db.create_all()

        changed_titles = 0
        for t in DEFAULT_TITLES:
            if upsert_title(t["name"], t["icon_url"], t["requestable"]):
                changed_titles += 1

        changed_settings = 0
        for k, v in DEFAULT_SETTINGS.items():
            if upsert_setting(k, v):
                changed_settings += 1

        db.session.commit()

        uri = app.config["SQLALCHEMY_DATABASE_URI"]
        print(
            "Seed complete.\n"
            f"- Titles added/updated: {changed_titles}\n"
            f"- Settings added/updated: {changed_settings}\n"
            f"- DB URI: {uri}"
        )