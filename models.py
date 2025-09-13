# models.py

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import ForeignKey

db = SQLAlchemy()

# ---------------- Titles ----------------
class Title(db.Model):
    __tablename__ = "titles"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String, unique=True, nullable=False, index=True)
    icon_url = db.Column(db.String)
    requestable = db.Column(db.Boolean, default=True)

    def __repr__(self):
        return f"<Title {self.name} requestable={self.requestable}>"

# ---------------- Active titles (live holders) ----------------
class ActiveTitle(db.Model):
    __tablename__ = "active_title"
    id = db.Column(db.Integer, primary_key=True)
    title_name = db.Column(db.String, ForeignKey("titles.name"), nullable=False, unique=True, index=True)
    holder = db.Column(db.String(80), nullable=False)
    claim_at = db.Column(db.DateTime(timezone=True), nullable=False)   # UTC aware
    expiry_at = db.Column(db.DateTime(timezone=True), nullable=True)   # None for Harmony

    def __repr__(self):
        return f"<ActiveTitle {self.title_name} holder={self.holder}>"

# ---------------- Reservations (calendar) ----------------
class Reservation(db.Model):
    __tablename__ = "reservation"  # singular; matches main.py SQL
    id = db.Column(db.Integer, primary_key=True)

    title_name = db.Column(db.String(120), nullable=False, index=True)
    ign = db.Column(db.String(120), nullable=False)
    coords = db.Column(db.String(64), nullable=True)

    # Legacy string timestamp (kept for compatibility)
    slot_ts = db.Column(db.String(32), nullable=True)

    # Canonical slot datetime (UTC, seconds=0), timezone-aware for Postgres safety
    slot_dt = db.Column(db.DateTime(timezone=True), index=True)

    # Token used for self-serve cancellations
    cancel_token = db.Column(db.String(64), unique=True, index=True)

    def __repr__(self):
        return f"<Reservation {self.title_name} {self.ign} {self.slot_dt}>"

    # NOTE:
    # Unique & index DDL is created idempotently in main.py:
    # - UNIQUE (title_name, slot_dt) via uix_reservation_title_slotdt
    # - ix_reservation_slot_dt, ix_reservation_title
    # We omit __table_args__ here to avoid name clashes across engines.

# ---------------- Web form / Discord request log ----------------
class RequestLog(db.Model):
    __tablename__ = "request_log"
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.String, nullable=False)  # ISO8601 string
    title_name = db.Column(db.String, nullable=False)
    in_game_name = db.Column(db.String, nullable=False)
    coordinates = db.Column(db.String)
    discord_user = db.Column(db.String)

    def __repr__(self):
        return f"<RequestLog {self.timestamp} {self.title_name} {self.in_game_name}>"

# ---------------- Server / multi-guild settings ----------------
class ServerConfig(db.Model):
    __tablename__ = "server_config"
    guild_id = db.Column(db.String, primary_key=True)
    webhook_url = db.Column(db.String, nullable=False)
    guardian_role_id = db.Column(db.String, nullable=True)
    is_default = db.Column(db.Boolean, default=False)

    def __repr__(self):
        return f"<ServerConfig guild={self.guild_id} default={self.is_default}>"

    @classmethod
    def clear_default(cls):
        db.session.query(cls).update({cls.is_default: False})
        db.session.commit()

# ---------------- Key/value app settings ----------------
class Setting(db.Model):
    __tablename__ = "setting"  # matches main.py lookups (db.session.get(Setting, ...))
    key = db.Column(db.String, primary_key=True)
    value = db.Column(db.String, nullable=False)

    def __repr__(self):
        return f"<Setting {self.key}={self.value}>"

    @classmethod
    def set(cls, key, val):
        row = db.session.get(cls, key)
        if not row:
            row = cls(key=key, value=str(val))
            db.session.add(row)
        else:
            row.value = str(val)
        db.session.commit()