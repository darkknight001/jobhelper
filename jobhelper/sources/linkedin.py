"""LinkedIn discovery through the public guest job endpoints (no login).

Applying (Easy Apply) happens later in the logged-in browser; discovery stays logged
out so search traffic never touches your account.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup

from .base import UA, Posting, html_to_text, looks_remote

log = logging.getLogger(__name__)

SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?"
DETAIL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
VIEW = "https://www.linkedin.com/jobs/view/{id}/"


def parse_search(html: str) -> list[Posting]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for card in soup.select("div.base-card, div.job-search-card"):
        urn = card.get("data-entity-urn", "")
        m = re.search(r"jobPosting:(\d+)", urn)
        if not m:
            link = card.select_one("a.base-card__full-link")
            m = re.search(r"-(\d+)\?", link.get("href", "")) if link else None
        if not m:
            continue
        jid = m.group(1)
        title = card.select_one(".base-search-card__title")
        company = card.select_one(".base-search-card__subtitle")
        loc = card.select_one(".job-search-card__location")
        when = card.select_one("time")
        posted = None
        if when and when.get("datetime"):
            try:
                posted = datetime.fromisoformat(when["datetime"]).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        location = loc.get_text(strip=True) if loc else None
        out.append(Posting(
            source="linkedin", external_id=jid, url=VIEW.format(id=jid),
            title=title.get_text(strip=True) if title else "", company=company.get_text(strip=True) if company else "",
            location=location, remote=looks_remote(location), posted_at=posted,
        ))
    return out


def parse_detail(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    desc = soup.select_one(".show-more-less-html__markup, .description__text")
    criteria = {}
    for item in soup.select(".description__job-criteria-item"):
        k = item.select_one(".description__job-criteria-subheader")
        v = item.select_one(".description__job-criteria-text")
        if k and v:
            criteria[k.get_text(strip=True)] = v.get_text(strip=True)
    offsite = bool(soup.select_one('[data-tracking-control-name*="offsite"]')) or "apply-link-offsite" in html
    title = soup.select_one(".top-card-layout__title, .topcard__title")
    company = soup.select_one(".topcard__org-name-link, .topcard__flavor a")
    loc = soup.select_one(".topcard__flavor--bullet")
    return {"description": html_to_text(str(desc)) if desc else None, "criteria": criteria,
            "easy_apply": False if offsite else None,
            "title": title.get_text(strip=True) if title else None,
            "company": company.get_text(strip=True) if company else None,
            "location": loc.get_text(strip=True) if loc else None}


async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
    for attempt in range(3):
        r = await client.get(url)
        if r.status_code == 429:
            await asyncio.sleep(10 * (attempt + 1) + random.random() * 5)
            continue
        return r
    return None


async def search_only(keywords: list[str], locations: list[str], *, max_age_days: int,
                      max_results: int) -> list[Posting]:
    """Cards without details; the pipeline fetches details after the title prefilter."""
    seen: dict[str, Posting] = {}
    window = f"r{int(max_age_days) * 86400}"
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": UA}, follow_redirects=True) as client:
        for kw in keywords:
            for loc in locations:
                for start in range(0, max_results, 25):
                    q = urlencode({"keywords": kw, "location": loc, "f_TPR": window, "start": start})
                    r = await _get(client, SEARCH + q)
                    if r is None or r.status_code != 200 or not r.text.strip():
                        break
                    page = parse_search(r.text)
                    for p in page:
                        seen.setdefault(p.external_id, p)
                    await asyncio.sleep(1.5 + random.random() * 2)
                    if len(page) < 10:
                        break
    return list(seen.values())


async def fill_details(postings: list[Posting]) -> None:
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": UA}, follow_redirects=True) as client:
        for p in postings:
            r = await _get(client, DETAIL.format(id=p.external_id))
            if r is not None and r.status_code == 200:
                d = parse_detail(r.text)
                p.description = d["description"]
                p.easy_apply = d["easy_apply"]
                p.experience = d["criteria"].get("Seniority level")
                # Postings from alert emails arrive with only an ID.
                p.title = p.title or d["title"] or ""
                p.company = p.company or d["company"] or ""
                p.location = p.location or d["location"]
            await asyncio.sleep(1.0 + random.random() * 1.5)
