"""Response tracking from Gmail: classify job mail, update applications, alert, trigger prep.

Reads only allowlisted job mail through `mail.search_job_mail` (see mail.py for the rules)
and never replies, accepts, or changes anything in the mailbox.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from . import config, db, kb, llm, mail, notify, prep, skills, usage
from .sources import email_alerts

log = logging.getLogger(__name__)

# Order matters: a later status never regresses to an earlier one.
RANK = {"applied": 1, "viewed": 2, "shortlisted": 3, "assessment": 4, "interview": 5, "offer": 6, "rejected": 7}
KIND_TO_STATUS = {"viewed": "viewed", "shortlisted": "shortlisted", "assessment": "assessment",
                  "interview": "interview", "offer": "offer", "rejected": "rejected"}
ALERT_KINDS = {"shortlisted", "assessment", "interview", "recruiter_questions", "offer"}
PREP_KINDS = {"shortlisted", "assessment", "interview"}

SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "message_index": {"type": "integer"},
            "kind": {"type": "string", "enum": ["application_received", "viewed", "shortlisted", "assessment",
                                                "interview", "recruiter_questions", "rejected", "offer",
                                                "recruiter_outreach", "job_alert", "other", "not_job_related"]},
            "company": {"type": "string"},
            "role": {"type": "string"},
            "application_match": {"type": ["integer", "null"]},
            "confidence": {"type": "number"},
            "summary": {"type": "string"},
            "action_required": {"type": "boolean"},
            "urgency": {"type": "string", "enum": ["high", "normal"]},
            "event": {"type": "object", "properties": {
                "when": {"type": "string"}, "deadline": {"type": "string"}, "format": {"type": "string"},
                "link": {"type": "string"}, "duration": {"type": "string"}}},
            "questions": {"type": "array", "items": {"type": "string"}},
            "suggested_reply": {"type": "string"},
        },
        "required": ["message_index", "kind", "company", "role", "application_match", "confidence",
                     "summary", "action_required", "urgency", "questions", "suggested_reply"],
    }}},
    "required": ["results"],
}


def _open_applications(s) -> list[dict[str, Any]]:
    rows = s.execute(select(db.Application, db.Job).join(db.Job)
                     .where(db.Application.status.in_(list(RANK) + ["dry_run_ok"]))).all()
    return [{"application_id": a.id, "company": j.company, "role": j.title, "portal": a.portal,
             "status": a.status, "applied_at": a.applied_at.date().isoformat() if a.applied_at else None}
            for a, j in rows]


def _system() -> str:
    return f"""{skills.body("recruiter-email-triage")}

## This run
You are the JobHelper tracker. Classify each <message> below. Match it to one of the
<applications> by company and role when you can (application_match = its application_id,
else null). Use "not_job_related" for anything that isn't about the candidate's job search;
it will be discarded unread. For suggested_reply, use only these candidate facts:

<candidate_brief>
{kb.candidate_brief(max_chars=8000)}
</candidate_brief>"""


async def poll(days: int = 3, senders: list[str] | None = None) -> dict[str, Any]:
    stats: dict[str, Any] = {"fetched": 0, "new": 0, "classified": 0, "alerts": 0, "prep": 0}
    preps: list[tuple[int, str]] = []
    with db.run_log("email") as run_stats:
        mails = await (mail.search(senders, f"newer_than:{days}d", limit=80) if senders
                       else mail.search_job_mail(f"newer_than:{days}d", limit=80))
        stats["fetched"] = len(mails)
        with db.session() as s:
            # Job-alert mail belongs to job search (pipeline.discover), which marks it seen itself.
            fresh = [m for m in mails if not s.get(db.EmailSeen, m.message_id) and not is_job_alert(m)]
            apps = _open_applications(s)
        stats["new"] = len(fresh)
        for i in range(0, len(fresh), 5):
            if not await _classify_batch(fresh[i:i + 5], apps, stats, preps):
                break   # paused by the usage governor: unread-by-us mail is retried next poll
        # Briefs take minutes; write them only after every alert of this poll has gone out.
        for app_pk, trigger in preps:
            if await prep.ensure_brief(app_pk, trigger=trigger):
                stats["prep"] += 1
        run_stats.update(stats)
    return stats


SENT = ("applied", "already_applied", "viewed", "shortlisted", "assessment", "interview", "offer", "rejected")


async def learn_senders(days: int = 60) -> dict[str, Any]:
    """Learn the email domain of companies you applied to (headers only, see mail.py rule 4), then
    read those domains' recent job mail so confirmations and replies sent from them are tracked."""
    since, recheck = db.utcnow() - timedelta(days=days), db.utcnow() - timedelta(days=7)
    with db.session() as s:
        companies = sorted({c for (c,) in s.execute(select(db.Job.company).join(db.Application).where(
            db.Application.status.in_(SENT), db.Application.applied_at >= since)).all() if c})
        done = {r.company: r for r in s.scalars(select(db.CompanySender))}
    todo = [c for c in companies if c not in done or (done[c].domain is None and done[c].checked_at < recheck)]
    found = await mail.learn_company_domains(todo, days) if todo else {}
    with db.session() as s:
        for c in todo:
            row = s.get(db.CompanySender, c) or db.CompanySender(company=c)
            row.domain, row.checked_at = found.get(c), db.utcnow()
            s.add(row)
    # Re-read all learned senders' recent mail: they send little, mail already handled costs nothing,
    # and anything a usage pause cut short gets picked up the next day.
    with db.session() as s:
        learned = sorted({d for (d,) in s.execute(select(db.CompanySender.domain).where(
            db.CompanySender.domain.is_not(None))).all()})
    backfill = await poll(days=days, senders=learned) if learned else {}
    return {"checked": len(todo), "learned": found, "backfill": backfill}


def is_job_alert(m: mail.Mail) -> bool:
    """Same rule job search uses: job links in it and not a status-update subject."""
    return bool(email_alerts.extract(m)) and not email_alerts.STATUS_SUBJECT.search(m.subject or "")


async def _classify_batch(batch: list[mail.Mail], apps: list[dict[str, Any]], stats: dict[str, Any],
                          preps: list[tuple[int, str]]) -> bool:
    """Classify and act on a batch. Returns False if the usage governor paused email triage."""
    msgs = "\n\n".join(
        f"<message index=\"{i}\">\nFrom: {m.sender_name} <{m.sender}>\nDate: {m.date}\nSubject: {m.subject}\n\n"
        f"{(m.text or '')[:6000]}\n</message>" for i, m in enumerate(batch))
    prompt = f"<applications>\n{llm.dumps(apps)}\n</applications>\n\n{msgs}"
    try:
        out = await llm.structured(prompt, SCHEMA, system=_system(),
                                   model=config.settings()["llm"]["email_model"], effort="low", task="email")
    except llm.LLMDeferred as exc:
        stats["paused"] = str(exc)
        await usage.notify_paused("email", str(exc))
        return False
    except llm.LLMError as exc:
        log.error("email classification failed: %s", exc)
        return True
    results = {r["message_index"]: r for r in out.get("results", [])}
    for i, m in enumerate(batch):
        r = results.get(i)
        if r is None:
            continue
        stats["classified"] += 1
        await _apply_result(m, r, stats, preps)
    return True


async def _apply_result(m: mail.Mail, r: dict[str, Any], stats: dict[str, Any],
                        preps: list[tuple[int, str]]) -> None:
    kind = r["kind"]
    app_id = r.get("application_match")
    with db.session() as s:
        s.add(db.EmailSeen(message_id=m.message_id, kind="tracking" if kind != "not_job_related" else "ignored",
                           subject=m.subject[:500] if kind != "not_job_related" else None,
                           sender=m.sender, result={k: r[k] for k in ("kind", "company", "role", "confidence")}))
        if kind in ("not_job_related", "job_alert"):
            return
        app_ = s.get(db.Application, app_id) if app_id else None
        if app_ and kind in KIND_TO_STATUS:
            new = KIND_TO_STATUS[kind]
            if RANK.get(new, 0) > RANK.get(app_.status, 0) or app_.status == "dry_run_ok":
                app_.status, app_.last_update_at = new, db.utcnow()
                app_.job.status = new
        db.add_event(s, f"mail_{kind}", r["summary"], application=app_, detail={
            "from": m.sender, "subject": m.subject, "date": m.date.isoformat() if m.date else None,
            "event": r.get("event") or {}, "questions": r.get("questions") or [],
            "suggested_reply": r.get("suggested_reply") or "", "urgency": r.get("urgency"),
            "company": r.get("company"), "role": r.get("role")})
        app_pk = app_.id if app_ else None

    if kind in ALERT_KINDS:
        stats["alerts"] += 1
        ev = r.get("event") or {}
        lines = [f"{_emoji(kind)} {kind.replace('_', ' ').title()}: {r.get('company')} — {r.get('role')}",
                 r["summary"]]
        for label, key in (("When", "when"), ("Deadline", "deadline"), ("Format", "format"),
                           ("Duration", "duration"), ("Link", "link")):
            if ev.get(key):
                lines.append(f"{label}: {ev[key]}")
        if r.get("questions"):
            lines.append("They asked:\n" + "\n".join(f"• {q}" for q in r["questions"]))
        lines.append(f"From: {m.sender} · \"{m.subject}\"")
        if app_pk:
            lines.append(notify.link(f"/applications/{app_pk}"))
        lines.append("I haven't replied or accepted anything.")
        await notify.send("\n".join(lines), kind="response")
    if kind in PREP_KINDS and app_pk:
        preps.append((app_pk, f"{kind}: {r['summary']}"))


def _emoji(kind: str) -> str:
    return {"interview": "📅", "assessment": "📝", "shortlisted": "⭐", "recruiter_questions": "❓",
            "offer": "🎉"}.get(kind, "📬")
