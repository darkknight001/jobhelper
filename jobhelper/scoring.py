"""LLM fit scoring (batched, structured output) and routing to auto-apply / review / skip."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select

from . import companies, config, db, kb, llm, skills, usage

log = logging.getLogger(__name__)

PORTALS = ("naukri", "linkedin", "indeed")


def portal_of(source: str) -> str:
    return source if source in PORTALS else "company"


def _schema(resume_files: list[str]) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": "object",
        "properties": {
            "job_id": {"type": "integer"},
            "score": {"type": "integer", "minimum": 0, "maximum": 100},
            "verdict": {"type": "string", "enum": ["strong", "good", "stretch", "weak", "reject"]},
            "summary": {"type": "string", "description": "One sentence a busy person reads on a phone."},
            "reasons": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
            "gaps": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
            "red_flags": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
            "seniority_fit": {"type": "string", "enum": ["under", "fit", "over", "unclear"]},
            "location_ok": {"type": "boolean"},
        },
        "required": ["job_id", "score", "verdict", "summary", "reasons", "gaps", "red_flags",
                     "seniority_fit", "location_ok"],
    }
    if len(resume_files) > 1:
        item["properties"]["resume_variant"] = {"type": "string", "enum": resume_files}
        item["required"].append("resume_variant")
    return {"type": "object", "properties": {"results": {"type": "array", "items": item}},
            "required": ["results"]}


def system_prompt() -> str:
    """The job-fit-scoring skill plus this harness's thresholds and the candidate brief."""
    s = config.settings()
    p = config.profile()
    prefs = p.get("preferences") or {}
    return f"""{skills.body("job-fit-scoring")}

## This run
You are running inside the JobHelper harness: the candidate facts are in <candidate_brief>
below (do not ask for files), and you must return the JSON shape from section 5.
- Candidate experience: {s['candidate']['years_experience']} years.
- Preferred locations: {', '.join(prefs.get('preferred_locations') or [])}; work modes: {', '.join(prefs.get('work_mode') or [])}.
- Target roles: Tech Lead / Lead, Senior Backend (Python), Staff/Principal, AI/GenAI platform backend.
- Thresholds: auto-apply >= {s['scoring']['auto_apply_min']}, review >= {s['scoring']['review_min']}.

<candidate_brief>
{kb.candidate_brief()}
</candidate_brief>"""


def _job_block(j: db.Job) -> str:
    desc = (j.description or "")[:5000]
    return (f"<job id=\"{j.id}\">\nTitle: {j.title}\nCompany: {j.company}\nLocation: {j.location or 'unknown'}"
            f"{' (remote)' if j.remote else ''}\nExperience: {j.experience or 'unknown'}\nSalary: {j.salary or 'unknown'}\n"
            f"Skills: {', '.join(j.skills or [])}\nSource: {j.source}\n\n{desc}\n</job>")


def applied_or_queued_today(s, portal: str) -> int:
    tzinfo = config.tz()
    start = datetime.combine(datetime.now(tzinfo).date(), time.min, tzinfo)
    q = select(func.count(db.Application.id)).where(
        db.Application.portal == portal,
        ((db.Application.applied_at >= start) |
         ((db.Application.status.in_(["queued", "applying", "awaiting_review"])) & (db.Application.created_at >= start - timedelta(hours=12)))),
    )
    return s.scalar(q) or 0


def route(s, job: db.Job) -> str:
    sc = config.settings()
    score = job.score or 0
    detail = job.score_detail or {}
    # Companies rated 5 on AmbitionBox (companies.py) auto-apply from a lower score; rated 3 and
    # unrated follow auto_apply_min. Rated-1 companies never reach scoring.
    rated = s.get(db.CompanyRating, companies.norm(job.company))
    auto_min = sc["scoring"]["auto_apply_min"]
    if rated is not None and rated.overall == 5:
        auto_min = min(auto_min, (sc.get("company_ratings") or {}).get("auto_apply_min_rated_5", 80))
    if score >= auto_min and detail.get("location_ok", True) and not detail.get("red_flags"):
        portal = portal_of(job.source)
        if applied_or_queued_today(s, portal) < sc["apply"]["daily_cap_per_portal"]:
            s.add(db.Application(job_id=job.id, portal=portal, mode="auto", status="queued"))
            return "queued_auto"
        job.review_note = f"Auto-apply candidate, but today's {portal} cap is reached."
        return "review"
    if score >= sc["scoring"]["review_min"]:
        return "review"
    return "skipped"


async def score_pending(limit: int = 200) -> dict[str, Any]:
    try:   # jobs at companies rated 1 never reach Claude
        screened = await companies.screen_prefiltered()
    except Exception:
        log.exception("company screening failed; scoring without it")
        screened = {}
    sc = config.settings()
    model = sc["llm"]["scoring_model"]
    batch_size = int(sc["scoring"].get("batch_size", 8))
    main = sc["apply"].get("resume")
    resume_files = [main] if main else sorted(p.name for p in config.RESUME_DIR.glob("*.pdf"))
    schema = _schema(resume_files)
    system = system_prompt()

    with db.session() as s:
        jobs = s.scalars(select(db.Job).where(db.Job.status == "prefiltered")
                         .order_by(db.Job.priority.desc(), db.Job.discovered_at.desc()).limit(limit)).all()
        ids = [j.id for j in jobs]
        blocks = {j.id: _job_block(j) for j in jobs}

    stats = {"scored": 0, "queued_auto": 0, "review": 0, "skipped": 0, "errors": 0, "deferred": 0}
    if screened:
        stats["companies"] = screened
    batches = [ids[i:i + batch_size] for i in range(0, len(ids), batch_size)]
    paused: list[str] = []

    async def run_batch(batch: list[int]) -> None:
        if paused:                      # the governor said stop; leave the rest for later
            stats["deferred"] += len(batch)
            return
        prompt = ("Score each job below for this candidate. Return one result per job id.\n\n"
                  + "\n\n".join(blocks[i] for i in batch))
        try:
            out = await llm.structured(prompt, schema, system=system, model=model, effort="medium",
                                       task="scoring", ref="batch")
        except llm.LLMDeferred as exc:
            paused.append(str(exc))
            stats["deferred"] += len(batch)   # still 'prefiltered'; scored on a later run
            return
        except llm.LLMError as exc:
            log.error("scoring batch failed: %s", exc)
            stats["errors"] += len(batch)
            return
        by_id = {r["job_id"]: r for r in out.get("results", []) if r.get("job_id") in batch}
        with db.session() as s:
            for jid in batch:
                r = by_id.get(jid)
                job = s.get(db.Job, jid)
                if not r or job is None:
                    stats["errors"] += 1
                    continue
                job.score = float(r["score"])
                job.score_detail = r
                job.resume_variant = r.get("resume_variant") or (resume_files[0] if resume_files else None)
                job.scored_at = db.utcnow()
                job.status = route(s, job)
                s.flush()
                stats["scored"] += 1
                stats[job.status] += 1

    await asyncio.gather(*(run_batch(b) for b in batches))
    if paused:
        stats["paused"] = paused[0]
        await usage.notify_paused("scoring", paused[0])
    return stats
