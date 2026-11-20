"""Company ratings from AmbitionBox employee reviews, rated 1 / 3 / 5 on the user's four criteria.

Culture = company culture · Benefits = salary & benefits · Type of work = average of work
satisfaction and skill development · Work life = work-life balance. The overall 1/3/5 comes from
the mean of those four. Jobs at companies rated 1 are filtered out before scoring (no Claude use).
Companies without enough reviews stay unrated and are never filtered.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select

from . import config, db

log = logging.getLogger(__name__)
BASE = "https://www.ambitionbox.com"
DELAY = 1.5   # seconds between lookups, to be gentle with the site
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/124.0 Safari/537.36", "Accept-Language": "en-IN,en;q=0.9"}
CRITERIA = {"culture": ("companyCultureRating",), "benefits": ("compensationBenefitsRating",),
            "work": ("workSatisfactionRating", "skillDevelopmentRating"), "work_life": ("workLifeRating",)}
LABELS = {"culture": "culture", "benefits": "benefits", "work": "type of work", "work_life": "work-life"}
SUFFIX = re.compile(r"\b(private|pvt|limited|ltd|llp|inc|incorporated|corp|corporation|co|company|india|"
                    r"the|solutions?|services|technolog(y|ies)|software)\b")


def _settings() -> dict[str, Any]:
    return {"enabled": True, "bands": [3.3, 4.0], "min_reviews": 20, "refresh_days": 30,
            **(config.settings().get("company_ratings") or {})}


def norm(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())).strip()


def _core(name: str) -> set[str]:
    return set(SUFFIX.sub(" ", norm(name)).split())


def _flat(name: str) -> str:
    """Name without suffixes or spaces: "JPMorganChase" == "JPMorgan Chase & Co."."""
    return SUFFIX.sub(" ", norm(name)).replace(" ", "")


def band(value: float | None, bands: list[float]) -> int | None:
    if value is None:
        return None
    return 5 if value >= bands[1] else 3 if value >= bands[0] else 1


def grade(ratings: dict[str, float], bands: list[float]) -> tuple[int | None, dict[str, Any]]:
    """AmbitionBox `ratingsData` → (overall 1/3/5, per-criterion detail)."""
    vals = {}
    for c, keys in CRITERIA.items():
        got = [float(ratings[k]) for k in keys if ratings.get(k) is not None]
        vals[c] = round(sum(got) / len(got), 2) if got else None
    known = [v for v in vals.values() if v is not None]
    overall = band(sum(known) / len(known), bands) if len(known) >= 3 else None
    return overall, {"scores": {c: band(v, bands) for c, v in vals.items()}, "values": vals,
                     "ambitionbox_overall": ratings.get("overallCompanyRating")}


def pick(query: str, candidates: list[dict[str, Any]], min_reviews: int) -> dict[str, Any] | None:
    """The AmbitionBox company that is clearly the same as the posting's company, else None."""
    want = _core(query)
    if not want:
        return None
    for i, c in enumerate(candidates):
        if c.get("type") != "company" or (c.get("reviewCount") or 0) < min_reviews:
            continue
        have = _core(c.get("name", ""))
        if not have:
            continue
        if _flat(c.get("name", "")) == _flat(query) or len(want & have) / len(want | have) >= 0.6:
            return c
        # "A.P. Moller - Maersk" vs "Maersk", "Relanto" vs "Relanto Global": trust only the top hit.
        if i == 0 and (have <= want or want <= have) and any(len(t) >= 4 for t in want & have):
            return c
    return None



def parse_ratings(html: str) -> dict[str, float] | None:
    m = re.search(r'"ratingsData":(\{[^{}]*\})', html)
    return json.loads(m.group(1)) if m else None


async def fetch(client: httpx.AsyncClient, company: str) -> tuple[int | None, dict[str, Any]]:
    st = _settings()
    r = await client.get(f"{BASE}/api/v2/search", params={"query": company, "category": "all"})
    hit = pick(company, (r.json() or {}).get("data") or [], st["min_reviews"])
    if hit is None:
        return None, {"note": "no AmbitionBox company with enough reviews matches this name"}
    url = f"{BASE}/reviews/{hit['url']}-reviews"
    page = await client.get(url)
    ratings = parse_ratings(page.text)
    if not ratings:
        return None, {"note": "AmbitionBox page had no ratings", "url": url}
    overall, detail = grade(ratings, st["bands"])
    return overall, {**detail, "url": url, "matched": hit.get("name"), "reviews": hit.get("reviewCount")}


def describe(rating: db.CompanyRating | None) -> str | None:
    if rating is None or rating.overall is None:
        return None
    sc = (rating.detail or {}).get("scores") or {}
    parts = ", ".join(f"{LABELS[c]} {sc[c]}" for c in CRITERIA if sc.get(c))
    return f"{rating.overall}/5 ({parts})"


async def ensure(companies: set[str], limit: int = 60) -> int:
    """Look up companies not rated yet (or stale). Returns how many were fetched."""
    st = _settings()
    now = db.utcnow()
    with db.session() as s:
        todo = []
        for name in sorted(companies):
            key = norm(name)
            row = s.get(db.CompanyRating, key) if key else None
            stale = row is not None and row.fetched_at < now - timedelta(
                days=1 if (row.detail or {}).get("error") else st["refresh_days"])
            if key and (row is None or stale):
                todo.append((key, name))
    done = 0
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True) as client:
        for key, name in todo[:limit]:
            try:
                overall, detail = await fetch(client, name)
            except Exception as exc:    # site down or changed: unrated for now, retried tomorrow
                overall, detail = None, {"error": f"{type(exc).__name__}: {exc}"[:200]}
            with db.session() as s:
                row = s.get(db.CompanyRating, key) or db.CompanyRating(name=key)
                row.overall, row.detail, row.fetched_at = overall, detail, db.utcnow()
                s.add(row)
            done += 1
            await asyncio.sleep(DELAY)
    return done


async def screen_prefiltered() -> dict[str, int]:
    """Rate the companies of jobs waiting for scoring; filter out jobs at companies rated 1."""
    if not _settings()["enabled"]:
        return {}
    with db.session() as s:
        names = {c for (c,) in s.execute(select(db.Job.company).where(db.Job.status == "prefiltered")).all() if c}
    fetched = await ensure(names)
    dropped = 0
    with db.session() as s:
        for job in s.scalars(select(db.Job).where(db.Job.status == "prefiltered")).all():
            row = s.get(db.CompanyRating, norm(job.company))
            if row is not None and row.overall == 1:
                job.status, job.filter_reason = "filtered_out", f"company rated {describe(row)} on AmbitionBox"[:300]
                dropped += 1
    return {"looked_up": fetched, "filtered_out": dropped}
