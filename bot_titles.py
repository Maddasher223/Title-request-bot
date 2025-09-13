# bot_titles.py
# discord.py v2.x (py-cord/nextcord variants: adapt imports if needed)

import re
import os
import datetime as dt
from typing import Optional, List

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

# ========= CONFIG (edit these) =========
DASHBOARD_BASE_URL = (os.getenv("DASHBOARD_BASE_URL") or "").rstrip("/")  # normalize; allow unset
BOOK_SLOT_PATH     = "/book-slot"  # existing Flask route (form post)

# Optional read-only JSON endpoints. If you don’t have them yet, leave blank.
# If you later add them, commands will auto-upgrade UX without code changes.
API_REQUESTABLE = os.getenv("API_REQUESTABLE", "")  # e.g. "/api/requestable" -> returns ["Guardian of Fire", ...]
API_STATUS      = os.getenv("API_STATUS", "")       # e.g. "/api/status_cards"
API_SCHEDULE    = os.getenv("API_SCHEDULE", "")     # e.g. "/api/schedule?days=12"

# Fallback — must match server rules (Harmony is NOT requestable)
REQUESTABLE_TITLES_FALLBACK = [
    "Guardian of Fire",
    "Guardian of Water",
    "Guardian of Earth",
    "Guardian of Air",
    "Architect",
    "General",
    "Governor",
    "Prefect",
]

# If you only allow slots at exact hours (matching your UI), set minutes_allowed = {0}
minutes_allowed = {0}  # for strict HH:00; set to {0, 30} if you ever allow half-hours

COORDS_RE = re.compile(r"^\s*\d+\s*:\s*\d+\s*$")


def _now_utc():
    return dt.datetime.now(dt.timezone.utc)


def _is_valid_date_utc(s: str) -> bool:
    try:
        dt.date.fromisoformat(s)
        return True
    except Exception:
        return False


def _is_valid_time_utc(s: str) -> bool:
    try:
        hh, mm = s.split(":")
        h, m = int(hh), int(mm)
        return 0 <= h <= 23 and 0 <= m <= 59 and m in minutes_allowed
    except Exception:
        return False


def _headers():
    return {"User-Agent": "title-bot/1.1 (+discord)"}


async def _fetch_json(session: aiohttp.ClientSession, base: str, path: str) -> Optional[object]:
    if not path or not base:
        return None
    url = f"{base}{path}"
    try:
        async with session.get(url, timeout=10, headers=_headers()) as resp:
            if resp.status == 200:
                try:
                    return await resp.json()
                except Exception:
                    return None
            return None
    except Exception:
        return None


async def _get_requestable(session: aiohttp.ClientSession) -> List[str]:
    data = await _fetch_json(session, DASHBOARD_BASE_URL, API_REQUESTABLE)
    if isinstance(data, list) and all(isinstance(x, str) for x in data):
        return data
    return REQUESTABLE_TITLES_FALLBACK


# ---------- FIXED: module-level autocomplete (no 'self' required) ----------
async def _title_autocomplete(interaction: discord.Interaction, current: str):
    async with aiohttp.ClientSession() as session:
        all_titles = await _get_requestable(session)
    current_lower = (current or "").lower()
    filtered = [t for t in all_titles if current_lower in t.lower()]
    return [app_commands.Choice(name=t, value=t) for t in filtered[:25]]


class TitlesGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="titles", description="Title tools and quick helpers")

    # ---------- /titles reserve ----------
    @app_commands.command(name="reserve", description="Reserve a title slot")
    @app_commands.describe(
        title="Which title to reserve",
        ign="Your in-game name",
        coords="Outpost coordinates in X:Y (e.g. 123:456). Leave blank or '-' if unknown.",
        date_utc="Date (UTC) in YYYY-MM-DD",
        time_utc="Time (UTC) in HH:MM (00:00, 12:00)",
    )
    @app_commands.autocomplete(title=_title_autocomplete)
    async def reserve(
        self,
        interaction: discord.Interaction,
        title: str,
        ign: str,
        coords: Optional[str],
        date_utc: str,
        time_utc: str,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not DASHBOARD_BASE_URL:
            return await interaction.followup.send(
                "❌ Server is not configured. Ask an admin to set `DASHBOARD_BASE_URL` on the bot.",
                ephemeral=True,
            )

        # Normalize inputs
        title = (title or "").strip()
        ign = (ign or "").strip()
        coords_raw = (coords or "").strip()
        coords_norm = "-" if not coords_raw or coords_raw == "-" else coords_raw

        async with aiohttp.ClientSession() as session:
            requestable = await _get_requestable(session)

        errors = []
        if title not in requestable:
            errors.append("**title** is not requestable.")
        if not ign:
            errors.append("**ign** is required.")
        if coords_norm != "-" and not COORDS_RE.match(coords_norm):
            errors.append("**coords** must look like `123:456` (or use `-`).")
        if not _is_valid_date_utc(date_utc or ""):
            errors.append("**date** must be `YYYY-MM-DD` (UTC).")
        if not _is_valid_time_utc(time_utc or ""):
            mm_note = "00" if minutes_allowed == {0} else "00 or 30"
            errors.append(f"**time** must be `HH:MM` (UTC), minutes {mm_note}.")

        # Past guard
        if not errors:
            try:
                hh, mm = map(int, time_utc.split(":"))
                y, m, d = map(int, date_utc.split("-"))
                when = dt.datetime(y, m, d, hh, mm, tzinfo=dt.timezone.utc)
                if when < _now_utc():
                    errors.append("That **date/time** is already in the past (UTC).")
            except Exception:
                errors.append("Could not parse **date/time**.")

        if errors:
            return await interaction.followup.send("I couldn't submit that:\n• " + "\n• ".join(errors), ephemeral=True)

        # POST to your existing Flask form endpoint
        form = {"title": title, "ign": ign, "coords": coords_norm, "date": date_utc, "time": time_utc}
        url = f"{DASHBOARD_BASE_URL}{BOOK_SLOT_PATH}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=form, allow_redirects=False, headers=_headers(), timeout=12) as resp:
                    # Treat 2xx and 3xx as success since Flask redirects after flash
                    if 200 <= resp.status < 400:
                        return await interaction.followup.send(
                            f"✅ Reserved **{title}** for **{ign}** at **{date_utc} {time_utc} UTC**.\n"
                            f"Dashboard: {DASHBOARD_BASE_URL}",
                            ephemeral=True,
                        )
                    body = await resp.text()
                    return await interaction.followup.send(
                        f"⚠️ The server didn’t accept the reservation (HTTP {resp.status}).\n"
                        f"```{body[:300]}```",
                        ephemeral=True,
                    )
        except Exception as e:
            return await interaction.followup.send(f"❌ Network/Server error:\n```\n{e}\n```", ephemeral=True)

    # ---------- /titles list ----------
    @app_commands.command(name="list", description="See which titles are requestable")
    async def list_titles(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with aiohttp.ClientSession() as session:
            requestable = await _get_requestable(session)
        lines = "\n".join(f"• {t}" for t in requestable)
        await interaction.followup.send(f"**Requestable titles:**\n{lines}", ephemeral=True)

    # ---------- /titles timeguide ----------
    @app_commands.command(name="timeguide", description="Convert a UTC time to common regions")
    @app_commands.describe(
        date_utc="YYYY-MM-DD (UTC). Leave empty for today.",
        time_utc="HH:MM in 24h (UTC), e.g. 00:00 or 12:00",
    )
    async def timeguide(
        self,
        interaction: discord.Interaction,
        time_utc: str,
        date_utc: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if not _is_valid_time_utc(time_utc or ""):
            return await interaction.followup.send(
                "Time must be `HH:MM` 24h (UTC). Try `00:00` or `12:00`.",
                ephemeral=True,
            )
        if date_utc and not _is_valid_date_utc(date_utc):
            return await interaction.followup.send("Date must be `YYYY-MM-DD` (UTC).", ephemeral=True)

        today = dt.date.today()
        y, m, d = (today.year, today.month, today.day) if not date_utc else map(int, date_utc.split("-"))
        hh, mm = map(int, time_utc.split(":"))
        base = dt.datetime(y, m, d, hh, mm, tzinfo=dt.timezone.utc)

        def tz_line(label: str, tzname: str) -> str:
            try:
                from zoneinfo import ZoneInfo
                local = base.astimezone(ZoneInfo(tzname))
                badge = ""
                if local.date() > base.date():
                    badge = " _(next day)_"
                elif local.date() < base.date():
                    badge = " _(prev. day)_"
                return f"• **{label}** — {local.strftime('%H:%M')}{badge}"
            except Exception:
                return f"• **{label}** — (unavailable)"

        lines = [
            tz_line("Los Angeles (PT)", "America/Los_Angeles"),
            tz_line("US Mountain", "America/Denver"),
            tz_line("US Central / Mexico City", "America/Chicago"),
            tz_line("New York (ET)", "America/New_York"),
            tz_line("United Kingdom", "Europe/London"),
            tz_line("Germany (CET/CEST)", "Europe/Berlin"),
            tz_line("Argentina", "America/Argentina/Buenos_Aires"),
        ]

        await interaction.followup.send(
            f"**{base.strftime('%Y-%m-%d %H:%M')} UTC** converts to:\n" + "\n".join(lines),
            ephemeral=True,
        )

    # ---------- /titles help ----------
    @app_commands.command(name="help", description="How to use the title commands")
    async def help_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        msg = (
            "**Commands**\n"
            "• `/titles reserve title:<pick> ign:<name> coords:<X:Y or -> date_utc:<YYYY-MM-DD> time_utc:<HH:MM>` — book a slot\n"
            "• `/titles list` — see requestable titles\n"
            "• `/titles timeguide time_utc:<HH:MM> [date_utc:<YYYY-MM-DD>]` — quick conversions\n"
            "\n"
            "All scheduling is in **UTC** (dashboard uses the same).\n"
            f"Dashboard: {DASHBOARD_BASE_URL or '(not configured)'}\n"
        )
        await interaction.followup.send(msg, ephemeral=True)

    # ---------- Admin placeholders (graceful UX) ----------
    admin = app_commands.Group(name="admin", description="Admin actions (requires server API enabled)")

    @admin.command(name="force_release", description="(admin) Force-release a title")
    @app_commands.describe(title="Title to release now")
    @app_commands.autocomplete(title=_title_autocomplete)
    async def admin_force_release(self, interaction: discord.Interaction, title: str):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            "This command requires a small authenticated API on the server.\n"
            "Until then, use the **Admin Dashboard → Force Release**.",
            ephemeral=True,
        )

    @admin.command(name="assign", description="(admin) Manually assign a title immediately")
    @app_commands.describe(
        title="Title to assign",
        ign="IGN to assign",
        hours="Duration (hours) for timed titles; leave blank for default",
    )
    @app_commands.autocomplete(title=_title_autocomplete)
    async def admin_assign(self, interaction: discord.Interaction, title: str, ign: str, hours: Optional[int] = None):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            "This command requires a small authenticated API on the server.\n"
            "Until then, use the **Admin Dashboard → Manual Assign**.",
            ephemeral=True,
        )

    @admin.command(name="set_shift_hours", description="(admin) Set default shift hours for timed titles")
    @app_commands.describe(hours="1–72")
    async def admin_set_shift(self, interaction: discord.Interaction, hours: int):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            "This command requires a small authenticated API on the server.\n"
            "Until then, use the **Admin Dashboard → Set Shift Hours**.",
            ephemeral=True,
        )


# ---------- Bot hookup ----------
class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        grp = TitlesGroup()
        self.tree.add_command(grp)
        # Nest admin subgroup
        grp.add_command(grp.admin)
        await self.tree.sync()


# If running standalone:
# bot = MyBot()
# bot.run(os.getenv("DISCORD_TOKEN"))