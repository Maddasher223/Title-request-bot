# web_routes.py — Public Flask routes (dashboard + booking)

import os
import csv
import asyncio
from datetime import datetime, timedelta, timezone
from flask import render_template, request, redirect, url_for, flash, session

def register_routes(app, deps):
    """
    Expected deps:
      ORDERED_TITLES, TITLES_CATALOG, ICON_FILES, REQUESTABLE, ADMIN_PIN,
      state, save_state, log_action, log_to_csv, send_webhook_notification,
      parse_iso_utc, now_utc, iso_slot_key_naive, title_is_vacant_now,
      get_shift_hours,
      # optional
      bot,
      db_helpers (dict with set_shift_hours, compute_slots, requestable_title_names,
                  title_status_cards, schedule_lookup),
      send_to_log_channel (async func),
      reserve_slot_core (callable: title, ign, coords, start_dt, source, who, guild_id)
    """
    # ----- Unpack deps (robustly) -----
    ORDERED_TITLES = deps['ORDERED_TITLES']
    TITLES_CATALOG = deps['TITLES_CATALOG']
    ICON_FILES     = deps.get('ICON_FILES', {})        # {title_name: "/static/icons/..png"}
    REQUESTABLE    = deps['REQUESTABLE']
    ADMIN_PIN      = deps['ADMIN_PIN']

    state          = deps['state']
    save_state     = deps['save_state']
    log_action     = deps['log_action']
    log_to_csv     = deps['log_to_csv']
    # NOTE: do not call send_webhook_notification here on success; core already does it
    # send_webhook_notification = deps['send_webhook_notification']

    parse_iso_utc  = deps['parse_iso_utc']
    now_utc        = deps['now_utc']
    iso_slot_key_naive = deps['iso_slot_key_naive']
    title_is_vacant_now = deps['title_is_vacant_now']
    get_shift_hours = deps['get_shift_hours']

    reserve_slot_core = deps.get('reserve_slot_core')  # required for DB-backed booking

    bot            = deps.get('bot')
    db_helpers     = deps.get('db_helpers', {}) or {}
    set_shift_hours = deps.get('set_shift_hours') or db_helpers.get('set_shift_hours') or (lambda *_: None)

    # DB helpers with safe fallbacks
    compute_slots = db_helpers.get('compute_slots') or (
        lambda sh: [f"{h:02d}:00" for h in range(0, 24, max(1, int(sh) if str(sh).isdigit() and int(sh) > 0 and 24 % int(sh) == 0 else 12))]
    )
    requestable_title_names = db_helpers.get('requestable_title_names') or (
        lambda: sorted([t for t in ORDERED_TITLES if t != "Guardian of Harmony"])
    )
    title_status_cards = db_helpers.get('title_status_cards')
    schedule_lookup_db = db_helpers.get('schedule_lookup')  # optional DB-driven grid

    # Optional: async logger channel
    async def _noop_log_channel(_bot, _msg):
        return
    send_to_log_channel = deps.get('send_to_log_channel', _noop_log_channel)

    UTC = timezone.utc

    # ----- Utilities -----
    class _ImmediateResult:
        """Tiny wrapper so callers can still call .result(timeout=...) even when we ran sync."""
        def __init__(self, value):
            self._value = value
        def result(self, timeout=None):
            return self._value

    def schedule_on_bot_loop(coro):
        """
        Run an async coroutine safely on the Discord bot loop from Flask thread.
        If the bot/loop isn't available or running, fall back to asyncio.run(),
        returning an object with .result() for compatibility.
        """
        try:
            if bot and getattr(bot, "loop", None) and getattr(bot.loop, "is_running", lambda: False)():
                return asyncio.run_coroutine_threadsafe(coro, bot.loop)
            # Fallback: run synchronously
            return _ImmediateResult(asyncio.run(coro))
        except RuntimeError:
            try:
                loop = asyncio.new_event_loop()
                try:
                    return _ImmediateResult(loop.run_until_complete(coro))
                finally:
                    loop.close()
            except Exception:
                return None
        except Exception:
            return None

    def _reservation_to_ign(reservation):
        return reservation.get('ign') if isinstance(reservation, dict) else (str(reservation) if reservation is not None else "-")

    def _reservation_to_coords(reservation):
        if isinstance(reservation, dict):
            return reservation.get('coords', '-')
        return '-'

    def _safe_parse_iso(k: str):
        """Parse ISO robustly; always return UTC-aware datetime or None."""
        try:
            dt = parse_iso_utc(k)
            if dt is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt
        except Exception:
            pass
        try:
            dt = datetime.fromisoformat(k)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except Exception:
            return None

    # ----- ALWAYS ensure state shape before any request -----
    @app.before_request
    def _ensure_state_keys():
        # Top-level keys
        if 'titles' not in state: state['titles'] = {}
        if 'schedules' not in state: state['schedules'] = {}
        if 'config' not in state: state['config'] = {}
        if 'sent_reminders' not in state: state['sent_reminders'] = []
        if 'activated_slots' not in state: state['activated_slots'] = {}
        if 'approvals' not in state: state['approvals'] = {}
        # Each title entry present
        titles_dict = state['titles']
        for title_name, details in TITLES_CATALOG.items():
            if title_name not in titles_dict:
                titles_dict[title_name] = {
                    'holder': None,
                    'queue': [],
                    'claim_date': None,
                    'expiry_date': None,
                    'pending_claimant': None,
                    'icon': details.get('image'),
                    'buffs': details.get('effects')
                }

    # =========================
    # Public pages
    # =========================
    @app.route("/")
    def dashboard():
        # Prefer DB-driven status cards; fall back to legacy state if helper isn't wired
        if title_status_cards:
            titles_data = title_status_cards()
        else:
            titles_data = []
            titles_dict = state.get('titles', {})
            for title_name in ORDERED_TITLES:
                cat = TITLES_CATALOG.get(title_name, {})
                data = titles_dict.get(title_name, {})
                holder_info = "None"
                if data.get('holder'):
                    holder = data['holder']
                    holder_info = f"{holder.get('name','?')} ({holder.get('coords','-')})"

                remaining = "—"
                if data.get('expiry_date'):
                    try:
                        expiry = _safe_parse_iso(data['expiry_date'])
                        if expiry:
                            delta = expiry - now_utc()
                            remaining = str(timedelta(seconds=int(delta.total_seconds()))) if delta.total_seconds() > 0 else "Expired"
                        else:
                            remaining = "Invalid"
                    except Exception:
                        remaining = "Invalid"

                # Next reservation from legacy state
                next_slot_key = next_ign = None
                sched = state.get('schedules', {}).get(title_name, {})
                future = []
                for k, v in sched.items():
                    dt = _safe_parse_iso(k)
                    if dt and dt >= now_utc():
                        future.append((dt, v))
                if future:
                    future.sort(key=lambda x: x[0])
                    dt, v = future[0]
                    next_slot_key = dt.strftime("%Y-%m-%dT%H:%M:%S")
                    next_ign = _reservation_to_ign(v)

                icon_path = ICON_FILES.get(title_name) or cat.get('image') or ""
                titles_data.append({
                    'name': title_name,
                    'holder': holder_info,
                    'expires_in': remaining,
                    'icon': icon_path,
                    'buffs': cat.get('effects', []),
                    'next_reserved': (f"{next_slot_key} by {next_ign}" if (next_slot_key and next_ign) else "—")
                })

        today = now_utc().date()
        days = [(today + timedelta(days=i)) for i in range(7)]

        # Slots and requestable titles from DB helpers when present
        shift = int(get_shift_hours())
        hours = compute_slots(shift)             # e.g., 00:00, 04:00, 08:00, ...
        requestable = requestable_title_names()  # DB-truth, excludes unrequestable
        cfg = state.get('config', {})

        # Build schedule grid
        if schedule_lookup_db:
            schedule_grid = schedule_lookup_db(days, hours)
        else:
            allowed_hours = set(hours)
            schedule_grid = {}
            for title_name, sched in state.get('schedules', {}).items():
                for k, v in sched.items():
                    dt = _safe_parse_iso(k)
                    if not dt:
                        continue
                    dkey = dt.date().isoformat()
                    tkey = dt.strftime("%H:%M")
                    if tkey not in allowed_hours:
                        continue
                    day_map = schedule_grid.setdefault(dkey, {})
                    time_map = day_map.setdefault(tkey, {})
                    if isinstance(v, dict):
                        time_map[title_name] = {"ign": v.get("ign", "-"), "coords": v.get("coords", "-")}
                    else:
                        time_map[title_name] = {"ign": str(v), "coords": "-"}

        return render_template(
            'dashboard.html',
            titles=titles_data,
            days=days,
            hours=hours,
            schedule_lookup=schedule_grid,
            today=today.strftime('%Y-%m-%d'),
            requestable_titles=requestable,
            shift_hours=get_shift_hours(),
            config=cfg
        )

    @app.route("/log")
    def view_log():
        csv_path = os.path.join(os.path.dirname(__file__), "data", "requests.csv")
        log_data = []
        try:
            if os.path.exists(csv_path):
                with open(csv_path, 'r', newline='', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    log_data = list(reader)
        except Exception:
            log_data = []
        return render_template('log.html', logs=log_data[::-1])

    @app.route("/book-slot", methods=['POST'])
    def book_slot():
        # --- form data
        title_name = (request.form.get('title') or '').strip()
        ign        = (request.form.get('ign') or '').strip()
        coords     = (request.form.get('coords') or '').strip()
        date_str   = (request.form.get('date') or '').strip()
        time_str   = (request.form.get('time') or '').strip()

        if not all([title_name, ign, date_str, time_str]):
            flash("Missing form data: title, IGN, date, and time are required.")
            return redirect(url_for("dashboard"))

        # Verify DB-requestable titles
        _req_titles = set(requestable_title_names())
        if title_name not in _req_titles:
            flash("This title cannot be requested.")
            return redirect(url_for("dashboard"))

        # Ensure time matches current slot grid
        _shift = int(get_shift_hours())
        _allowed = set(compute_slots(_shift))
        if time_str not in _allowed:
            flash(f"Time must be one of {sorted(_allowed)} UTC.")
            return redirect(url_for("dashboard"))

        # Parse and validate time
        try:
            start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        except ValueError:
            flash("Time must be HH:MM (24h), e.g., 12:00.")
            return redirect(url_for("dashboard"))
        if start_dt <= now_utc():
            flash("Cannot schedule a time in the past.")
            return redirect(url_for("dashboard"))

        # --- Persist via DB core (handles uniqueness, webhook, airtable, request log)
        try:
            reserve_slot_core(
                title_name,
                ign.strip(),
                (coords or "-").strip(),
                start_dt,
                source="Web Form",
                who="Web Form",
                guild_id=None,   # DB/server-config logic will pick default
            )
        except ValueError as e:
            flash(str(e))
            return redirect(url_for("dashboard"))
        except Exception:
            flash("Internal error while booking. Please try again.")
            return redirect(url_for("dashboard"))

        # Optional CSV mirror for legacy log page
        csv_data = {
            "timestamp": now_utc().isoformat(),
            "title_name": title_name,
            "in_game_name": ign,
            "coordinates": coords or "-",
            "discord_user": "Web Form"
        }
        try:
            log_to_csv(csv_data)
        except Exception:
            pass

        # Optional bot log channel
        try:
            schedule_on_bot_loop(send_to_log_channel(bot, f"[SCHEDULE:WEB] reserved {title_name} for {ign} @ {date_str} {time_str} UTC"))
        except Exception:
            pass

        flash(f"Reserved {title_name} for {ign} on {date_str} at {time_str} UTC.")
        return redirect(url_for("dashboard"))