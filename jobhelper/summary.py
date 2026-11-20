"""Daily summary and stats, as plain text (Telegram) and as data (dashboard / API)."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import func, or_, select

from . import config, db, notify, pause, usage


def _day_bounds(day: datetime | None = None) -> tuple[datetime, datetime]:
    tzinfo = config.tz()
    d = (day or datetime.now(tzinfo)).date()
    start = datetime.combine(d, time.min, tzinfo)
    return start, start + timedelta(days=1)


def stats(days: int = 14) -> dict[str, Any]:
    tzinfo = config.tz()
    start, end = _day_bounds()
    with db.session() as s:
        applied_today = Counter(dict(s.execute(
            select(db.Application.portal, func.count()).where(db.Application.applied_at >= start,
                                                              db.Application.applied_at < end)
            .group_by(db.Application.portal)).all()))
        by_status = dict(s.execute(select(db.Application.status, func.count()).group_by(db.Application.status)).all())
        job_status = dict(s.execute(select(db.Job.status, func.count()).group_by(db.Job.status)).all())
        found_today = s.scalar(select(func.count(db.Job.id)).where(db.Job.discovered_at >= start)) or 0
        series = []
        for i in range(days - 1, -1, -1):
            d0 = start - timedelta(days=i)
            d1 = d0 + timedelta(days=1)
            series.append({
                "day": d0.strftime("%d %b"),
                "applied": s.scalar(select(func.count(db.Application.id)).where(
                    db.Application.applied_at >= d0, db.Application.applied_at < d1)) or 0,
                "found": s.scalar(select(func.count(db.Job.id)).where(
                    db.Job.discovered_at >= d0, db.Job.discovered_at < d1)) or 0,
            })
        responses_today = s.execute(select(db.Event).where(
            or_(db.Event.kind.like("mail_%"), db.Event.kind.like("portal_%")), db.Event.at >= start, db.Event.at < end,
            db.Event.kind.not_in(["mail_application_received", "mail_job_alert", "mail_other"]))
            .order_by(db.Event.at)).scalars().all()
        responses = [{"kind": e.kind.split("_", 1)[1], "summary": e.summary, "application_id": e.application_id,
                      "company": (e.detail or {}).get("company")} for e in responses_today]
        review = s.scalars(select(db.Job).where(db.Job.status == "review")
                           .order_by(db.Job.score.desc()).limit(10)).all()
        review_rows = [{"id": j.id, "title": j.title, "company": j.company, "score": int(j.score or 0),
                        "source": j.source, "summary": (j.score_detail or {}).get("summary", "")} for j in review]
        review_total = job_status.get("review", 0)
        needs_user = s.scalar(select(func.count(db.Application.id)).where(db.Application.status == "needs_user")) or 0
        waiting = s.scalars(select(db.Application).where(db.Application.status == "awaiting_review")).first()
        apply_yourself = [{"id": a.id, "title": a.job.title, "company": a.job.company,
                           "url": a.final_url or a.job.apply_url or a.job.url}
                          for a in s.scalars(select(db.Application).where(db.Application.status == "apply_yourself")
                                             .order_by(db.Application.last_update_at.desc()))]
        awaiting_review = ({"id": waiting.id, "title": waiting.job.title, "company": waiting.job.company}
                           if waiting else None)
        silent = [{"id": a.id, "title": a.job.title, "company": a.job.company}
                  for a in s.scalars(select(db.Application).where(
                      db.Application.status == "applied", db.Application.final_url.is_not(None),
                      db.Application.applied_at < db.utcnow() - timedelta(days=3)))
                  if not _on_portal(a.final_url) and not any(e.kind.startswith("mail_") for e in a.events)]
        funnel = {k: by_status.get(k, 0) for k in ("applied", "viewed", "shortlisted", "assessment", "interview",
                                                   "offer", "rejected")}
    total_applied = sum(funnel.values())
    return {"date": datetime.now(tzinfo).strftime("%a %d %b %Y"), "applied_today": dict(applied_today),
            "applied_today_total": sum(applied_today.values()), "found_today": found_today,
            "responses_today": responses, "review": review_rows, "review_total": review_total,
            "needs_user": needs_user, "awaiting_review": awaiting_review, "apply_yourself": apply_yourself,
            "silent": silent,
            "funnel": funnel,
            "total_applied": total_applied,
            "job_status": job_status, "series": series,
            "dry_run": bool(config.settings()["apply"].get("dry_run", True)),
            "dry_run_ok": by_status.get("dry_run_ok", 0)}


def _on_portal(url: str | None) -> bool:
    return any(p in (url or "") for p in ("naukri.com", "linkedin.com", "indeed.com"))


def daily_text() -> str:
    st = stats()
    lines = [f"🗂 JobHelper · {st['date']}"]
    if (p := pause.current()):
        lines.append(f"⏸ Paused {p['label']}: no job search or new applications. /resume to start again.")
    if st["dry_run"]:
        lines.append(f"(dry-run mode: {st['dry_run_ok']} applications filled but not submitted)")
    per = ", ".join(f"{p} {n}" for p, n in sorted(st["applied_today"].items())) or "none"
    lines.append(f"Applied today: {st['applied_today_total']} ({per})")
    lines.append(f"New jobs found: {st['found_today']}")
    if st["responses_today"]:
        lines.append("\nResponses today:")
        lines += [f"• {r['kind'].replace('_', ' ')}: {r['summary']}" for r in st["responses_today"][:10]]
    if st["apply_yourself"]:
        lines.append("\n🖐 Apply these yourself (sites that block automated browsers):")
        lines += [f"#{x['id']} {x['title']} · {x['company']}: {x['url']}" for x in st["apply_yourself"][:8]]
    if st["awaiting_review"]:
        w = st["awaiting_review"]
        review_link = notify.link(f"/applications/{w['id']}")
        lines.append(f"\n📝 Waiting for your review (queue paused): #{w['id']} {w['title']} · {w['company']} "
                     f"{review_link}")
    if st["silent"]:
        lines.append(f"\n📭 No confirmation email yet (applied on a company site 3+ days ago): {len(st['silent'])}. "
                     f"Worth checking those sites or your inbox:")
        lines += [f"#{x['id']} {x['title']} · {x['company']}" for x in st["silent"][:5]]
    if st["needs_user"]:
        lines.append(f"\n⏸ {st['needs_user']} application(s) waiting on you: {notify.link('/applications?status=needs_user')}")
    if st["review"]:
        lines.append(f"\nFor review ({st['review_total']}): send /review for tap buttons, or open {notify.link('/review')}:")
        lines += [f"#{r['id']} [{r['score']}] {r['title']} · {r['company']} ({r['source']})" for r in st["review"]]
    f = st["funnel"]
    lines.append(f"\nPipeline: {st['total_applied']} applied · {f['viewed']} viewed · {f['shortlisted']} shortlisted · "
                 f"{f['assessment']} assessments · {f['interview']} interviews · {f['offer']} offers · {f['rejected']} rejected")
    u = usage.today()
    if u["by_task"]:
        parts = ", ".join(f"{t} ≈${v['cost']}" for t, v in sorted(u["by_task"].items(), key=lambda kv: -kv[1]["cost"]))
        windows = ", ".join(f"{r['type'].replace('_', ' ')} {int((r['utilization'] or 0) * 100)}%"
                            for r in u["readings"] if r["utilization"] is not None)
        lines.append(f"\nClaude today: ≈${u['total_cost']} at API prices ({parts})"
                     + (f"; limits: {windows}" if windows else "")
                     + (" · paused: " + "; ".join(u["paused"].values()) if u["paused"] else ""))
    lines.append(notify.link("/"))
    return "\n".join(lines)


async def send_daily() -> bool:
    with db.run_log("summary"):
        return await notify.send(daily_text(), kind="summary")


# ------------------------------------------------------------------ any period (weekly etc.)

def applications_in_period(days: int, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Applications started or applied in the last `days` days, newest first, with current status."""
    since = _day_bounds()[0] - timedelta(days=max(days, 1) - 1)
    with db.session() as s:
        stmt = (select(db.Application, db.Job).join(db.Job)
                .where(func.coalesce(db.Application.applied_at, db.Application.created_at) >= since))
        if status:
            stmt = stmt.where(db.Application.status == status)
        rows = s.execute(stmt.order_by(func.coalesce(db.Application.applied_at, db.Application.created_at).desc())
                         .limit(limit)).all()
        return [{"id": a.id, "job_id": j.id, "title": j.title, "company": j.company, "portal": a.portal,
                 "mode": a.mode, "status": a.status, "applied_at": a.applied_at, "blocker": a.blocker,
                 "last_update": (a.events[-1].summary if a.events else None)} for a, j in rows]


def period_text(days: int = 7) -> str:
    """Report for the last `days` days (1 = today), for Telegram / Hermes."""
    if days <= 1:
        return daily_text()
    tzinfo = config.tz()
    end = _day_bounds()[1]
    since = end - timedelta(days=days)
    apps = applications_in_period(days)
    with db.session() as s:
        found = s.scalar(select(func.count(db.Job.id)).where(db.Job.discovered_at >= since)) or 0
        events = s.scalars(select(db.Event).where(
            db.Event.kind.like("mail_%"), db.Event.at >= since,
            db.Event.kind.not_in(["mail_application_received", "mail_job_alert", "mail_other"]))
            .order_by(db.Event.at)).all()
        responses = [(e.kind[5:], e.summary) for e in events]
        review_total = s.scalar(select(func.count(db.Job.id)).where(db.Job.status == "review")) or 0
    applied = [a for a in apps if a["applied_at"]]
    per_portal = Counter(a["portal"] for a in applied)
    by_status = Counter(a["status"] for a in apps)
    lines = [f"🗓 JobHelper · last {days} days (to {datetime.now(tzinfo):%a %d %b})"]
    if config.settings()["apply"].get("dry_run", True):
        lines.append(f"(dry-run mode: {by_status.get('dry_run_ok', 0)} filled, not submitted)")
    lines.append(f"Applied: {len(applied)} ({', '.join(f'{p} {n}' for p, n in sorted(per_portal.items())) or 'none'})"
                 f" · new jobs found: {found} · waiting for review: {review_total}")
    moved = {k: by_status.get(k, 0) for k in ("viewed", "shortlisted", "assessment", "interview", "offer", "rejected")}
    lines.append("Status now: " + " · ".join(f"{v} {k}" for k, v in moved.items()))
    if responses:
        lines.append("\nResponses:")
        lines += [f"• {k.replace('_', ' ')}: {summ}" for k, summ in responses[-12:]]
    if apps:
        lines.append("\nApplications:")
        for a in apps[:25]:
            when = a["applied_at"].astimezone(tzinfo).strftime("%d %b") if a["applied_at"] else "—"
            lines.append(f"#{a['id']} {when} · {a['company']} — {a['title'][:60]} · {a['status'].replace('_', ' ')}")
        if len(apps) > 25:
            lines.append(f"…and {len(apps) - 25} more: {notify.link('/applications')}")
    lines.append(notify.link("/"))
    return "\n".join(lines)


async def send_weekly() -> bool:
    with db.run_log("summary"):
        return await notify.send(period_text(7), kind="summary")
