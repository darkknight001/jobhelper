"""Claude usage ledger and limit governor.

JobHelper shares the user's Claude subscription with their own interactive use, so it has
to be a polite tenant:

* every call's tokens are recorded (`LLMUsage`) so the dashboard shows where usage goes;
* every `rate_limit_event` the SDK streams updates the live limit readings (`RateLimitState`:
  status, utilization, reset time per window: five_hour, seven_day, seven_day_opus, ...);
* `allowed(task)` pauses work by priority as a window fills up. Background work (scoring,
  prep briefs) stops first, then applying; recruiter-mail triage keeps going longest, because
  missing an interview invite is the worst outcome. A rejected window pauses everything until
  it resets.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from . import config, db, notify

log = logging.getLogger(__name__)

# The chat assistant is the user asking in person, so it gets the highest tier.
TIER = {"scoring": "background", "prep": "background", "apply": "apply", "email": "email", "health": "email",
        "assistant": "email"}
DEFAULTS = {"background_max_utilization": 0.60, "apply_max_utilization": 0.75, "email_max_utilization": 0.95,
            "max_apply_runs_per_day": 12}
# Claude Code says e.g. "You've hit your 5-hour limit · resets 1:30pm (Asia/Kolkata)".
LIMIT_TEXT = re.compile(r"hit your [\w -]*limit|usage limit|rate limit|limit reached|too many requests|"
                        r"\b429\b|overloaded", re.I)
RESETS_AT = re.compile(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)?", re.I)


def _parse_reset(text: str) -> datetime | None:
    """'resets 1:30pm' → the next 13:30 in the user's timezone, as UTC."""
    m = RESETS_AT.search(text or "")
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    now = datetime.now(config.tz())
    at = now.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
    if at <= now:
        at += timedelta(days=1)
    return at.astimezone(timezone.utc)


def _gov() -> dict[str, Any]:
    return {**DEFAULTS, **(config.settings().get("llm", {}).get("governor") or {})}


# ------------------------------------------------------------------ observing the SDK stream

def observe(msg: Any) -> None:
    """Feed every message from an SDK stream here; rate-limit events update the readings."""
    info = getattr(msg, "rate_limit_info", None)
    if info is None:
        return
    kind = info.rate_limit_type or "unknown"
    resets = datetime.fromtimestamp(info.resets_at, timezone.utc) if info.resets_at else None
    with db.session() as s:
        row = s.get(db.RateLimitState, kind) or db.RateLimitState(limit_type=kind, status=info.status)
        row.status, row.utilization, row.resets_at, row.updated_at = (
            info.status, info.utilization, resets, db.utcnow())
        s.merge(row)


def record(result: Any, *, task: str, ref: str | None = None) -> None:
    """Store what a finished call consumed (one row per model it used)."""
    if result is None:
        return
    ok = not getattr(result, "is_error", False)
    turns = getattr(result, "num_turns", 0) or 0
    dur = getattr(result, "duration_ms", 0) or 0
    per_model = getattr(result, "model_usage", None) or {}
    with db.session() as s:
        if per_model:
            for model, u in per_model.items():
                s.add(db.LLMUsage(task=task, model=model, ref=ref, ok=ok, turns=turns, duration_ms=dur,
                                  input_tokens=u.get("inputTokens", 0), output_tokens=u.get("outputTokens", 0),
                                  cache_read_tokens=u.get("cacheReadInputTokens", 0),
                                  cache_write_tokens=u.get("cacheCreationInputTokens", 0),
                                  cost_usd=float(u.get("costUSD", 0) or 0)))
        else:
            u = getattr(result, "usage", None) or {}
            s.add(db.LLMUsage(task=task, model=None, ref=ref, ok=ok, turns=turns, duration_ms=dur,
                              input_tokens=u.get("input_tokens", 0), output_tokens=u.get("output_tokens", 0),
                              cache_read_tokens=u.get("cache_read_input_tokens", 0),
                              cache_write_tokens=u.get("cache_creation_input_tokens", 0),
                              cost_usd=float(getattr(result, "total_cost_usd", 0) or 0)))
    text = " ".join(str(x) for x in (getattr(result, "errors", None) or []) + [getattr(result, "result", "") or ""])
    if getattr(result, "api_error_status", None) == 429 or (not ok and LIMIT_TEXT.search(text)):
        note_limit_hit(text)


def note_limit_hit(text: str = "") -> None:
    """A call failed on a limit: pause until the reset time it states, else for 30 minutes."""
    with db.session() as s:
        kind = "seven_day" if re.search(r"weekly|7-day|seven", text, re.I) else "five_hour"
        row = s.get(db.RateLimitState, kind)
        until = _parse_reset(text) or db.utcnow() + timedelta(minutes=30)
        if row is None:
            row = db.RateLimitState(limit_type=kind, status="rejected", resets_at=until)
        elif row.resets_at is None or row.resets_at < until:
            row.status, row.resets_at = "rejected", until
        else:
            row.status = "rejected"
        row.updated_at = db.utcnow()
        s.merge(row)
    log.warning("Claude limit hit (%s); pausing", text[:120])


# ------------------------------------------------------------------ the governor

def readings() -> list[dict[str, Any]]:
    """Current, non-stale limit readings."""
    now = db.utcnow()
    out = []
    with db.session() as s:
        for r in s.scalars(select(db.RateLimitState)):
            if r.resets_at is not None and r.resets_at <= now:
                continue                                   # that window has reset
            if r.resets_at is None and r.updated_at < now - timedelta(minutes=30):
                continue                                   # old reading, no reset time
            out.append({"type": r.limit_type, "status": r.status, "utilization": r.utilization,
                        "resets_at": r.resets_at})
    return out


def _apply_runs_today(s) -> int:
    tzinfo = config.tz()
    start = datetime.combine(datetime.now(tzinfo).date(), time.min, tzinfo)
    return s.scalar(select(func.count(func.distinct(db.LLMUsage.ref))).where(
        db.LLMUsage.task == "apply", db.LLMUsage.at >= start)) or 0


def allowed(task: str) -> tuple[bool, str]:
    """May `task` call Claude right now? Returns (ok, reason when not)."""
    gov = _gov()
    tier = TIER.get(task, "background")
    limit = gov[f"{tier}_max_utilization"]
    for r in readings():
        when = r["resets_at"].astimezone(config.tz()).strftime("%H:%M") if r["resets_at"] else "soon"
        label = r["type"].replace("_", " ")
        if r["status"] == "rejected":
            return False, f"your Claude {label} limit is reached (resets {when} IST)"
        if r["utilization"] is not None and r["utilization"] >= limit:
            return False, (f"your Claude {label} window is {r['utilization']:.0%} used; {tier} work pauses at "
                           f"{limit:.0%} to leave you headroom (resets {when} IST)")
    if task == "apply":
        with db.session() as s:
            n = _apply_runs_today(s)
        if n >= gov["max_apply_runs_per_day"]:
            return False, f"today's ceiling of {gov['max_apply_runs_per_day']} apply runs is reached"
    return True, ""


async def notify_paused(task: str, reason: str) -> None:
    """One Telegram notice per pause reason per few hours, not one per skipped call."""
    tier = TIER.get(task, "background")
    key = f"{tier}: {reason}"
    with db.session() as s:
        recent = s.scalar(select(db.Event).where(db.Event.kind == "llm_paused", db.Event.summary == key,
                                                 db.Event.at >= db.utcnow() - timedelta(hours=4)))
        db.add_event(s, "llm_paused", key)
    if recent is None:
        what = {"background": "scoring and prep briefs", "apply": "applying", "email": "email triage"}[tier]
        await notify.send(f"⏸ JobHelper paused {what}: {reason}. It resumes on its own; nothing is lost.",
                          kind="usage")


# ------------------------------------------------------------------ reporting

def today() -> dict[str, Any]:
    tzinfo = config.tz()
    start = datetime.combine(datetime.now(tzinfo).date(), time.min, tzinfo)
    with db.session() as s:
        rows = s.execute(select(db.LLMUsage.task, func.count(db.LLMUsage.id), func.sum(db.LLMUsage.turns),
                                func.sum(db.LLMUsage.input_tokens + db.LLMUsage.cache_write_tokens),
                                func.sum(db.LLMUsage.cache_read_tokens), func.sum(db.LLMUsage.output_tokens),
                                func.sum(db.LLMUsage.cost_usd))
                         .where(db.LLMUsage.at >= start).group_by(db.LLMUsage.task)).all()
        apply_runs = _apply_runs_today(s)
    by_task = {t: {"calls": c, "turns": tu or 0, "input": i or 0, "cache_read": cr or 0, "output": o or 0,
                   "cost": round(cost or 0, 2)} for t, c, tu, i, cr, o, cost in rows}
    gov = _gov()
    paused = {tier: allowed(task)[1] for task, tier in (("scoring", "background"), ("apply", "apply"),
                                                        ("email", "email"))}
    return {"by_task": by_task, "total_cost": round(sum(v["cost"] for v in by_task.values()), 2),
            "readings": readings(), "apply_runs": apply_runs, "max_apply_runs": gov["max_apply_runs_per_day"],
            "paused": {k: v for k, v in paused.items() if v}}
