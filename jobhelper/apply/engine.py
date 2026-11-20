"""Apply queue: caps, pacing, review decisions, and turning agent results into state."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select

from .. import config, db, kb, mail, notify, pause, telegram_bot, usage
from ..browser import BROWSER
from ..prefilter import too_old
from ..scoring import portal_of
from . import agent
from .tools import RunContext

log = logging.getLogger(__name__)

_running = asyncio.Lock()


def _in_window() -> bool:
    lo, hi = config.settings()["apply"].get("window_hours", [9, 22])
    return lo <= datetime.now(config.tz()).hour < hi


def applied_today(s, portal: str) -> int:
    tzinfo = config.tz()
    start = datetime.combine(datetime.now(tzinfo).date(), time.min, tzinfo)
    return s.scalar(select(func.count(db.Application.id)).where(
        db.Application.portal == portal, db.Application.applied_at >= start)) or 0


# ------------------------------------------------------------------ review decisions

def approve(job_id: int, note: str | None = None, *, via: str = "dashboard") -> str:
    with db.session() as s:
        job = s.get(db.Job, job_id)
        if job is None:
            return "unknown job"
        if job.status not in ("review", "skipped", "scored"):
            return f"job is {job.status}, not awaiting review"
        job.status, job.reviewed_at, job.review_note = "approved", db.utcnow(), note or job.review_note
        app_ = db.Application(job_id=job.id, portal=portal_of(job.source), mode="approved", status="queued")
        s.add(app_)
        s.flush()
        db.add_event(s, "approved", f"Approved via {via}", application=app_)
        return f"queued #{job.id} {job.title} at {job.company}"


def dismiss(job_id: int, note: str | None = None, *, via: str = "dashboard") -> str:
    with db.session() as s:
        job = s.get(db.Job, job_id)
        if job is None:
            return "unknown job"
        job.status, job.reviewed_at, job.review_note = "dismissed", db.utcnow(), note
        db.add_event(s, "dismissed", f"Dismissed via {via}", job_id=job.id)
        return f"dismissed #{job.id}"


async def mark_submitted(application_id: int, *, via: str = "dashboard") -> str:
    """The user submitted a dry-run-filled form themselves. Record it and keep evidence."""
    with db.session() as s:
        a = s.get(db.Application, application_id)
        if a is None or a.status not in ("awaiting_review", "dry_run_ok", "review_expired", "apply_yourself"):
            return "nothing awaiting review with that id"
        in_own_browser = a.status == "apply_yourself"
    shot = config.ARTIFACT_DIR / f"app-{application_id}" / "submitted.png"
    try:   # best effort: whatever the remote browser shows now is usually the confirmation page
        if in_own_browser:
            raise RuntimeError("submitted in the user's own browser")
        ctx = await BROWSER.context()
        pages = agent.PARKED.get(application_id) or ctx.pages   # its own tab when JobHelper kept one
        if pages:
            shot.parent.mkdir(parents=True, exist_ok=True)
            await pages[-1].screenshot(path=str(shot))
    except Exception:
        shot = None
    await agent.release_tabs(application_id)
    with db.session() as s:
        a = s.get(db.Application, application_id)
        a.status, a.applied_at, a.last_update_at = "applied", db.utcnow(), db.utcnow()
        a.job.status = "applied"
        a.notes = (a.notes or "") + (f"\nSubmitted by you in your own browser (via {via})." if in_own_browser
                                     else f"\nSubmitted by you after review (via {via}).")
        if shot and shot.exists():
            a.artifacts = [*(a.artifacts or []), f"app-{application_id}/submitted.png"]
        db.add_event(s, "applied_manual", f"You submitted it after reviewing the filled form (via {via})",
                     application=a)
        return f"recorded #{a.id} {a.job.title} at {a.job.company} as applied"


REMOVABLE = ("queued", "needs_user", "failed", "awaiting_review", "dry_run_ok", "review_expired", "apply_yourself")
ALREADY_APPLIED = "Already applied"
SKIP_REASONS = ("No longer accepting applications", ALREADY_APPLIED, "Posting closed or expired", "Not a fit after all",
                "Duplicate or aggregator listing", "Other")


def remove_application(application_id: int, reason: str = "", *, via: str = "dashboard") -> str:
    """Take a job out of the apply queue for good (closed posting, not a fit, duplicate...).
    Releases the review hold if the queue was waiting on it."""
    with db.session() as s:
        a = s.get(db.Application, application_id)
        if a is None:
            return "unknown application"
        if a.status not in REMOVABLE:
            return f"application is {a.status.replace('_', ' ')}; nothing to skip"
        reason = (reason or "Skipped").strip()[:200]
        agent.release_tabs_soon(a.id)
        a.last_update_at, a.blocker = db.utcnow(), None
        a.notes = (a.notes or "") + f"\nSkipped by you: {reason} (via {via})."
        if reason == ALREADY_APPLIED:   # recorded like the agent's already_applied: an application, not a dismissal
            a.status, a.job.status = "already_applied", "applied"
            db.add_event(s, "already_applied", f"You marked it already applied (via {via})", application=a)
        else:
            a.status = "skipped_by_user"
            a.job.status, a.job.review_note = "dismissed", reason
            db.add_event(s, "skipped", f"You skipped it: {reason} (via {via})", application=a)
        return f"skipped #{a.id} {a.job.title} at {a.job.company}"


def _holding() -> int | None:
    """The application the queue is waiting on, expiring holds older than review_hold_hours."""
    hours = config.settings()["apply"].get("review_hold_hours", 12)
    with db.session() as s:
        for a in s.scalars(select(db.Application).where(db.Application.status == "awaiting_review")):
            if (a.last_update_at or a.created_at) < db.utcnow() - timedelta(hours=hours):
                a.status = "review_expired"
                db.add_event(s, "review_expired", f"No decision within {hours}h; the queue moved on. "
                             "You can still submit it yourself and tap \"I submitted it\".", application=a)
                continue
            return a.id
    return None


# ------------------------------------------------------------------ restart / time limit

_current: dict[str, Any] = {}          # {"app_id": int, "task": asyncio.Task} while an agent runs


async def _run_one(app_id: int) -> dict[str, Any]:
    """apply_one as its own task, so Restart can cancel it and a time limit can stop a hung run."""
    minutes = config.settings()["apply"].get("run_timeout_minutes", 20)
    task = asyncio.create_task(apply_one(app_id))
    _current.update(app_id=app_id, task=task)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=minutes * 60)
    except asyncio.TimeoutError:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        with db.session() as s:
            a = s.get(db.Application, app_id)
            a.status, a.blocker = "needs_user", f"timed_out: no result after {minutes} minutes"
            a.job.status = "needs_user"
            db.add_event(s, "apply_timed_out", f"Stopped after {minutes} minutes without a result", application=a)
            title, company = a.job.title, a.job.company
        await notify.send(f"⏱ Stopped {title} at {company}: no result after {minutes} min. "
                          f"Tap Restart or Skip: {notify.link(f'/applications/{app_id}')}", kind="blocker",
                          buttons=telegram_bot.application_buttons(app_id, "timed_out"))
        return {"application": app_id, "status": "timed_out"}
    except asyncio.CancelledError:
        if task.cancelled() and _current.get("restarting") == app_id:
            return {"application": app_id, "status": "restarted"}   # the user pressed Restart
        raise
    finally:
        _current.clear()


RESTARTABLE = ("applying", "needs_user", "failed", "awaiting_review", "dry_run_ok", "review_expired",
               "apply_yourself")


async def run_after_restart(application_id: int) -> dict[str, Any]:
    """Wait for any batch to wind down, then run the restarted application on its own."""
    for _ in range(120):
        if not _running.locked():
            break
        await asyncio.sleep(1)
    return await run_queue(max_runs=1, ignore_window=True, only=[application_id])


async def restart_application(application_id: int, *, via: str = "dashboard") -> str:
    """Start an application again from scratch: stop its run if one is live, re-queue it, and
    run it next (ahead of the rest of the queue)."""
    with db.session() as s:
        a = s.get(db.Application, application_id)
        if a is None:
            return "unknown application"
        if a.status not in RESTARTABLE:
            return f"application is {a.status.replace('_', ' ')}; nothing to restart"
    task = _current.get("task")
    if _current.get("app_id") == application_id and task is not None and not task.done():
        _current["restarting"] = application_id
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
    with db.session() as s:
        a = s.get(db.Application, application_id)
        a.status, a.blocker, a.last_update_at = "queued", None, db.utcnow()
        a.job.status = "approved" if a.mode == "approved" else "queued_auto"
        db.add_event(s, "restarted", f"Restarted by you (via {via})", application=a)
    await agent.release_tabs(application_id)   # a restart starts from scratch in a new tab
    return f"restarting #{application_id}"


def recover_stranded() -> int:
    """At startup: no agent survives a restart, so anything still 'applying' goes back in the queue."""
    with db.session() as s:
        n = 0
        for a in s.scalars(select(db.Application).where(db.Application.status == "applying")):
            a.status = "queued"
            db.add_event(s, "requeued", "Re-queued: JobHelper restarted while this was being filled", application=a)
            n += 1
        return n


def last_block(s, application_id: int) -> dict[str, Any]:
    """What the latest run stopped on: kind, details, url and, for questions, question + options."""
    ev = s.scalar(select(db.Event).where(db.Event.application_id == application_id,
                                         db.Event.kind == "apply_needs_user").order_by(db.Event.id.desc()))
    return (ev.detail or {}) if ev is not None else {}


def move_to_apply_yourself(application_id: int, *, via: str = "dashboard") -> str:
    """You'd rather finish a stuck application in your own browser: it leaves the queue and
    shows up with the apply-yourself ones ("I submitted it" / Skip)."""
    with db.session() as s:
        a = s.get(db.Application, application_id)
        if a is None or a.status != "needs_user":
            return "nothing waiting on you with that id"
        block = last_block(s, a.id)
        a.final_url = block.get("url") or a.final_url or a.job.apply_url or a.job.url
        a.notes = (a.notes or "") + f"\nWas waiting on: {a.blocker}" if a.blocker else a.notes
        a.status, a.blocker, a.last_update_at = "apply_yourself", None, db.utcnow()
        a.job.status = "apply_yourself"
        db.add_event(s, "apply_yourself", f"You chose to apply yourself (via {via})", application=a)
        out = f"#{a.id} {a.job.title} at {a.job.company}: apply in your own browser, then tap I submitted it\n{a.final_url}"
    agent.release_tabs_soon(application_id)   # its parked tab is no longer needed
    return out


def resume_blocked(application_id: int, answer: str | None = None, *, via: str = "dashboard") -> str:
    """Put a needs_user application back at the front of the queue. Its next run continues in the
    tab it left open. With `answer` (to the question it stopped on), the answer is saved to the KB
    first, so later applications answer that question themselves."""
    with db.session() as s:
        a = s.get(db.Application, application_id)
        if a is None or a.status != "needs_user":
            return "nothing to resume"
        block, company = last_block(s, a.id), a.job.company
    answer = (answer or "").strip()[:1000]
    question = block.get("question") or ""
    if answer:
        if not question:
            return "that application isn't waiting on a question; finish the step in the remote browser and tap Resume"
        kb.add_note(f"Question: {question}\nAnswer: {answer}", source=f"{via}, application #{application_id} at {company}",
                    topic=f"Screening answer: {question[:80]}")
    with db.session() as s:
        a = s.get(db.Application, application_id)
        if a.status != "needs_user":
            return "nothing to resume"
        prev, a.status, a.blocker = a.blocker, "queued", None
        db.add_event(s, "resumed", f"You answered: {answer}" if answer else "Resumed after manual step",
                     application=a, detail={"blocker": prev, "url": block.get("url"), "question": question,
                                            "answer": answer or None})
    if answer:
        return f"Saved to your knowledge base. Continuing #{application_id} next."
    return f"re-queued application {application_id}"


async def run_when_free(application_id: int, wait_seconds: int = 1800) -> dict[str, Any]:
    """Run one resumed application as soon as no other apply run holds the browser. (It is also
    first in line for the next scheduled batch, whichever comes first.)"""
    for _ in range(wait_seconds // 5):
        if not _running.locked():
            break
        await asyncio.sleep(5)
    return await run_queue(max_runs=1, ignore_window=True, only=[application_id])


# ------------------------------------------------------------------ queue runner

def _next_batch(limit: int) -> list[int]:
    cap = config.settings()["apply"]["daily_cap_per_portal"]
    with db.session() as s:
        rows = s.execute(
            select(db.Application.id, db.Application.portal)
            .join(db.Job, db.Job.id == db.Application.job_id)
            .where(db.Application.status == "queued")
            # Resumed / re-queued applications first: their tab is waiting in the browser.
            .order_by((db.Application.attempts > 0).desc(), db.Job.priority.desc(),
                      (db.Application.mode == "approved").desc(),
                      db.Job.score.desc().nullslast(), db.Application.created_at)
        ).all()
        picked, used = [], {}
        for app_id, portal in rows:
            used.setdefault(portal, applied_today(s, portal))
            if used[portal] < cap:
                picked.append(app_id)
                used[portal] += 1
            if len(picked) >= limit:
                break
        return picked


def _only(app_ids: list[int]) -> list[int]:
    """The given applications that are queued and still within today's cap."""
    cap = config.settings()["apply"]["daily_cap_per_portal"]
    with db.session() as s:
        out = []
        for i in app_ids:
            a = s.get(db.Application, i)
            if a is not None and a.status == "queued" and applied_today(s, a.portal) < cap:
                out.append(i)
        return out


# Sites whose bot protection (Cloudflare & co.) rejects the automated browser even when a human
# clicks. Jobs there go to "apply yourself" instead of burning an agent run. Starts with the
# ones seen so far; the agent adds more as it meets them (stored in apply.manual_sites).
BUILTIN_MANUAL_SITES = ("workable.com",)
BOT_CHECK = re.compile(r"cloudflare|turnstile|verify (that )?you are (a )?human|are you human|hcaptcha|recaptcha|"
                       r"captcha|bot[_ -]?check|human verification", re.I)


def manual_sites() -> list[str]:
    learned = config.settings()["apply"].get("manual_sites") or []
    return sorted({*BUILTIN_MANUAL_SITES, *learned})


def _site(url: str | None) -> str:
    host = re.sub(r"^https?://", "", url or "").split("/")[0]
    return mail.registrable(host) if host else ""


def remember_manual_site(url: str | None) -> str | None:
    site = _site(url)
    if site and site not in manual_sites() and site not in ("linkedin.com", "naukri.com", "indeed.com"):
        learned = config.settings()["apply"].get("manual_sites") or []
        config.set_override("apply.manual_sites", sorted({*learned, site}))
        return site
    return None


async def _send_apply_yourself(application_id: int, title: str, company: str, url: str, why: str) -> None:
    await notify.send(
        f"🖐 Apply yourself: {title} at {company}\n{why}\n"
        f"Open this in your own browser (phone or laptop) and submit there:\n{url}\n"
        f"Then tap \"I submitted it\" (or Skip): {notify.link(f'/applications/{application_id}')}\n"
        f"In Telegram: \"submitted {application_id}\".",
        kind="apply_yourself", buttons=telegram_bot.application_buttons(application_id, "apply_yourself"))


async def apply_one(application_id: int) -> dict[str, Any]:
    s_ = config.settings()
    dry = bool(s_["apply"].get("dry_run", True))
    with db.session() as s:
        a = s.get(db.Application, application_id)
        job = a.job
        a.status, a.started_at, a.attempts = "applying", db.utcnow(), a.attempts + 1
        start_url = job.apply_url if (job.source in ("greenhouse", "lever", "ashby") and job.apply_url) else job.url
        resume = config.RESUME_DIR / (s_["apply"].get("resume") or job.resume_variant or "")
        if not resume.is_file():
            resume = next(iter(sorted(config.RESUME_DIR.glob("*.pdf"))), resume)
        job_info = {"id": job.id, "title": job.title, "company": job.company, "url": job.url,
                    "apply_url": job.apply_url, "location": job.location, "source": job.source,
                    "description": (job.description or "")[:8000],
                    "fit_summary": (job.score_detail or {}).get("summary"),
                    "fit_reasons": (job.score_detail or {}).get("reasons")}
        portal = portal_of(job.source)
        last = s.scalar(select(db.Event).where(db.Event.application_id == a.id).order_by(db.Event.id.desc()))
        resumed_from = None
        if last is not None and last.kind == "resumed":
            d = last.detail or {}
            resumed_from = (f"The previous run stopped with: {d.get('blocker')}"
                            + (f"\nIts page: {d['url']}" if d.get("url") else "")
                            + (f"\nThe user answered \"{d.get('question')}\" with: {d['answer']}\nEnter exactly that "
                               "answer there (it is now in the KB too)." if d.get("answer") else
                               "\nThe user says they have handled that in this same browser."))
        if too_old(job.posted_at, job.discovered_at, s_):
            days = s_["apply"].get("max_job_age_days", 7)
            a.status, a.last_update_at, job.status = "not_eligible", db.utcnow(), "skipped"
            db.add_event(s, "too_old", f"Not applied: posted over {days} days ago", application=a)
            return {"application": application_id, "status": "not_eligible"}

    # Known automation-blocking site? Hand it to the user straight away, no agent run.
    if _site(start_url) in manual_sites() or _site(job_info.get("apply_url")) in manual_sites():
        url = job_info.get("apply_url") or start_url
        with db.session() as s:
            a = s.get(db.Application, application_id)
            a.status, a.final_url, a.last_update_at = "apply_yourself", url, db.utcnow()
            a.job.status = "apply_yourself"
            db.add_event(s, "apply_yourself", f"{_site(url)} blocks automated browsers; apply in your own browser",
                         application=a)
        await _send_apply_yourself(application_id, job_info["title"], job_info["company"], url,
                                   f"{_site(url)} blocks automated browsers, so JobHelper didn't try.")
        return {"application": application_id, "status": "apply_yourself"}

    ctx = RunContext(application_id=application_id, job=job_info, resume_path=resume, dry_run=dry,
                     manual_sites=manual_sites(), resumed_from=resumed_from)
    for dom in ctx.job_domains():
        ctx.mail_domains.add(mail.allow_domain_temporarily(dom, trusted=True))
    try:
        res = await agent.run(ctx, portal, start_url)
    finally:   # the career site's mail is trusted only while its run lasts
        mail.forget_temporary_domains(ctx.mail_domains)
    if res.get("status") == "deferred":       # hit a Claude limit mid-run: back in the queue
        with db.session() as s:
            a = s.get(db.Application, application_id)
            a.status, a.last_update_at = "queued", db.utcnow()
            db.add_event(s, "apply_deferred", (res.get("notes") or "")[:500], application=a)
        return {"application": application_id, "status": "deferred"}
    status = res.get("status", "failed")
    status = {"ready_to_submit": "dry_run_ok"}.get(status, status)
    if status not in ("applied", "dry_run_ok", "already_applied", "not_eligible", "needs_user", "failed"):
        status = "failed"
    learned = None
    if status == "needs_user" and BOT_CHECK.search(f"{res.get('kind')} {res.get('details')}"):
        # A human check the remote browser can't pass: hand it to the user's own browser.
        status = "apply_yourself"
        learned = remember_manual_site(res.get("url") or res.get("final_url"))

    with db.session() as s:
        a = s.get(db.Application, application_id)
        job = a.job
        a.status, a.last_update_at = status, db.utcnow()
        a.answers = ctx.answers
        a.final_url = res.get("final_url") or res.get("url") or a.final_url
        a.notes = (res.get("evidence") or "") + ("\n" + res["notes"] if res.get("notes") else "")
        a.artifacts = [f"app-{application_id}/transcript.jsonl", f"app-{application_id}/final.png"]
        if status == "applied":
            a.applied_at = db.utcnow()
            job.status = "applied"
        elif status == "already_applied":
            job.status = "applied"
        elif status == "needs_user":
            a.blocker = f"{res.get('kind')}: {res.get('details')}"
            job.status = "needs_user"
        elif status == "apply_yourself":
            a.final_url = res.get("url") or a.final_url or job.apply_url or job.url
            job.status = "apply_yourself"
        elif status == "not_eligible":
            job.status = "skipped"
        elif status == "failed":
            job.status = "failed"
        else:  # dry_run_ok
            if s_["apply"].get("hold_for_review", True):
                # Review-then-submit: leave the filled form open and hold the queue until the
                # user submits it themselves ("I submitted it") or skips it.
                status = a.status = "awaiting_review"
            job.status = status
        detail = {"answers": len(ctx.answers)}
        if status == "needs_user":
            detail.update({k: res[k] for k in ("kind", "url", "question", "options") if res.get(k)})
        db.add_event(s, f"apply_{status}", (a.notes or a.blocker or status)[:1000], application=a, detail=detail)
        title, company, a_url = job.title, job.company, a.final_url or job.apply_url or job.url

    if status == "awaiting_review":
        hours = s_["apply"].get("review_hold_hours", 12)
        await notify.send(
            f"📝 Ready for your review: {title} at {company}\n"
            f"The form is filled and open in the remote browser; nothing was submitted.\n"
            f"Check it, submit it yourself if you're happy, then tap \"I submitted it\" (or \"Skip\"):\n"
            f"{notify.link(f'/applications/{application_id}')}\n"
            f"The queue waits for you (up to {hours}h). In Telegram: \"submitted {application_id}\" or "
            f"\"skip {application_id}\".",
            kind="review", buttons=telegram_bot.application_buttons(application_id, "awaiting_review"))

    if status == "apply_yourself":
        why = "The site shows a human check that the automated browser can't pass."
        if learned:
            why += f" JobHelper will send future {learned} jobs straight to you."
        await _send_apply_yourself(application_id, title, company, a_url, why)
    if status == "needs_user" and res.get("question"):
        await notify.send(
            f"❓ #{application_id} {title} at {company} asks:\n{res['question']}\n\n"
            f"Tap an answer, or reply to this message with yours. It's saved to your knowledge base and the "
            f"application continues where it stopped (its tab stays open; other jobs carry on meanwhile).\n"
            f"{notify.link(f'/applications/{application_id}')}",
            kind="blocker", buttons=telegram_bot.question_buttons(application_id, res.get("options") or []))
    elif status == "needs_user":
        await notify.send(
            f"⏸ Needs you: {title} at {company}\n{res.get('kind')}: {res.get('details')}\n"
            f"Its tab stays open in the remote browser (other jobs carry on in their own tabs). Finish the "
            f"step there, then tap Resume:\n{notify.link(f'/applications/{application_id}')}",
            kind="blocker", buttons=telegram_bot.application_buttons(application_id, "needs_user"))
    elif status == "failed":
        await notify.send(f"⚠️ Apply failed: {title} at {company}\n{(res.get('notes') or '')[:300]}\n"
                          f"{notify.link(f'/applications/{application_id}')}", kind="failure",
                          buttons=telegram_bot.application_buttons(application_id, "failed"))
    return {"application": application_id, "status": status}


async def _alert_paused(problem: str) -> None:
    """One Telegram alert per 6 hours while the apply queue is paused, not one per job."""
    with db.session() as s:
        recent = s.scalar(select(db.Event).where(db.Event.kind == "apply_paused",
                                                 db.Event.at >= db.utcnow() - timedelta(hours=6)))
        db.add_event(s, "apply_paused", problem)
    if recent is None:
        await notify.send(f"⚠️ Applying is paused: browser automation isn't working.\n{problem}\n"
                          f"Queued jobs stay queued; nothing was marked as needing you.\n{notify.link('/settings')}",
                          kind="failure")


async def run_queue(max_runs: int = 3, *, ignore_window: bool = False,
                    only: list[int] | None = None, ignore_pause: bool = False) -> dict[str, Any]:
    """Apply to the next queued jobs. `only` runs exactly those applications (e.g. the one
    the user just unblocked with Resume) instead of the front of the queue. A pause holds
    the queue back, except for applications the user picked by hand (`only`) and
    `ignore_pause` (the dashboard's "Run now")."""
    if not (only or ignore_pause) and pause.is_paused():
        return {"paused": pause.current()["label"]}
    if _running.locked():
        return {"skipped": "another apply run is in progress"}
    if not ignore_window and not _in_window():
        return {"skipped": "outside the apply window"}
    held = _holding()
    if held is not None:
        return {"held": f"waiting for your review of application {held}"}
    ok, why = usage.allowed("apply")
    if not ok:
        if _next_batch(1):      # only tell the user when there was something to apply to
            await usage.notify_paused("apply", why)
        return {"deferred": why}
    async with _running:
        with db.run_log("apply") as stats:
            results = []
            ids = _only(only) if only else _next_batch(max_runs)
            if not ids:
                stats["results"] = []
                return stats
            problem = await agent.preflight()
            if problem:
                stats["paused"] = problem
                await _alert_paused(problem)
                return stats
            pace = config.settings()["apply"].get("pacing_seconds", [90, 240])
            for i, app_id in enumerate(ids):
                if i:
                    await asyncio.sleep(random.uniform(*pace))
                    if not ignore_pause and pause.is_paused():   # paused mid-batch: the rest wait
                        stats["paused"] = "paused by you"
                        break
                    ok, why = usage.allowed("apply")
                    if not ok:          # a limit filled up mid-batch: stop, the rest stay queued
                        stats["deferred"] = why
                        await usage.notify_paused("apply", why)
                        break
                try:
                    results.append(await _run_one(app_id))
                    if results[-1]["status"] == "restarted":
                        break   # the restarted application runs next, on its own
                    if results[-1]["status"] == "timed_out":
                        continue
                    if results[-1]["status"] == "awaiting_review":
                        break   # hold: the filled form stays open until the user decides
                    if results[-1]["status"] == "deferred":
                        why = usage.allowed("apply")[1] or "Claude usage limit reached"
                        stats["deferred"] = why
                        await usage.notify_paused("apply", why)
                        break   # the rest stay queued until the limit resets
                except Exception as exc:
                    log.exception("apply %s crashed", app_id)
                    with db.session() as s:
                        a = s.get(db.Application, app_id)
                        a.status, a.notes = "failed", f"crash: {exc}"
                    results.append({"application": app_id, "status": "failed"})
            stats["results"] = results
        return stats
