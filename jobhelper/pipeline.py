"""Discovery pipeline: gather → cheap filter → enrich survivors → store → prefilter → score."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, tuple_

from . import config, db, mail, prefilter, scoring
from .sources import ats, email_alerts, indeed, linkedin, naukri
from .sources.base import Posting, upsert

log = logging.getLogger(__name__)


def _known_keys(postings: list[Posting]) -> set[tuple[str, str]]:
    keys = {(p.source, p.external_id) for p in postings}
    if not keys:
        return set()
    with db.session() as s:
        rows = s.execute(select(db.Job.source, db.Job.external_id)
                         .where(tuple_(db.Job.source, db.Job.external_id).in_(list(keys)))).all()
    return {(r[0], r[1]) for r in rows}


async def gather(sources: list[str] | None = None) -> tuple[list[Posting], dict[str, Any]]:
    s = config.settings()
    srch = s["search"]
    want = set(sources or ["boards", "linkedin", "naukri", "indeed", "email"])
    postings: list[Posting] = []
    stats: dict[str, Any] = {}

    async def attempt(name: str, coro) -> None:
        try:
            got, st = await coro
            postings.extend(got)
            stats[name] = {"found": len(got), **({"detail": st} if st else {})}
        except mail.MailNotConfigured as exc:
            stats[name] = {"skipped": str(exc)}
        except Exception as exc:  # a broken source must not stop the others
            log.exception("source %s failed", name)
            stats[name] = {"error": f"{type(exc).__name__}: {exc}"}

    async def _li():
        return await linkedin.search_only(srch["keywords"], srch["locations"]["linkedin"],
                                          max_age_days=srch["max_age_days"],
                                          max_results=srch["max_results_per_query"]), {}

    if "email" in want:
        await attempt("email", email_alerts.discover(s["email"]["alert_senders"]))
    if "boards" in want:
        await attempt("boards", ats.discover(s.get("ats_boards") or {}))
    if "linkedin" in want:
        await attempt("linkedin", _li())
    if "naukri" in want:
        await attempt("naukri", naukri.search(srch["keywords"], srch["locations"]["naukri"],
                                              years=s["candidate"]["years_experience"],
                                              max_age_days=srch["max_age_days"]))
    if "indeed" in want and srch["locations"].get("indeed"):   # empty list = site search off
        await attempt("indeed", indeed.search(srch["keywords"], srch["locations"]["indeed"],
                                              max_age_days=srch["max_age_days"]))
    return postings, stats


async def enrich(postings: list[Posting]) -> dict[str, int]:
    by_source: dict[str, list[Posting]] = {}
    for p in postings:
        if not p.description or len(p.description) < 400 or not p.title:
            by_source.setdefault(p.source, []).append(p)
    done = {}
    for source, items in by_source.items():
        try:
            if source == "linkedin":
                await linkedin.fill_details(items)
            elif source == "naukri":
                await naukri.enrich(items)
            elif source == "indeed":
                await indeed.enrich(items)
            done[source] = len(items)
        except Exception as exc:
            log.exception("enrich %s failed", source)
            done[source] = f"error: {exc}"  # type: ignore[assignment]
    return done


def prefilter_new() -> dict[str, int]:
    s_ = config.settings()
    out = {"prefiltered": 0, "filtered_out": 0}
    with db.session() as s:
        for job in s.scalars(select(db.Job).where(db.Job.status == "new")).all():
            v = prefilter.evaluate(job.title, job.location, job.remote, job.experience, job.description, s_)
            if v.ok and prefilter.too_old(job.posted_at, job.discovered_at, s_):
                v = prefilter.Verdict(False, f"posted over {s_['apply'].get('max_job_age_days', 7)} days ago")
            job.status = "prefiltered" if v.ok else "filtered_out"
            job.filter_reason = None if v.ok else v.reason
            out[job.status] += 1
    return out


async def discover(sources: list[str] | None = None, *, score: bool = True) -> dict[str, Any]:
    settings = config.settings()
    with db.run_log("discover") as stats:
        postings, stats["sources"] = await gather(sources)
        # Dedupe within this run, then drop what we've already stored.
        uniq: dict[tuple[str, str], Posting] = {}
        for p in postings:
            uniq.setdefault((p.source, p.external_id), p)
        known = _known_keys(list(uniq.values()))
        fresh = [p for k, p in uniq.items() if k not in known]
        # Only spend detail fetches on postings whose title/location could pass.
        survivors = [p for p in fresh if not p.title or (
            prefilter.check_title(p.title, settings).ok and prefilter.check_location(p.location, p.remote, settings).ok)]
        stats["fresh"], stats["survivors"] = len(fresh), len(survivors)
        stats["enriched"] = await enrich(survivors)
        # Priority flags on known jobs still need updating, so upsert everything we saw.
        stats["stored"] = upsert(list(uniq.values()))
        stats["prefilter"] = prefilter_new()
        if score:
            stats["scoring"] = await scoring.score_pending()
    return stats
