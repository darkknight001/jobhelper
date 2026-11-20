"""Interview prep briefs for shortlisted / interview / assessment applications."""
from __future__ import annotations

import logging
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from . import config, db, kb, llm, notify, skills, usage

log = logging.getLogger(__name__)
PREP_DIR = config.DATA_DIR / "prep"


def path_for(application_id: int) -> Path:
    return PREP_DIR / f"app-{application_id}.md"


async def ensure_brief(application_id: int, *, trigger: str, force: bool = False) -> bool:
    """Write the brief once per application (or again with force). Returns True if written."""
    out = path_for(application_id)
    if out.exists() and not force:
        return False
    ok, why = usage.allowed("prep")
    if not ok:   # retried by retry_pending() (hourly and at the 02:00 catch-up)
        await usage.notify_paused("prep", why)
        return False
    with db.session() as s:
        a = s.get(db.Application, application_id)
        if a is None:
            return False
        j = a.job
        job_block = (f"Title: {j.title}\nCompany: {j.company}\nLocation: {j.location}\nURL: {j.url}\n"
                     f"Fit notes: {(j.score_detail or {}).get('summary')}\n\n{(j.description or '')[:10000]}")
        timeline = "\n".join(f"- {e.at:%Y-%m-%d} {e.kind}: {e.summary}" for e in a.events[-10:])
        company, title = j.company, j.title

    system = f"""{skills.body("interview-prep-brief")}

## This run
You are JobHelper writing the brief for the candidate. Candidate facts are below; you may use
WebSearch for public company research only. Return the brief as Markdown and nothing else.

<candidate_brief>
{kb.candidate_brief(max_chars=45000, include_private=True)}
</candidate_brief>
This brief is for the candidate only, so you may use the private notes (past interview
feedback, gaps, negotiation notes) to prepare them. Still follow the claims policy for any
line they might say to the interviewer."""
    prompt = (f"Write the prep brief.\n\nTrigger: {trigger}\n\n<job>\n{job_block}\n</job>\n\n"
              f"<timeline>\n{timeline}\n</timeline>")
    options = ClaudeAgentOptions(
        model=config.settings()["llm"]["writing_model"], system_prompt=system,
        # No WebFetch: this run holds the private notes, and fetching a URL a job post or web
        # page suggests could send them out.
        tools=["WebSearch"], allowed_tools=["WebSearch"],
        permission_mode="dontAsk", setting_sources=[], max_turns=24,
        cwd=str(config.DATA_DIR / "claude-cwd"), env=llm.base_env(), effort="high",
    )
    (config.DATA_DIR / "claude-cwd").mkdir(parents=True, exist_ok=True)
    result = None
    try:
        async for msg in query(prompt=prompt, options=options):
            usage.observe(msg)
            if isinstance(msg, ResultMessage):
                result = msg
    except Exception:
        log.exception("prep brief failed")
        usage.record(result, task="prep", ref=f"app-{application_id}")
        if result is None:
            return False
    else:
        usage.record(result, task="prep", ref=f"app-{application_id}")
    if result is None or result.is_error or not result.result:
        log.error("prep brief: no result (%s)", getattr(result, "subtype", None))
        return False
    PREP_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(result.result, encoding="utf-8")
    with db.session() as s:
        a = s.get(db.Application, application_id)
        db.add_event(s, "prep_ready", f"Prep brief ready ({trigger[:80]})", application=a)
    first_lines = "\n".join(l for l in result.result.splitlines() if l.strip())[:900]
    await notify.send(f"📚 Prep brief: {company} — {title}\n{notify.link(f'/prep/{application_id}')}\n\n{first_lines}",
                      kind="prep")
    return True


async def retry_pending() -> dict[str, int]:
    """Write briefs that were deferred (usage limits) or failed: shortlisted/interview/assessment
    applications that still have none."""
    from sqlalchemy import select
    with db.session() as s:
        ids = [a.id for a in s.scalars(select(db.Application).where(
            db.Application.status.in_(["shortlisted", "assessment", "interview"])))]
    written = 0
    for app_id in ids:
        if not path_for(app_id).exists():
            written += int(await ensure_brief(app_id, trigger="catch-up for an earlier shortlist/interview"))
    return {"pending": len(ids), "written": written}
