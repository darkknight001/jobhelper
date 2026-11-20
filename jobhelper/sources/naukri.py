"""Naukri discovery in the logged-in browser.

Naukri's search API needs headers its own JavaScript computes, so rather than calling
it directly we open the normal search page and capture the JSON the page fetches.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from playwright.async_api import Page, Response

from ..browser import BROWSER, human_pause, is_challenge
from .base import Posting, html_to_text, looks_remote

log = logging.getLogger(__name__)
BASE = "https://www.naukri.com"


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def search_url(keyword: str, location: str, years: int, max_age_days: int, page: int = 1) -> str:
    kw = slug(keyword)
    if location == "remote":
        path = f"/{kw}-jobs" + (f"-{page}" if page > 1 else "")
        return f"{BASE}{path}?k={quote(keyword)}&experience={years}&jobAge={max_age_days}&wfhType=2"
    loc = slug(location)
    path = f"/{kw}-jobs-in-{loc}" + (f"-{page}" if page > 1 else "")
    return (f"{BASE}{path}?k={quote(keyword)}&l={quote(location.replace('-', ' '))}"
            f"&experience={years}&jobAge={max_age_days}")


def _placeholder(job: dict[str, Any], kind: str) -> str | None:
    for ph in job.get("placeholders") or []:
        if ph.get("type") == kind:
            return ph.get("label")
    return None


def parse_search_json(data: dict[str, Any]) -> list[Posting]:
    out = []
    for j in data.get("jobDetails") or []:
        jid = str(j.get("jobId") or "")
        if not jid:
            continue
        url = j.get("jdURL") or ""
        if url.startswith("/"):
            url = BASE + url
        loc = _placeholder(j, "location")
        created = j.get("createdDate")
        skills = [s.strip() for s in (j.get("tagsAndSkills") or "").split(",") if s.strip()]
        out.append(Posting(
            source="naukri", external_id=jid, url=url, title=j.get("title", ""),
            company=j.get("companyName", ""), location=loc, experience=_placeholder(j, "experience"),
            salary=_placeholder(j, "salary"), skills=skills,
            description=html_to_text(j.get("jobDescription")) or None,
            remote=looks_remote(loc, j.get("title")),
            posted_at=datetime.fromtimestamp(created / 1000, timezone.utc) if isinstance(created, (int, float)) else None,
        ))
    return out


async def _capture(page: Page, url: str, pattern: str, timeout: float = 25) -> dict[str, Any] | None:
    fut: asyncio.Future = asyncio.get_running_loop().create_future()

    async def on_response(resp: Response) -> None:
        if pattern in resp.url and not fut.done():
            try:
                fut.set_result(await resp.json())
            except Exception:
                pass

    page.on("response", on_response)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        return await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        page.remove_listener("response", on_response)


async def search(keywords: list[str], locations: list[str], *, years: int, max_age_days: int,
                 pages: int = 2) -> tuple[list[Posting], dict[str, Any]]:
    seen: dict[str, Posting] = {}
    stats: dict[str, Any] = {}
    async with BROWSER.lock:
        async with BROWSER.page() as page:
            for kw in keywords:
                for loc in locations:
                    got = 0
                    for n in range(1, pages + 1):
                        data = await _capture(page, search_url(kw, loc, years, max_age_days, n), "/jobapi/v3/search")
                        if data is None:
                            wall = await is_challenge(page)
                            if wall:
                                stats["blocked"] = wall
                                return list(seen.values()), stats
                            break
                        batch = parse_search_json(data)
                        for p in batch:
                            seen.setdefault(p.external_id, p)
                        got += len(batch)
                        await human_pause(2.5, 6)
                        if len(batch) < 15:
                            break
                    stats[f"{kw} @ {loc}"] = got
    return list(seen.values()), stats


def _find_redirect(obj: Any) -> str | None:
    """The detail payload's key for company-site applies has moved around; search for it."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and v.startswith("http") and re.search(r"redirect|companyApply|applyUrl", k, re.I):
                return v
            found = _find_redirect(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_redirect(v)
            if found:
                return found
    return None


async def enrich(postings: list[Posting]) -> None:
    """Full description and company-site redirect for postings that passed the prefilter."""
    if not postings:
        return
    async with BROWSER.lock:
        async with BROWSER.page() as page:
            for p in postings:
                data = await _capture(page, p.url, "/jobapi/v4/job/")
                if data:
                    jd = data.get("jobDetails") or data
                    p.description = html_to_text(jd.get("description")) or p.description
                    # Postings from alert emails arrive with only an ID.
                    p.title = p.title or jd.get("title") or ""
                    p.company = p.company or (jd.get("companyDetail") or {}).get("name") or jd.get("companyName") or ""
                    if not p.location:
                        locs = [x.get("label") for x in jd.get("locations") or [] if isinstance(x, dict)]
                        p.location = ", ".join(filter(None, locs)) or None
                    if not p.experience and jd.get("minimumExperience") is not None:
                        p.experience = f"{jd.get('minimumExperience')}-{jd.get('maximumExperience')} Yrs"
                    p.apply_url = _find_redirect(jd) or p.apply_url
                    skills = [s.get("label") for s in (jd.get("keySkills") or {}).get("other", []) if s.get("label")]
                    p.skills = p.skills or skills
                try:
                    btn = await page.inner_text("body", timeout=3000)
                    if "apply on company site" in btn.lower():
                        p.extra["company_site"] = True
                        p.easy_apply = False
                    elif "already applied" in btn.lower():
                        p.extra["already_applied"] = True
                except Exception:
                    pass
                await human_pause(2, 5)
