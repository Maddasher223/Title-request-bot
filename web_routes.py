# web_routes.py — All Flask routes (dashboard + admin) wired by register_routes()

import os
import csv
import asyncio
from datetime import datetime, timedelta, timezone
from flask import render_template, request, redirect, url_for, flash, session

def register_routes(app, deps):
    """
    Expected deps (robust to optional ones):
      ORDERED_TITLES, TITLES_CATALOG, ICON_FILES, REQUESTABLE, ADMIN_PIN,
      state, save_state, log_action, log_to_csv, send_webhook_notification,
      parse_iso_utc, now_utc, iso_slot_key_naive, title_is_vacant_now,
      get_shift_hours, bot,
      # optional
      db_helpers (dict with set_shift_hours, compute_slots, requestable_title_names, title_status_cards, schedule_lookup),
      send_to_log_channel (async func), schedule_lookup (optional)
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
    send_webhook_notification = deps['send_webhook_notification']

    parse_iso_utc  = deps['parse_iso_utc']
    now_utc        = deps['now_utc']
    iso_slot_key_naive = deps['iso_slot_key_naive']
    title_is_vacant_now = deps['title_is_vacant_now']
    get_shift_hours = deps['get_shift_hours']

    bot            = deps.get('bot')
    db_helpers     = deps.get('db_helpers', {}) or {}
    # Optional helpers (no-ops if absent)
    set_shift_hours = deps.get('set_shift_hours') or db_helpers.get('set_shift_hours') or (lambda *_: None)
    schedule_lookup = deps.get('schedule_lookup')  # may be None

    # Helpers from DB (with safe fallbacks)
    compute_slots = db_helpers.get('compute_slots') or (
        lambda sh: [f"{h:02d}:00" for h in range(0, 24, max(1, int(sh) if str(sh).isdigit() and int(sh) > 0 and 24 % int(sh) == 0 else 12))]
    )
    requestable_title_names = db_helpers.get('requestable_title_names') or (
        lambda: sorted([t for t in ORDERED_TITLES if t != "Guardian of Harmony"])
    )
    title_status_cards = db_helpers.get('title_status_cards')

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
            # If somehow we're already in a loop, create a new task runner
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

    def is_admin() -> bool:
        return bool(session.get("is_admin"))

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
                # ensure tz-aware
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

    def _compute_next_reservation_for_title(title_name: str):
        """Return (slot_iso, ign) for the earliest future reservation for a title (legacy state-based)."""
        sched = state.get('schedules', {}).get(title_name, {})
        future = []
        now = now_utc()
        for k, v in sched.items():
            dt = _safe_parse_iso(k)
            if dt and dt >= now:
                future.append((dt, v))
        if not future:
            return None, None
        future.sort(key=lambda x: x[0])
        dt, v = future[0]
        return dt.strftime("%Y-%m-%dT%H:%M:%S"), _reservation_to_ign(v)

    def _get_all_upcoming_reservations(days_ahead: int = 7):
        """Return a list of {title, slot_iso, ign, coords} for the next N days, sorted by time (legacy state-based)."""
        now = now_utc()
        cutoff = now + timedelta(days=days_ahead)
        items = []
        for title_name, sched in state.get('schedules', {}).items():
            for k, v in sched.items():
                dt = _safe_parse_iso(k)
                if dt and (now <= dt <= cutoff):
                    items.append({
                        "title": title_name,
                        "slot_iso": dt.strftime("%Y-%m-%dT%H:%M:%S"),
                        "ign": _reservation_to_ign(v),
                        "coords": _reservation_to_coords(v),
                    })
        items.sort(key=lambda r: r["slot_iso"])
        return items

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

                next_slot_key, next_ign = _compute_next_reservation_for_title(title_name)
                next_res_text = f"{next_slot_key} by {next_ign}" if (next_slot_key and next_ign) else "—"

                icon_path = ICON_FILES.get(title_name) or cat.get('image') or ""
                titles_data.append({
                    'name': title_name,
                    'holder': holder_info,
                    'expires_in': remaining,
                    'icon': icon_path,
                    'buffs': cat.get('effects', []),
                    'next_reserved': next_res_text
                })

        today = now_utc().date()
        days = [(today + timedelta(days=i)) for i in range(7)]

        # Drive slots and requestable titles from DB
        shift = int(get_shift_hours())
        hours = compute_slots(shift)           # e.g., 00:00, 04:00, 08:00, 12:00, ...
        requestable = requestable_title_names()  # DB-truth, excludes unrequestable
        cfg = state.get('config', {})

        allowed_hours = set(hours)

        # { date_iso: { "HH:MM": { title_name: {"ign":..,"coords":..} } } }  (legacy state-backed schedule)
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
        return render_template('log.html', logs=reversed(log_data))

    @app.route("/book-slot", methods=['POST'])
    def book_slot():
        title_name = (request.form.get('title') or '').strip()
        ign        = (request.form.get('ign') or '').strip()
        coords     = (request.form.get('coords') or '').strip()
        date_str   = (request.form.get('date') or '').strip()
        time_str   = (request.form.get('time') or '').strip()

        if not all([title_name, ign, date_str, time_str]):
            flash("Missing form data: title, IGN, date, and time are required.")
            return redirect(url_for("dashboard"))

        # Verify against DB-requestable titles
        _req_titles = set(requestable_title_names())
        if title_name not in _req_titles:
            flash("This title cannot be requested.")
            return redirect(url_for("dashboard"))

        # Ensure the selected time matches the current slot grid
        _shift = int(get_shift_hours())
        _allowed = set(compute_slots(_shift))  # e.g., {"00:00","04:00","08:00","12:00","16:00","20:00"}
        if time_str not in _allowed:
            flash(f"Time must be one of {sorted(_allowed)} UTC.")
            return redirect(url_for("dashboard"))

        try:
            schedule_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        except ValueError:
            flash("Time must be HH:MM (24h), e.g., 12:00.")
            return redirect(url_for("dashboard"))
        if schedule_time < now_utc():
            flash("Cannot schedule a time in the past.")
            return redirect(url_for("dashboard"))

        schedule_key = iso_slot_key_naive(schedule_time)

        # Perform the reservation on the bot loop (single-thread the mutations)
        async def _reserve_and_maybe_assign_now():
            schedules_for_title = state.setdefault('schedules', {}).setdefault(title_name, {})
            if schedule_key in schedules_for_title:
                return False, schedules_for_title[schedule_key]

            # Always store a dict with IGN + coords
            schedules_for_title[schedule_key] = {"ign": ign, "coords": coords or "-"}

            # Log
            log_action('schedule_book_web', title=title_name, time=schedule_key, ign=ign, coords=(coords or '-'))

            # If current slot and vacant, grant now (legacy state path)
            hours = get_shift_hours()
            if (schedule_time <= now_utc() < schedule_time + timedelta(hours=hours)) and title_is_vacant_now(title_name):
                end = schedule_time + timedelta(hours=hours)
                state['titles'].setdefault(title_name, {})
                state['titles'][title_name].update({
                    'holder': {'name': ign, 'coords': coords or '-', 'discord_id': 0},
                    'claim_date': schedule_time.isoformat(),
                    'expiry_date': end.isoformat(),
                    'pending_claimant': None
                })
                log_action('auto_assign_now', title=title_name, ign=ign, start=schedule_time.isoformat())

            await save_state()
            return True, None

        fut = schedule_on_bot_loop(_reserve_and_maybe_assign_now())
        try:
            ok, existing_val = fut.result(timeout=4) if fut else (False, None)
        except Exception:
            ok, existing_val = (False, None)

        if not ok:
            existing_ign = _reservation_to_ign(existing_val) if existing_val is not None else "unknown"
            flash(f"That slot for {title_name} is already reserved by {existing_ign}.")
            return redirect(url_for("dashboard"))

        # CSV + webhook + async log-channel send
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
        try:
            send_webhook_notification(csv_data, reminder=False)
        except Exception:
            pass

        try:
            schedule_on_bot_loop(send_to_log_channel(bot, f"[SCHEDULE:WEB] reserved {title_name} for {ign} @ {date_str} {time_str} UTC"))
        except Exception:
            pass

        flash(f"Reserved {title_name} for {ign} on {date_str} at {time_str} UTC.")
        return redirect(url_for("dashboard"))

    # =========================
    # Admin: session auth
    # =========================
    @app.route("/admin/login", methods=["GET"])
    def admin_login_form():
        return render_template("admin_login.html")

    @app.route("/admin/login", methods=["POST"])
    def admin_login_submit():
        pin = (request.form.get("pin") or "").strip()
        if pin == ADMIN_PIN:
            session["is_admin"] = True
            flash("Welcome, admin.")
            return redirect(url_for("admin_home"))
        flash("Incorrect PIN.")
        return redirect(url_for("admin_login_form"))

    @app.route("/admin/logout", methods=["GET"])
    def admin_logout():
        session.pop("is_admin", None)
        flash("Logged out.")
        return redirect(url_for("dashboard"))

    # =========================
    # Admin: dashboard & actions
    # =========================
    @app.route("/admin", methods=["GET"])
    def admin_home():
        if not is_admin():
            return redirect(url_for("admin_login_form"))

        # Active titles (legacy state view for the small admin page)
        active = []
        titles_dict = state.get('titles', {})
        for title_name in ORDERED_TITLES:
            t = titles_dict.get(title_name, {})
            if t and t.get("holder"):
                exp = _safe_parse_iso(t.get("expiry_date")) if t.get("expiry_date") else None
                active.append({
                    "title": title_name,
                    "holder": t["holder"].get("name", "-"),
                    "coords": t["holder"].get("coords", "-"),
                    "expires": exp.isoformat() if exp else "-"
                })

        # Upcoming reservations + annotate approval flag
        upcoming = _get_all_upcoming_reservations()
        approvals = state.get("approvals", {})
        for item in upcoming:
            t = item["title"]
            k = item["slot_iso"]
            item["approved"] = bool(approvals.get(t, {}).get(k))

        # Recent CSV logs
        csv_path = os.path.join(os.path.dirname(__file__), "data", "requests.csv")
        logs = []
        try:
            if os.path.exists(csv_path):
                with open(csv_path, 'r', newline='', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    logs = list(reader)[-200:]
        except Exception:
            logs = []

        cfg = state.get('config', {})
        current_settings = {
            "announcement_channel": cfg.get("announcement_channel"),
            "log_channel": cfg.get("log_channel"),
            "shift_hours": cfg.get("shift_hours", get_shift_hours()),
        }

        # Provide the list of all titles for the manual assignment form
        all_titles = ORDERED_TITLES

        return render_template(
            "admin.html",
            active_titles=active,
            upcoming=upcoming,
            logs=reversed(logs),
            settings=current_settings,
            all_titles=all_titles
        )

    @app.route("/admin/approve", methods=["POST"])
    def admin_approve():
        if not is_admin():
            return redirect(url_for("admin_login_form"))

        title = (request.form.get("title") or "").strip()
        slot  = (request.form.get("slot") or "").strip()

        async def _approve():
            sched = state.get("schedules", {}).get(title, {})
            if not (title and slot and slot in sched):
                return False, None
            state.setdefault("approvals", {}).setdefault(title, {})[slot] = True
            await save_state()
            return True, sched[slot]

        fut = schedule_on_bot_loop(_approve())
        ok, reserved = (False, None)
        try:
            ok, reserved = fut.result(timeout=3) if fut else (False, None)
        except Exception:
            ok = False

        if not ok:
            flash("Reservation not found")
            return redirect(url_for("admin_home"))

        reserved_ign = _reservation_to_ign(reserved)
        try:
            schedule_on_bot_loop(send_to_log_channel(bot, f"[ADMIN] Approved {title} @ {slot} for {reserved_ign}"))
        except Exception:
            pass

        flash(f"Approved {title} @ {slot}")
        return redirect(url_for("admin_home"))

    @app.route("/admin/cancel", methods=["POST"])
    def admin_cancel():
        if not is_admin():
            return redirect(url_for("admin_login_form"))

        title = (request.form.get("title") or "").strip()
        slot = (request.form.get("slot") or "").strip()

        async def _cancel():
            sched = state.get("schedules", {}).get(title, {})
            if not (title and slot and slot in sched):
                return False, None
            reserved = sched[slot]
            del sched[slot]
            # remove approval flag if present
            ap = state.get("approvals", {}).get(title, {})
            if slot in ap:
                try:
                    del ap[slot]
                except Exception:
                    pass
            await save_state()
            return True, reserved

        fut = schedule_on_bot_loop(_cancel())
        ok, reserved = (False, None)
        try:
            ok, reserved = fut.result(timeout=3) if fut else (False, None)
        except Exception:
            ok = False

        if not ok:
            flash("Reservation not found")
            return redirect(url_for("admin_home"))

        reserved_ign = _reservation_to_ign(reserved)
        try:
            schedule_on_bot_loop(send_to_log_channel(bot, f"[ADMIN] Cancel {title} @ {slot} (was {reserved_ign})"))
        except Exception:
            pass

        flash(f"Cancelled {title} @ {slot} (was {reserved_ign})")
        return redirect(url_for("admin_home"))

    @app.route("/admin/force-release", methods=["POST"])
    def admin_force_release():
        if not is_admin():
            return redirect(url_for("admin_login_form"))
        title = (request.form.get("title") or "").strip()

        async def _force_release():
            if title not in state.get("titles", {}):
                return False
            state["titles"][title].update({
                'holder': None,
                'claim_date': None,
                'expiry_date': None,
                'pending_claimant': None
            })
            await save_state()
            return True

        fut = schedule_on_bot_loop(_force_release())
        try:
            ok = fut.result(timeout=3) if fut else False
        except Exception:
            ok = False

        if not ok:
            flash(f"Title '{title}' not found.")
            return redirect(url_for("admin_home"))

        try:
            schedule_on_bot_loop(send_to_log_channel(bot, f"[ADMIN] Force released {title}"))
        except Exception:
            pass
        flash(f"Force-released title '{title}'.")
        return redirect(url_for("admin_home"))

    @app.route("/admin/manual-assign", methods=["POST"])
    def admin_manual_assign():
        if not is_admin():
            return redirect(url_for("admin_login_form"))
        title = (request.form.get("title") or "").strip()
        ign = (request.form.get("ign") or "").strip()
        coords = (request.form.get("coords") or "-").strip()

        async def _assign():
            if not (title and ign and title in state.get("titles", {})):
                return False
            hours = get_shift_hours()
            now = now_utc()
            end = now + timedelta(hours=hours)
            state["titles"][title].update({
                "holder": {"name": ign, "coords": coords, "discord_id": 0},
                "claim_date": now.isoformat(),
                "expiry_date": end.isoformat(),
                "pending_claimant": None
            })
            await save_state()
            return True

        fut = schedule_on_bot_loop(_assign())
        try:
            ok = fut.result(timeout=3) if fut else False
        except Exception:
            ok = False

        if not ok:
            flash("Bad manual assignment request. Title and IGN are required.")
            return redirect(url_for("admin_home"))

        try:
            schedule_on_bot_loop(send_to_log_channel(bot, f"[ADMIN] MANUALLY ASSIGNED {title} -> {ign}"))
        except Exception:
            pass
        flash(f"Manually assigned {title} to {ign}")
        return redirect(url_for("admin_home"))

    @app.route("/admin/assign-now", methods=["POST"])
    def admin_assign_now():
        if not is_admin():
            return redirect(url_for("admin_login_form"))

        title = (request.form.get("title") or "").strip()
        ign = (request.form.get("ign") or "").strip()

        async def _assign_now():
            if not (title and ign and title in state.get("titles", {})):
                return False
            hours = get_shift_hours()
            now = now_utc()
            end = now + timedelta(hours=hours)
            state["titles"][title].update({
                "holder": {"name": ign, "coords": "-", "discord_id": 0},
                "claim_date": now.isoformat(),
                "expiry_date": end.isoformat(),
                "pending_claimant": None
            })
            await save_state()
            return True

        fut = schedule_on_bot_loop(_assign_now())
        try:
            ok = fut.result(timeout=3) if fut else False
        except Exception:
            ok = False

        if not ok:
            flash("Bad assign request")
            return redirect(url_for("admin_home"))

        try:
            schedule_on_bot_loop(send_to_log_channel(bot, f"[ADMIN] Assign-now {title} -> {ign}"))
        except Exception:
            pass

        flash(f"Assigned {title} immediately to {ign}")
        return redirect(url_for("admin_home"))

    @app.route("/admin/move", methods=["POST"])
    def admin_move():
        if not is_admin():
            return redirect(url_for("admin_login_form"))

        title = (request.form.get("title") or "").strip()
        slot = (request.form.get("slot") or "").strip()
        new_title = (request.form.get("new_title") or "").strip()
        new_slot = (request.form.get("new_slot") or "").strip()

        async def _move():
            if not (title and slot and new_title and new_slot):
                return False, "Missing info"
            sched = state.get("schedules", {}).get(title, {})
            if slot not in sched:
                return False, "Original reservation not found"
            reserved = sched[slot]
            del sched[slot]
            state.setdefault("schedules", {}).setdefault(new_title, {})[new_slot] = reserved
            # approval cleanup (not carried to new slot by default)
            old_ap = state.get("approvals", {}).setdefault(title, {})
            if slot in old_ap:
                try:
                    del old_ap[slot]
                except Exception:
                    pass
            await save_state()
            return True, reserved

        fut = schedule_on_bot_loop(_move())
        try:
            ok, reserved = fut.result(timeout=3) if fut else (False, None)
        except Exception:
            ok, reserved = (False, None)

        if not ok:
            flash("Missing info" if reserved == "Missing info" else "Original reservation not found")
            return redirect(url_for("admin_home"))

        reserved_ign = _reservation_to_ign(reserved)
        try:
            schedule_on_bot_loop(send_to_log_channel(bot, f"[ADMIN] Move {reserved_ign} {title}@{slot} → {new_title}@{new_slot}"))
        except Exception:
            pass

        flash(f"Moved {reserved_ign} from {title}@{slot} → {new_title}@{new_slot}")
        return redirect(url_for("admin_home"))

    @app.route("/admin/settings", methods=["POST"])
    def admin_settings():
        if not is_admin():
            return redirect(url_for("admin_login_form"))

        announce = (request.form.get("announce_channel") or "").strip()
        logch    = (request.form.get("log_channel") or "").strip()
        shift    = (request.form.get("shift_hours") or "").strip()

        async def _apply_settings():
            cfg = state.setdefault("config", {})
            if announce and announce.isdigit():
                cfg["announcement_channel"] = int(announce)
            if logch and logch.isdigit():
                cfg["log_channel"] = int(logch)
            if shift:
                try:
                    val = int(shift)
                    cfg["shift_hours"] = val
                    set_shift_hours(val)
                except ValueError:
                    pass
            await save_state()
            return True

        # user feedback for bad inputs
        if announce and not announce.isdigit():
            flash("Announcement channel must be a numeric ID.")
        if logch and not logch.isdigit():
            flash("Log channel must be a numeric ID.")
        if shift:
            try:
                int(shift)
            except ValueError:
                flash("Shift hours must be an integer.")

        fut = schedule_on_bot_loop(_apply_settings())
        try:
            _ = fut.result(timeout=3) if fut else None
        except Exception:
            pass

        try:
            schedule_on_bot_loop(send_to_log_channel(bot, "[ADMIN] Settings updated"))
        except Exception:
            pass

        flash("Settings updated")
        return redirect(url_for("admin_home"))