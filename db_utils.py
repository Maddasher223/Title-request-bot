# db_utils.py — DB helpers used by routes and the Discord task loop

from __future__ import annotations
from datetime import datetime, timezone, date as date_cls, timedelta
from collections import defaultdict
from typing import List, Tuple, Dict, Any
import os

from models import db, Setting, Title, ActiveTitle, Reservation
from sqlalchemy import func

UTC = timezone.utc


# ---------- misc ----------
def ensure_instance_dir(app) -> None:
    """Make sure instance/ exists so sqlite:///instance/app.db can be created."""
    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except Exception:
        # Non-fatal; caller can still proceed (e.g., with non-sqlite backends)
        pass


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso_date(d: date_cls) -> str:
    return d.strftime("%Y-%m-%d")


def _human_duration(td: timedelta) -> str:
    secs = int(td.total_seconds())
    if secs <= 0:
        return "0m"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m or not parts:
        parts.append(f"{m}m")
    return " ".join(parts)


# ---------- Settings ----------
def get_shift_hours(default: int = 12) -> int:
    """Read shift hours; coerce to a safe int; default to 12 on any bad value."""
    row = db.session.get(Setting, "shift_hours")
    if not row or not str(row.value).strip():
        return default
    try:
        hours = int(row.value)
    except Exception:
        return default
    if hours < 1 or hours > 72:
        return default
    return hours


def set_shift_hours(hours: int) -> None:
    """Persist shift hours (validated here)."""
    hours = int(hours)
    if not (1 <= hours <= 72):
        raise ValueError("shift_hours must be between 1 and 72")
    row = db.session.get(Setting, "shift_hours")
    if row:
        row.value = str(hours)
    else:
        db.session.add(Setting(key="shift_hours", value=str(hours)))
    db.session.commit()


# ---------- Scheduling helpers ----------
def compute_slots(shift_hours: int) -> list[str]:
    """
    Return HH:MM starts for a 24h day. If the given shift doesn't divide 24 evenly,
    fall back to 12-hour slots to avoid drift (e.g., 00:00, 12:00).
    """
    try:
        sh = int(shift_hours)
    except Exception:
        sh = 12
    if sh <= 0:
        sh = 12
    if 24 % sh != 0:
        sh = 12
    return [f"{h:02d}:00" for h in range(0, 24, sh)]


def requestable_title_names() -> list[str]:
    """
    Return requestable title names. Uses an explicit (True OR NULL) check to be
    SQLite/Postgres friendly without relying on COALESCE + IS TRUE semantics.
    """
    q = (
        Title.query
        .filter(Title.name != "Guardian of Harmony")
        .filter((Title.requestable.is_(True)) | (Title.requestable.is_(None)))
        .order_by(Title.id.asc())
    )
    return [t.name for t in q.all()]


def all_titles() -> list[Title]:
    return Title.query.order_by(Title.id.asc()).all()


def title_status_cards() -> list[Dict[str, Any]]:
    """
    Returns:
      [{ "name", "icon", "holder", "expires_in", "held_for", "buffs" }, ...]
    Uses ActiveTitle.claim_at / ActiveTitle.expiry_at.
    """
    titles = all_titles()
    active_by_title = {a.title_name: a for a in ActiveTitle.query.all()}
    now = now_utc()

    out: list[Dict[str, Any]] = []
    for t in titles:
        a = active_by_title.get(t.name)
        holder = a.holder if a else None

        # held_for
        held_for = None
        if a and a.claim_at:
            claimed_dt = a.claim_at if a.claim_at.tzinfo else a.claim_at.replace(tzinfo=UTC)
            held_for = _human_duration(now - claimed_dt) if now >= claimed_dt else "0m"

        # expires_in
        expires_in = "—"
        if a:
            if t.name == "Guardian of Harmony" and holder:
                expires_in = "Never"
            elif a.expiry_at:
                exp_dt = a.expiry_at if a.expiry_at.tzinfo else a.expiry_at.replace(tzinfo=UTC)
                delta = exp_dt - now
                expires_in = "Expired" if delta.total_seconds() <= 0 else _human_duration(delta)
            else:
                expires_in = "Does not expire"

        out.append({
            "name": t.name,
            "icon": t.icon_url or "",
            "holder": holder or "-- Available --",
            "expires_in": expires_in,
            "held_for": held_for,
            "buffs": "",  # dashboard template may override from TITLES_CATALOG
        })

    return out


# ---- range-based schedules on slot_dt ----
def _window_bounds_utc(days: list[date_cls]) -> tuple[datetime, datetime]:
    """Given a list of date objects, return inclusive start 00:00Z and exclusive end 00:00Z(+1)."""
    start_day = min(days)
    end_day = max(days) + timedelta(days=1)
    start_dt = datetime(start_day.year, start_day.month, start_day.day, tzinfo=UTC)
    end_dt = datetime(end_day.year, end_day.month, end_day.day, tzinfo=UTC)
    return start_dt, end_dt


def schedules_by_title(days: list[date_cls], hours: list[str]) -> dict[str, dict[str, dict]]:
    """
    Range-query version backed by Reservation.slot_dt (UTC DateTime).
    Returns {title_name: {"YYYY-MM-DDTHH:MM:00": {"ign","coords"}}}
    """
    if not days or not hours:
        return {}

    hours_set = set(hours)
    start_dt, end_dt = _window_bounds_utc(days)

    # Query only reservations inside the visible day window
    rows = (
        Reservation.query
        .filter(Reservation.slot_dt >= start_dt)
        .filter(Reservation.slot_dt < end_dt)
        .all()
    )

    out: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        if not r.slot_dt:
            # legacy rows should have been backfilled; if not, skip
            continue
        # Ensure UTC & :00 seconds
        dt = r.slot_dt if r.slot_dt.tzinfo else r.slot_dt.replace(tzinfo=UTC)
        hhmm = dt.strftime("%H:%M")
        if hhmm not in hours_set:
            continue  # keep grid clean

        key = dt.strftime("%Y-%m-%dT%H:%M:00")
        out[r.title_name][key] = {"ign": r.ign, "coords": (r.coords or "-")}
    return dict(out)


def schedule_lookup(days: list[date_cls], hours: list[str]) -> dict[str, dict[str, dict[str, dict]]]:
    """
    Return {YYYY-MM-DD: {HH:MM: {title: {'ign','coords'}}}}
    Uses schedules_by_title() above (already range-based on slot_dt).
    """
    by_title = schedules_by_title(days, hours)
    out: dict[str, dict[str, dict[str, dict]]] = defaultdict(dict)
    for title, slots in by_title.items():
        for slot_iso, entry in slots.items():
            d_str, t_full = slot_iso.split("T")
            t_key = t_full[:5]
            out.setdefault(d_str, {}).setdefault(t_key, {})[title] = entry
    return dict(out)


# ---------- Title lifecycle (used by admin & bot) ----------
def activate_slot_db(
    title: str,
    ign: str,
    start_dt: datetime,
    set_expiry: bool,
    shift_hours: int | None = None,
) -> None:
    """Upsert ActiveTitle with holder / claim_at / optional expiry_at (UTC-aware)."""
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=UTC)

    exp_dt = None
    if set_expiry:
        hours = shift_hours if shift_hours is not None else get_shift_hours()
        exp_dt = start_dt + timedelta(hours=int(hours))

    row = ActiveTitle.query.filter_by(title_name=title).one_or_none()
    if row:
        row.holder = ign
        row.claim_at = start_dt
        row.expiry_at = exp_dt
    else:
        db.session.add(ActiveTitle(
            title_name=title,
            holder=ign,
            claim_at=start_dt,
            expiry_at=exp_dt,
        ))
    db.session.commit()


def release_title_db(title: str) -> bool:
    row = ActiveTitle.query.filter_by(title_name=title).one_or_none()
    if not row:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


def upcoming_unactivated_reservations(now: datetime) -> List[Tuple[str, str, datetime]]:
    """
    Return reservations whose slot has started (slot_dt <= now) and either:
      - the title is not active, or
      - it's active but claim_at < slot (so this slot hasn't been auto-activated)
    Uses slot_dt (UTC).
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    rows = (
        Reservation.query
        .filter(Reservation.slot_dt <= now)
        .order_by(Reservation.slot_dt.asc())
        .all()
    )
    active = {a.title_name: a for a in ActiveTitle.query.all()}

    out: List[Tuple[str, str, datetime]] = []
    for r in rows:
        if not r.slot_dt:
            continue  # should be backfilled already
        slot_dt = r.slot_dt if r.slot_dt.tzinfo else r.slot_dt.replace(tzinfo=UTC)

        a = active.get(r.title_name)
        if not a:
            out.append((r.title_name, r.ign, slot_dt))
            continue

        claim_at = a.claim_at
        if claim_at and claim_at.tzinfo is None:
            claim_at = claim_at.replace(tzinfo=UTC)

        # If already claimed at or after this slot start, skip; else, needs activation
        if claim_at and claim_at >= slot_dt:
            continue

        out.append((r.title_name, r.ign, slot_dt))

    return out