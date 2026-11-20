"""Pause the workflow for a while: from the Telegram bot (/pause 2h) or the dashboard.

While paused, JobHelper doesn't search for jobs, score them or start applications (hourly prep
brief retries wait too). An application that's already running finishes; the next one waits.
Gmail is still checked, so interview, assessment and recruiter-question alerts (and the prep
brief for a shortlist) keep coming, and the daily and weekly summaries still go out. When the pause ends, everything
picks up again on the next scheduled run.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any

from . import config

STATE = config.DATA_DIR / "pause.json"
_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(minutes?|mins?|m|hours?|hrs?|h|days?|d)?", re.I)


def _read() -> dict[str, Any]:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write(st: dict[str, Any]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st), encoding="utf-8")
    try:
        os.chmod(STATE, 0o600)
    except OSError:
        pass


def parse_duration(text: str) -> timedelta | None:
    """'2h', '30m', '1h30m', '1d', '90' (minutes)… 'tomorrow' / 'morning' = until 9 AM.
    Returns None for 'until I resume' (no argument, 'off', 'indefinitely')."""
    t = (text or "").strip().lower()
    if t in ("", "indefinitely", "forever", "off", "until resume"):
        return None
    if t in ("tomorrow", "morning", "9am", "until tomorrow"):
        now = datetime.now(config.tz())
        nine = datetime.combine(now.date(), time(9, 0), config.tz())
        if nine <= now:
            nine += timedelta(days=1)
        return nine - now
    total = timedelta()
    pos = 0
    for m in _PART.finditer(t.replace(" ", "")):
        if m.start() != pos:
            raise ValueError(f"can't read “{text}”")
        n, unit = float(m.group(1)), (m.group(2) or "m").lower()
        total += timedelta(days=n) if unit.startswith("d") else (
            timedelta(hours=n) if unit.startswith("h") else timedelta(minutes=n))
        pos = m.end()
    if pos != len(t.replace(" ", "")) or total <= timedelta():
        raise ValueError(f"can't read “{text}”; try 30m, 2h, 1d or tomorrow")
    if total > timedelta(days=30):
        raise ValueError("pauses are capped at 30 days")
    return total


def pause(duration: timedelta | None, *, via: str = "dashboard") -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    st = {"since": now.isoformat(), "via": via,
          "until": (now + duration).isoformat() if duration else None}
    _write(st)
    return current() or {}


def resume() -> bool:
    """Lift the pause now. Returns whether there was one."""
    was = bool(_read())
    _write({})
    return was


def current() -> dict[str, Any] | None:
    """The active pause, or None. A pause whose time is up counts as lifted."""
    st = _read()
    if not st.get("since"):
        return None
    until = datetime.fromisoformat(st["until"]) if st.get("until") else None
    if until and until <= datetime.now(timezone.utc):
        return None
    return {"since": datetime.fromisoformat(st["since"]), "until": until, "via": st.get("via", ""),
            "label": label(until)}


def expired() -> bool:
    """True once, when a timed pause has run out: clears it so the caller can say so."""
    st = _read()
    if st.get("until") and datetime.fromisoformat(st["until"]) <= datetime.now(timezone.utc):
        _write({})
        return True
    return False


def is_paused() -> bool:
    return current() is not None


def label(until: datetime | None) -> str:
    if until is None:
        return "until you resume"
    local = until.astimezone(config.tz())
    today = datetime.now(config.tz()).date()
    day = "" if local.date() == today else ("tomorrow " if local.date() == today + timedelta(days=1)
                                             else local.strftime("%a %d %b "))
    return f"until {day}{local.strftime('%I:%M %p').lstrip('0')} {local.tzname() or ''}".rstrip()


def pause_now(arg: str, via: str = "telegram") -> str:
    """Pause from free text ("2h", "30", "tomorrow", "" = until resumed); the reply to show the user."""
    raw = arg.strip()
    try:
        dur = parse_duration(raw + "m" if raw.isdigit() else raw)
    except ValueError as exc:
        return f"Not paused: {exc}."
    p = pause(dur, via=via)
    return (f"⏸ Paused {p['label']}. No job search, scoring or new applications until then "
            f"(a run already going finishes first). Gmail alerts and summaries keep coming. /resume to undo.")


def resume_now() -> str:
    """Resume and restart the apply queue shortly; the reply to show the user."""
    if not resume():
        return "JobHelper wasn't paused."
    asyncio.create_task(_continue_soon(5))
    return "▶ Resumed. The apply queue picks up now; job search runs at its next scheduled time."


async def _continue_soon(seconds: int) -> None:
    from .apply import engine   # engine imports this module, so import it at call time
    await asyncio.sleep(seconds)
    await engine.run_queue()
