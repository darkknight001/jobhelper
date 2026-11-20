"""Background schedule (IST): discovery, apply queue, Gmail polling, daily summary."""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import config, notify, pause, pipeline, portal_status, prep, scoring, summary, tracking
from .apply import engine

log = logging.getLogger(__name__)


# Jobs a pause (/pause in Telegram, or the dashboard) holds back. Gmail checks and summaries
# keep running so interview and recruiter alerts still reach you.
PAUSABLE = {"discover", "apply", "catch-up scoring", "prep retry"}


async def _safe(name: str, coro_fn, *args, **kwargs):
    if name in PAUSABLE and pause.is_paused():
        log.info("%s skipped: paused", name)
        return
    try:
        out = await coro_fn(*args, **kwargs)
        log.info("%s done: %s", name, out)
    except Exception:
        log.exception("%s failed", name)


async def _pause_watch() -> None:
    """Say so when a timed pause runs out."""
    if pause.expired():
        await notify.send("▶ JobHelper is running again: the pause has ended.", kind="pause")


def build() -> AsyncIOScheduler:
    s = config.settings()["schedule"]
    tz = config.tz()
    sch = AsyncIOScheduler(timezone=tz)
    common = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 900}
    sch.add_job(_safe, CronTrigger.from_crontab(s["discover_cron"], timezone=tz),
                args=["discover", pipeline.discover], id="discover", **common)
    sch.add_job(_safe, CronTrigger.from_crontab(s["apply_cron"], timezone=tz),
                args=["apply", engine.run_queue], id="apply", **common)
    sch.add_job(_safe, IntervalTrigger(minutes=int(s.get("email_poll_minutes", 15)), timezone=tz),
                args=["email", tracking.poll], id="email", **common)
    hh, mm = (int(x) for x in s.get("daily_summary", "21:00").split(":"))
    sch.add_job(_safe, CronTrigger(hour=hh, minute=mm, timezone=tz),
                args=["summary", summary.send_daily], id="summary", **common)
    # Off-peak catch-up: work deferred by the usage governor (unscored jobs, prep briefs) runs
    # when you're unlikely to be using Claude yourself. Prep briefs are also retried hourly.
    hh2, mm2 = (int(x) for x in s.get("catchup", "02:00").split(":"))
    sch.add_job(_safe, CronTrigger(hour=hh2, minute=mm2, timezone=tz),
                args=["catch-up scoring", scoring.score_pending], id="catchup_scoring", **common)
    sch.add_job(_safe, CronTrigger(hour=22, minute=15, timezone=tz),   # after the apply window
                args=["portal status", portal_status.check], id="portal_status", **common)
    sch.add_job(_safe, CronTrigger(hour=7, minute=10, timezone=tz),
                args=["learn company senders", tracking.learn_senders], id="learn_senders", **common)
    sch.add_job(_safe, CronTrigger(minute=35, timezone=tz),
                args=["prep retry", prep.retry_pending], id="prep_retry", **common)
    sch.add_job(_pause_watch, IntervalTrigger(minutes=1, timezone=tz), id="pause_watch", **common)
    weekly = s.get("weekly_summary")   # e.g. "sun 19:00"; empty to disable
    if weekly:
        dow, hhmm = weekly.split()
        wh, wm = (int(x) for x in hhmm.split(":"))
        sch.add_job(_safe, CronTrigger(day_of_week=dow, hour=wh, minute=wm, timezone=tz),
                    args=["weekly", summary.send_weekly], id="weekly", **common)
    return sch
