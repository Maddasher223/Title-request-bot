# admin_routes.py
from __future__ import annotations

import io
import csv
import math
from functools import wraps
from types import SimpleNamespace
from datetime import datetime, date as date_cls, timedelta, timezone
from typing import Dict, Any, Callable, Optional

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash, send_file, current_app
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy import or_

UTC = timezone.utc


def register_admin(app, deps: dict):
    """
    Mounts /admin dashboard + ops + management pages.

    deps expected:
      - ADMIN_PIN (str)
      - get_shift_hours (callable) -> int
      - set_shift_hours (callable) -> None  (or db_set_shift_hours)
      - send_webhook_notification (callable)
      - SERVER_CONFIGS (dict[int, dict])  [passed by reference for live updates]
      - db (SQLAlchemy instance)
      - models (dict or object with Title, Reservation, ActiveTitle, RequestLog, Setting, ServerConfig)
      - db_helpers (dict) with:
          compute_slots, requestable_title_names, schedule_lookup
      - airtable_upsert (optional callable)
    """
    # --- deps ---
    ADMIN_PIN: str = deps["ADMIN_PIN"]
    get_shift_hours: Callable[[], int] = deps["get_shift_hours"]
    set_shift_hours: Callable[[int], None] = deps.get("db_set_shift_hours") or deps.get("set_shift_hours")
    send_webhook_notification: Callable[..., None] = deps["send_webhook_notification"]
    SERVER_CONFIGS: Dict[int, Dict[str, Any]] = deps["SERVER_CONFIGS"]  # shared mutable cache
    db = deps["db"]

    M = deps["models"]
    # Allow passing a dict for models; convert to attribute-style access
    if isinstance(M, dict):
        M = SimpleNamespace(**M)

    H: Dict[str, Callable[..., Any]] = deps.get("db_helpers", {}) or {}
    airtable_upsert: Optional[Callable[..., None]] = deps.get("airtable_upsert")

    admin_bp = Blueprint("admin", __name__, template_folder="templates/admin", url_prefix="/admin")

    # --- helpers ---
    def now_utc() -> datetime:
        return datetime.now(UTC)

    def admin_required(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if session.get("is_admin"):
                return fn(*a, **kw)
            # preserve where we were headed
            return redirect(url_for("admin.login", next=request.path))
        return wrapper

    def _refresh_server_cache() -> None:
        """Reload DB server configs into the shared SERVER_CONFIGS dict."""
        SERVER_CONFIGS.clear()
        try:
            rows = M.ServerConfig.query.all()
        except Exception:
            return
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
            SERVER_CONFIGS[gid] = {"webhook": r.webhook_url, "guardian_role_id": rid}

    # ========== Auth ==========
    @admin_bp.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            pin = (request.form.get("pin") or "").strip()
            if pin == ADMIN_PIN:
                session["is_admin"] = True
                nxt = request.args.get("next") or url_for("admin.dashboard")
                return redirect(nxt)
            flash("Invalid PIN.", "error")
        return render_template("login.html")

    @admin_bp.route("/logout")
    def logout():
        session.pop("is_admin", None)
        return redirect(url_for("admin.login"))

    # ========== Dashboard (light overview) ==========
    @admin_bp.route("/")
    @admin_required
    def dashboard():
        shift = int(get_shift_hours())
        title_count = M.Title.query.count()
        reservation_count = M.Reservation.query.count()
        servers = M.ServerConfig.query.order_by(M.ServerConfig.guild_id.asc()).all()
        return render_template(
            "admin_dashboard.html",
            shift=shift,
            stats={
                "title_count": title_count,
                "reservation_count": reservation_count,
                "server_count": len(servers),
            },
            titles=M.Title.query.order_by(M.Title.name.asc()).all(),
            servers=servers,
        )

    # ========== Ops panel (rich view + actions UI) ==========
    @admin_bp.route("/ops")
    @admin_required
    def ops():
        compute_slots = H["compute_slots"]
        schedule_lookup = H["schedule_lookup"]
        requestable_title_names = H["requestable_title_names"]

        shift = int(get_shift_hours())
        slots = compute_slots(shift)
        today = date_cls.today()
        days = [today + timedelta(days=i) for i in range(14)]
        schedule_map = schedule_lookup(days, slots)

        # Active titles summary
        active_titles = []
        for row in M.ActiveTitle.query.all():
            expires_str = "Never"
            if row.expiry_at:
                dt = row.expiry_at
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                expires_str = dt.astimezone(UTC).isoformat()
            active_titles.append({
                "title": row.title_name,
                "holder": row.holder or "-",
                "expires": expires_str,
            })

        return render_template(
            "ops.html",
            active_titles=active_titles,
            requestable_titles=requestable_title_names(),
            today=today.isoformat(),
            days=days,
            slots=slots,
            schedule_lookup=schedule_map,
            shift_hours=shift,
        )

    # ========== Shift hours ==========
    @admin_bp.route("/shift", methods=["POST"])
    @admin_required
    def set_shift():
        try:
            hours = int(request.form.get("hours", "12"))
            if not (1 <= hours <= 72):
                raise ValueError
            set_shift_hours(hours)
            flash("Shift hours updated.", "success")
        except Exception:
            flash("Invalid hours (1-72).", "error")
        ref = request.referrer or ""
        return redirect(url_for("admin.ops") if "/admin/ops" in ref else url_for("admin.dashboard"))

    # ========== Titles management ==========
    @admin_bp.route("/titles", methods=["GET", "POST"])
    @admin_required
    def titles_page():
        if request.method == "POST":
            action = request.form.get("action")
            if action == "toggle_requestable":
                name = request.form.get("name")
                t = M.Title.query.filter_by(name=name).first()
                if t:
                    t.requestable = not bool(t.requestable)
                    db.session.commit()
                    flash(f"{name}: requestable → {t.requestable}", "success")
                else:
                    flash("Unknown title.", "error")

            elif action == "rename":
                old = (request.form.get("old_name") or "").strip()
                new = (request.form.get("new_name") or "").strip()
                if not (old and new):
                    flash("Provide both old and new names.", "error")
                elif old == new:
                    flash("New name is the same as the old name.", "error")
                else:
                    t = M.Title.query.filter_by(name=old).first()
                    if not t:
                        flash("Unknown title to rename.", "error")
                    elif M.Title.query.filter_by(name=new).first():
                        flash(f"Name '{new}' already exists.", "error")
                    else:
                        try:
                            # Transaction: rename Title and propagate to dependents
                            t.name = new
                            # Update dependent tables to keep data consistent
                            M.ActiveTitle.query.filter_by(title_name=old).update({"title_name": new})
                            M.Reservation.query.filter_by(title_name=old).update({"title_name": new})
                            db.session.commit()
                            flash(f"Renamed '{old}' → '{new}'", "success")
                        except IntegrityError:
                            db.session.rollback()
                            flash("Rename failed due to a uniqueness/constraint error.", "error")
                        except Exception as e:
                            db.session.rollback()
                            current_app.logger.exception("rename title failed: %s", e)
                            flash("Internal error while renaming.", "error")

            elif action == "icon":
                name = (request.form.get("name") or "").strip()
                icon = (request.form.get("icon_url") or "").strip()
                t = M.Title.query.filter_by(name=name).first()
                if t and icon:
                    try:
                        # very light validation; avoid empty/whitespace-only
                        if len(icon) < 2:
                            raise ValueError("Empty icon URL")
                        t.icon_url = icon
                        db.session.commit()
                        flash(f"Updated icon for '{name}'.", "success")
                    except Exception as e:
                        db.session.rollback()
                        current_app.logger.exception("icon update failed: %s", e)
                        flash("Failed to update icon.", "error")
                elif not t:
                    flash("Unknown title.", "error")
                else:
                    flash("Icon URL cannot be empty.", "error")

        titles = M.Title.query.order_by(M.Title.name.asc()).all()
        return render_template("titles.html", titles=titles)

    # ========== Reservations: search/paginate/export ==========
    @admin_bp.route("/reservations")
    @admin_required
    def reservations_page():
        q = (request.args.get("q") or "").strip()
        page = max(1, int(request.args.get("page", "1")))
        per_page = 25

        query = M.Reservation.query
        if q:
            like = f"%{q}%"
            query = query.filter(
                or_(M.Reservation.title_name.ilike(like),
                    M.Reservation.ign.ilike(like),
                    M.Reservation.coords.ilike(like))
            )

        total = query.count()
        rows = (query
                .order_by(M.Reservation.slot_dt.desc().nullslast(), M.Reservation.id.desc())
                .offset((page - 1) * per_page)
                .limit(per_page)
                .all())
        pages = max(1, math.ceil(total / per_page))
        return render_template("reservations.html", rows=rows, q=q, page=page, pages=pages, total=total)

    @admin_bp.route("/reservations/export.csv")
    @admin_required
    def reservations_export():
        q = (request.args.get("q") or "").strip()
        query = M.Reservation.query
        if q:
            like = f"%{q}%"
            query = query.filter(
                or_(M.Reservation.title_name.ilike(like),
                    M.Reservation.ign.ilike(like),
                    M.Reservation.coords.ilike(like))
            )
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["title_name", "ign", "coords", "slot_dt"])
        for r in query.order_by(M.Reservation.slot_dt.desc().nullslast()).all():
            w.writerow([r.title_name, r.ign, r.coords, (r.slot_dt.isoformat() if r.slot_dt else "")])
        out.seek(0)
        return send_file(
            io.BytesIO(out.getvalue().encode("utf-8")),
            mimetype="text/csv",
            as_attachment=True,
            download_name="reservations.csv",
        )

    # ========== Servers: CRUD + set default + test ping ==========
    @admin_bp.route("/servers", methods=["GET", "POST"])
    @admin_required
    def servers_page():
        if request.method == "POST":
            action = request.form.get("action")
            gid = (request.form.get("guild_id") or "").strip()
            wh = (request.form.get("webhook_url") or "").strip()
            role = (request.form.get("guardian_role_id") or "").strip()

            if action == "create":
                if not gid or not wh:
                    flash("guild_id and webhook_url are required.", "error")
                else:
                    exists = db.session.get(M.ServerConfig, gid)
                    if exists:
                        flash("Guild already exists.", "error")
                    else:
                        row = M.ServerConfig(
                            guild_id=gid,
                            webhook_url=wh,
                            guardian_role_id=(role or None),
                            is_default=False
                        )
                        db.session.add(row)
                        db.session.commit()
                        _refresh_server_cache()
                        flash("Server added.", "success")

            elif action == "update":
                row = db.session.get(M.ServerConfig, gid)
                if row:
                    if wh:
                        row.webhook_url = wh
                    row.guardian_role_id = (role or None)
                    db.session.commit()
                    _refresh_server_cache()
                    flash("Server updated.", "success")
                else:
                    flash("Unknown guild ID.", "error")

            elif action == "delete":
                row = db.session.get(M.ServerConfig, gid)
                if row:
                    db.session.delete(row)
                    db.session.commit()
                    _refresh_server_cache()
                    flash("Server deleted.", "success")
                else:
                    flash("Unknown guild ID.", "error")

            elif action == "set_default":
                target = db.session.get(M.ServerConfig, gid)
                if target:
                    # Clear existing default(s)
                    for r in M.ServerConfig.query.filter_by(is_default=True).all():
                        r.is_default = False
                    target.is_default = True
                    db.session.commit()
                    _refresh_server_cache()
                    flash(f"Default server set: {gid}", "success")
                else:
                    flash("Unknown guild ID.", "error")

            elif action == "test_ping":
                _refresh_server_cache()
                try:
                    send_webhook_notification(
                        {
                            "title_name": "(test)",
                            "in_game_name": "(dashboard)",
                            "coordinates": "-",
                            "timestamp": now_utc().isoformat(),
                            "discord_user": "Admin"
                        },
                        reminder=False,
                        guild_id=int(gid) if gid.isdigit() else None
                    )
                    flash("Test webhook sent (check Discord).", "success")
                except Exception as e:
                    flash(f"Webhook error: {e}", "error")

        servers = M.ServerConfig.query.order_by(M.ServerConfig.guild_id.asc()).all()
        return render_template("servers.html", servers=servers)

    # ========== Ops actions ==========
    @admin_bp.route("/manual-assign", methods=["POST"])
    @admin_required
    def manual_assign():
        try:
            title = (request.form.get("title") or "").strip()
            ign = (request.form.get("ign") or "").strip()
            goh_only = (request.form.get("goh_only") or "").strip()

            if goh_only and title != "Guardian of Harmony":
                flash("This assignment form is only for Guardian of Harmony.", "error")
                return redirect(url_for("admin.ops"))

            if not title or not ign:
                flash("Title and IGN are required.", "error")
                return redirect(url_for("admin.ops"))

            if not M.Title.query.filter_by(name=title).first():
                flash(f"Unknown title: {title}", "error")
                return redirect(url_for("admin.ops"))

            now = now_utc()
            expiry_dt = None if title == "Guardian of Harmony" else now + timedelta(hours=int(get_shift_hours()))

            row = M.ActiveTitle.query.filter_by(title_name=title).first()
            if not row:
                row = M.ActiveTitle(title_name=title, holder=ign, claim_at=now, expiry_at=expiry_dt)
                db.session.add(row)
            else:
                row.holder = ign
                row.claim_at = now
                row.expiry_at = expiry_dt

            db.session.commit()

            if airtable_upsert:
                try:
                    airtable_upsert("assignment", {
                        "Title": title,
                        "IGN": ign,
                        "Coordinates": "-",
                        "SlotStartUTC": now,
                        "SlotEndUTC": expiry_dt,
                        "Source": "Admin Manual Assign",
                        "DiscordUser": "Admin",
                    })
                except Exception:
                    pass

            flash(f"Manually assigned '{title}' to {ign}.", "success")
        except Exception as e:
            db.session.rollback()
            current_app.logger.exception("manual_assign failed: %s", e)
            flash("Internal error while assigning.", "error")
        return redirect(url_for("admin.ops"))

    @admin_bp.route("/manual-set-slot", methods=["POST"])
    @admin_required
    def manual_set_slot():
        compute_slots = H["compute_slots"]

        title = (request.form.get("title") or "").strip()
        ign = (request.form.get("ign") or "").strip()
        date_str = (request.form.get("date") or "").strip()
        slot = (request.form.get("slot") or "").strip()  # "HH:MM"

        if not all([title, ign, date_str, slot]):
            flash("Missing data for manual slot assignment.", "error")
            return redirect(url_for("admin.ops"))
        if title == "Guardian of Harmony":
            flash("'Guardian of Harmony' cannot be assigned to a timed slot.", "error")
            return redirect(url_for("admin.ops"))
        if not M.Title.query.filter_by(name=title).first():
            flash("Unknown title.", "error")
            return redirect(url_for("admin.ops"))

        try:
            start_dt = datetime.strptime(f"{date_str} {slot}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
            end_dt = start_dt + timedelta(hours=int(get_shift_hours()))
        except ValueError:
            flash("Invalid date or slot format.", "error")
            return redirect(url_for("admin.ops"))

        # Optional guard: ensure manual slot aligns to current grid
        allowed = set(compute_slots(int(get_shift_hours())))
        if slot not in allowed:
            flash(f"Slot must be one of {sorted(allowed)} UTC.", "error")
            return redirect(url_for("admin.ops"))

        slot_ts = f"{date_str}T{slot}:00"

        try:
            existing = (
                M.Reservation.query
                .filter(M.Reservation.title_name == title)
                .filter(M.Reservation.slot_dt == start_dt)
                .first()
            )
            if not existing:
                db.session.add(M.Reservation(
                    title_name=title,
                    ign=ign,
                    coords="-",
                    slot_dt=start_dt,
                    slot_ts=slot_ts,
                ))
            else:
                existing.ign = ign
                existing.coords = "-"
                if hasattr(existing, "slot_ts"):
                    existing.slot_ts = slot_ts

            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("That slot is already taken.", "error")
            return redirect(url_for("admin.ops"))
        except Exception as e:
            db.session.rollback()
            current_app.logger.exception("manual_set_slot (reservation) failed: %s", e)
            flash("Internal error while writing reservation.", "error")
            return redirect(url_for("admin.ops"))

        # keep ActiveTitle in sync if the slot has started
        try:
            if start_dt <= now_utc():
                row = M.ActiveTitle.query.filter_by(title_name=title).first()
                if not row:
                    row = M.ActiveTitle(title_name=title, holder=ign, claim_at=start_dt, expiry_at=end_dt)
                    db.session.add(row)
                else:
                    row.holder = ign
                    row.claim_at = start_dt
                    row.expiry_at = end_dt
                db.session.commit()
        except Exception as e:
            db.session.rollback()
            current_app.logger.exception("manual_set_slot (active) failed: %s", e)
            flash("Reservation saved, but live assignment failed to update.", "error")
            return redirect(url_for("admin.ops"))

        if airtable_upsert:
            try:
                airtable_upsert("assignment", {
                    "Title": title,
                    "IGN": ign,
                    "Coordinates": "-",
                    "SlotStartUTC": start_dt,
                    "SlotEndUTC": end_dt,
                    "Source": "Admin Forced Slot",
                    "DiscordUser": "Admin",
                })
            except Exception:
                pass

        flash(f"Manually set '{title}' for {ign} in the {date_str} {slot} slot.", "success")
        return redirect(url_for("admin.ops"))

    @admin_bp.route("/force-release", methods=["POST"])
    @admin_required
    def force_release():
        title = (request.form.get("title") or "").strip()
        if not title:
            flash("Missing title.", "error")
            return redirect(url_for("admin.ops"))

        try:
            row = M.ActiveTitle.query.filter_by(title_name=title).first()
            if row:
                db.session.delete(row)
                db.session.commit()
            flash(f"Force-released title '{title}'.", "success")
        except Exception as e:
            db.session.rollback()
            current_app.logger.exception("force_release failed: %s", e)
            flash("Internal error while releasing. The incident was logged.", "error")
        return redirect(url_for("admin.ops"))

    @admin_bp.route("/release-reservation", methods=["POST"])
    @admin_required
    def release_reservation():
        title = (request.form.get("title") or "").strip()
        date_str = (request.form.get("date") or "").strip()   # YYYY-MM-DD
        time_str = (request.form.get("time") or "").strip()   # HH:MM
        also_release_live = bool(request.form.get("also_release_live"))

        if not all([title, date_str, time_str]):
            flash("Missing title/date/time to release reservation.", "error")
            return redirect(url_for("admin.ops"))

        try:
            start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        except ValueError:
            flash("Invalid date/time to release.", "error")
            return redirect(url_for("admin.ops"))

        slot_ts = f"{date_str}T{time_str}:00"

        try:
            q = (M.Reservation.query
                 .filter(M.Reservation.title_name == title)
                 .filter(M.Reservation.slot_dt == start_dt))
            res = q.first()
            if not res:
                res = (
                    M.Reservation.query
                    .filter(M.Reservation.title_name == title)
                    .filter(M.Reservation.slot_ts == slot_ts)
                ).first()

            if not res:
                flash("Reservation not found.", "error")
                return redirect(url_for("admin.ops"))

            res_ign = res.ign
            db.session.delete(res)
            db.session.commit()
            flash(f"Reservation for '{title}' at {date_str} {time_str} was released.", "success")

        except Exception as e:
            db.session.rollback()
            current_app.logger.exception("release_reservation failed: %s", e)
            flash("Internal error while releasing reservation.", "error")
            return redirect(url_for("admin.ops"))

        # Optional live release if it matches the active slot
        if also_release_live:
            try:
                row = M.ActiveTitle.query.filter_by(title_name=title).first()
                if row:
                    same_holder = (row.holder or "") == (res_ign or "")
                    claim_at = row.claim_at
                    if claim_at and claim_at.tzinfo is None:
                        claim_at = claim_at.replace(tzinfo=UTC)
                    same_start = bool(claim_at and claim_at.replace(microsecond=0) == start_dt.replace(microsecond=0))
                    if same_holder and same_start:
                        db.session.delete(row)
                        db.session.commit()
                        flash(f"Live title '{title}' was also released.", "success")
            except Exception as e:
                db.session.rollback()
                current_app.logger.exception("live release after reservation delete failed: %s", e)
                flash("Reservation removed, but live title release failed.", "error")

        return redirect(url_for("admin.ops"))

    # Done — mount blueprint
    app.register_blueprint(admin_bp)