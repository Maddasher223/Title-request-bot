# web_routes.py — All Flask routes (dashboard + admin) wired by register_routes()

import os
import csv
import asyncio
from datetime import datetime, timedelta, timezone
from flask import render_template, request, redirect, url_for, flash, session

def register_routes(app, deps):
    """
    Injected dependencies from main.py (no circular import):
      deps = {
        ORDERED_TITLES, TITLES_CATALOG, ICON_FILES, REQUESTABLE, ADMIN_PIN,
        state, save_state, log_action, log_to_csv, send_webhook_notification, send_to_log_channel,
        parse_iso_utc, now_utc, iso_slot_key_naive, in_current_slot, title_is_vacant_now,
        compute_next_reservation_for_title, get_all_upcoming_reservations, set_shift_hours,
        bot
      }
    """
    # ----- Unpack deps -----
    ORDERED_TITLES = deps['ORDERED_TITLES']
    TITLES_CATALOG = deps['TITLES_CATALOG']
    ICON_FILES = deps['ICON_FILES']
    REQUESTABLE = deps['REQUESTABLE']
    ADMIN_PIN = deps['ADMIN_PIN']

    state = deps['state']
    save_state = deps['save_state']
    log_action = deps['log_action']
    log_to_csv = deps['log_to_csv']
    send_webhook_notification = deps['send_webhook_notification']
    send_to_log_channel = deps['send_to_log_channel']

    parse_iso_utc = deps['parse_iso_utc']
    now_utc = deps['now_utc']
    iso_slot_key_naive = deps['iso_slot_key_naive']
    in_current_slot = deps['in_current_slot']
    title_is_vacant_now = deps['title_is_vacant_now']
    compute_next_reservation_for_title = deps['compute_next_reservation_for_title']
    get_all_upcoming_reservations = deps['get_all_upcoming_reservations']
    set_shift_hours = deps['set_shift_hours']

    bot = deps['bot']

    UTC = timezone.utc

    # ----- Helpers -----
    def schedule_on_bot_loop(coro):
        """Run an async coroutine safely on the Discord bot loop from Flask thread."""
        try:
            return asyncio.run_coroutine_threadsafe(coro, bot.loop)
        except Exception:
            return None

    def is_admin() -> bool:
        return bool(session.get("is_admin"))

    def admin_required(f):
        from functools import wraps
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not is_admin():
                return redirect(url_for("admin_login_form"))
            return f(*args, **kwargs)
        return wrapper

    def get_shift_hours():
        cfg = state.get('config', {})
        val = cfg.get('shift_hours')
        try:
            return int(val) if val is not None else 3
        except Exception:
            return 3

    # ----- Public pages -----
    @app.route("/")
    def dashboard():
        titles_data = []
        for title_name in ORDERED_TITLES:
            data = state['titles'].get(title_name, {})
            holder_info = "None"
            if data.get('holder'):
                holder = data['holder']
                holder_info = f"{holder['name']} ({holder.get('coords','-')})"

            remaining = "N/A"
            if data.get('expiry_date'):
                try:
                    expiry = parse_iso_utc(data['expiry_date'])
                    delta = expiry - now_utc()
                    remaining = str(timedelta(seconds=int(delta.total_seconds()))) if delta.total_seconds() > 0 else "Expired"
                except Exception:
                    remaining = "Invalid"

            next_slot_key, next_ign = compute_next_reservation_for_title(title_name)
            next_res_text = f"{next_slot_key} by {next_ign}" if (next_slot_key and next_ign) else "—"

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
        # 3-hour grid; if you change shift hours in settings, the grid still uses 3h cells
        hours = [f"{h:02d}:00" for h in range(0, 24, 3)]
        schedules = state.get('schedules', {})
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
        csv_path = os.path.join(os.path.dirname(__file__), "data", "requests.csv")
        log_data = []
        if os.path.exists(csv_path):
            with open(csv_path, 'r', newline='') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    log_data.append(row)
        return render_template('log.html', logs=reversed(log_data))

    @app.post("/cancel")
    def cancel_schedule():
        """
        Web cancel for a reservation (no IGN required).
        """
        title_name = (request.form.get("title") or "").strip()
        slot_key   = (request.form.get("slot") or "").strip()

        if not title_name or not slot_key:
            flash("Missing info to cancel.")
            return redirect(url_for("dashboard"))

        schedule = state.get('schedules', {}).get(title_name, {})
        reserver_ign = schedule.get(slot_key)
        if not reserver_ign:
            flash("No reservation found for that slot.")
            return redirect(url_for("dashboard"))

        # Remove reservation + activation mark
        try:
            del schedule[slot_key]
        except KeyError:
            pass
        acts = state.get('activated_slots', {}).get(title_name, [])
        if slot_key in acts:
            acts.remove(slot_key)

        schedule_on_bot_loop(save_state())
        schedule_on_bot_loop(send_to_log_channel(bot,
            f"[UNSCHEDULE:WEB] cancelled {title_name} @ {slot_key} (was {reserver_ign})"))

        flash(f"Cancelled reservation for {title_name} @ {slot_key}.")
        return redirect(url_for("dashboard"))

    @app.route("/book-slot", methods=['POST'])
    def book_slot():
        title_name = (request.form.get('title') or '').strip()
        ign        = (request.form.get('ign') or '').strip()
        coords     = (request.form.get('coords') or '').strip()
        date_str   = (request.form.get('date') or '').strip()
        time_str   = (request.form.get('time') or '').strip()

        if not title_name or not ign or not date_str or not time_str:
            flash("Missing form data: title, IGN, date, and time are required.")
            return redirect(url_for("dashboard"))
        if title_name not in REQUESTABLE:
            flash("This title cannot be requested.")
            return redirect(url_for("dashboard"))

        try:
            schedule_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        except ValueError:
            flash("Time must be HH:MM (24h), e.g., 15:00.")
            return redirect(url_for("dashboard"))
        if schedule_time < now_utc():
            flash("Cannot schedule a time in the past.")
            return redirect(url_for("dashboard"))

        schedule_key = iso_slot_key_naive(schedule_time)

        # Only block same title + same slot
        schedules_for_title = state.setdefault('schedules', {}).setdefault(title_name, {})
        if schedule_key in schedules_for_title:
            flash(f"That slot for {title_name} is already reserved by {schedules_for_title[schedule_key]}.")
            return redirect(url_for("dashboard"))

        # Reserve immediately (shows as taken in grid)
        schedules_for_title[schedule_key] = ign
        log_action('schedule_book_web', 0, {'title': title_name, 'time': schedule_key, 'ign': ign})

        # If current slot and vacant, grant now
        try:
            if (schedule_time <= now_utc() < schedule_time + timedelta(hours=get_shift_hours())) and title_is_vacant_now(title_name):
                end = schedule_time + timedelta(hours=get_shift_hours())
                state['titles'].setdefault(title_name, {})
                state['titles'][title_name].update({
                    'holder': {'name': ign, 'coords': coords or '-', 'discord_id': 0},
                    'claim_date': schedule_time.isoformat(),
                    'expiry_date': end.isoformat(),
                    'pending_claimant': None
                })
                log_action('auto_assign_now', 0, {'title': title_name, 'ign': ign, 'start': schedule_time.isoformat()})
        except Exception:
            pass

        # CSV + webhook + persist + log
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
        except Exception:
            pass

        schedule_on_bot_loop(save_state())
        schedule_on_bot_loop(send_to_log_channel(bot,
            f"[SCHEDULE:WEB] reserved {title_name} for {ign} @ {date_str} {time_str} UTC"))

        flash(f"Reserved {title_name} for {ign} on {date_str} at {time_str} UTC.")
        return redirect(url_for("dashboard"))

    # ----- Admin: session auth -----
    @app.get("/admin/login")
    def admin_login_form():
        return render_template("admin_login.html")

    @app.post("/admin/login")
    def admin_login_submit():
        pin = (request.form.get("pin") or "").strip()
        if pin == ADMIN_PIN:
            session["is_admin"] = True
            flash("Welcome, admin.")
            return redirect(url_for("admin_home"))
        flash("Incorrect PIN.")
        return redirect(url_for("admin_login_form"))

    @app.get("/admin/logout")
    def admin_logout():
        session.pop("is_admin", None)
        flash("Logged out.")
        return redirect(url_for("dashboard"))

    # ----- Admin: dashboard -----
    @app.route("/admin", methods=["GET"])
    def admin_home():
        if not is_admin():
            return redirect(url_for("admin_login_form"))

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

        # Upcoming reservations + CSV logs
        upcoming = get_all_upcoming_reservations()

        csv_path = os.path.join(os.path.dirname(__file__), "data", "requests.csv")
        logs = []
        if os.path.exists(csv_path):
            with open(csv_path, 'r', newline='') as f:
                reader = csv.DictReader(f)
                logs = list(reader)[-200:]

        cfg = state.get('config', {})
        current_settings = {
            "announcement_channel": cfg.get("announcement_channel"),
            "log_channel": cfg.get("log_channel"),
            "shift_hours": cfg.get("shift_hours", get_shift_hours()),
        }

        return render_template(
            "admin.html",
            active_titles=active,
            upcoming=upcoming,
            logs=reversed(logs),
            settings=current_settings
        )

    # ----- Admin actions -----
    @app.post("/admin/force-release")
    def admin_force_release():
        if not is_admin():
            return redirect(url_for("admin_login_form"))
        title = (request.form.get("title") or "").strip()
        if not title or title not in state.get("titles", {}):
            flash("Bad title")
            return redirect(url_for("admin_home"))

        state["titles"][title].update({
            "holder": None,
            "claim_date": None,
            "expiry_date": None,
            "pending_claimant": None
        })
        schedule_on_bot_loop(save_state())
        schedule_on_bot_loop(send_to_log_channel(bot, f"[ADMIN] Force release {title}"))
        flash(f"Force released {title}")
        return redirect(url_for("admin_home"))

    @app.post("/admin/cancel")
    def admin_cancel():
        if not is_admin():
            return redirect(url_for("admin_login_form"))
        title = (request.form.get("title") or "").strip()
        slot = (request.form.get("slot") or "").strip()
        sched = state.get("schedules", {}).get(title, {})
        if not (title and slot and slot in sched):
            flash("Reservation not found")
            return redirect(url_for("admin_home"))

        ign = sched[slot]
        del sched[slot]
        schedule_on_bot_loop(save_state())
        schedule_on_bot_loop(send_to_log_channel(bot, f"[ADMIN] Cancel {title} @ {slot} (was {ign})"))
        flash(f"Cancelled {title} @ {slot} (was {ign})")
        return redirect(url_for("admin_home"))

    @app.post("/admin/assign-now")
    def admin_assign_now():
        if not is_admin():
            return redirect(url_for("admin_login_form"))
        title = (request.form.get("title") or "").strip()
        ign = (request.form.get("ign") or "").strip()
        if not (title and ign and title in state.get("titles", {})):
            flash("Bad assign request")
            return redirect(url_for("admin_home"))

        hours = get_shift_hours()
        now = now_utc()
        end = now + timedelta(hours=hours)
        state["titles"][title].update({
            "holder": {"name": ign, "coords": "-", "discord_id": 0},
            "claim_date": now.isoformat(),
            "expiry_date": end.isoformat(),
            "pending_claimant": None
        })
        schedule_on_bot_loop(save_state())
        schedule_on_bot_loop(send_to_log_channel(bot, f"[ADMIN] Assign-now {title} -> {ign}"))
        flash(f"Assigned {title} immediately to {ign}")
        return redirect(url_for("admin_home"))

    @app.post("/admin/move")
    def admin_move():
        if not is_admin():
            return redirect(url_for("admin_login_form"))
        title = (request.form.get("title") or "").strip()
        slot = (request.form.get("slot") or "").strip()
        new_title = (request.form.get("new_title") or "").strip()
        new_slot = (request.form.get("new_slot") or "").strip()
        if not (title and slot and new_title and new_slot):
            flash("Missing info")
            return redirect(url_for("admin_home"))

        sched = state.get("schedules", {}).get(title, {})
        if slot not in sched:
            flash("Original reservation not found")
            return redirect(url_for("admin_home"))

        ign = sched[slot]
        del sched[slot]
        state.setdefault("schedules", {}).setdefault(new_title, {})[new_slot] = ign

        schedule_on_bot_loop(save_state())
        schedule_on_bot_loop(send_to_log_channel(bot,
            f"[ADMIN] Move {ign} {title}@{slot} → {new_title}@{new_slot}"))
        flash(f"Moved {ign} from {title}@{slot} → {new_title}@{new_slot}")
        return redirect(url_for("admin_home"))

    @app.post("/admin/settings")
    def admin_settings():
        if not is_admin():
            return redirect(url_for("admin_login_form"))
        announce = (request.form.get("announce_channel") or "").strip()
        logch = (request.form.get("log_channel") or "").strip()
        shift = (request.form.get("shift_hours") or "").strip()

        cfg = state.setdefault("config", {})
        if announce:
            try:
                cfg["announcement_channel"] = int(announce)
            except ValueError:
                flash("Announcement channel must be a numeric ID.")
        if logch:
            try:
                cfg["log_channel"] = int(logch)
            except ValueError:
                flash("Log channel must be a numeric ID.")
        if shift:
            try:
                cfg["shift_hours"] = int(shift)
                set_shift_hours(cfg["shift_hours"])
            except ValueError:
                flash("Shift hours must be an integer.")

        schedule_on_bot_loop(save_state())
        schedule_on_bot_loop(send_to_log_channel(bot, "[ADMIN] Settings updated"))
        flash("Settings updated")
        return redirect(url_for("admin_home"))