"""Indeed India discovery in the shared browser (it sits behind Cloudflare).

Job cards come from the JSON the search page embeds in `window.mosaic.providerData`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from ..browser import BROWSER, human_pause, is_challenge
from .base import Posting, html_to_text, looks_remote

log = logging.getLogger(__name__)
BASE = "https://in.indeed.com"

CARDS_JS = """() => {
  const pd = window.mosaic && window.mosaic.providerData && window.mosaic.providerData["mosaic-provider-jobcards"];
  return pd && pd.metaData && pd.metaData.mosaicProviderJobCardsModel
    ? pd.metaData.mosaicProviderJobCardsModel.results : null;
}"""


def parse_cards(results: list[dict[str, Any]]) -> list[Posting]:
    out = []
    for r in results or []:
        jk = r.get("jobkey")
        if not jk:
            continue
        loc = r.get("formattedLocation")
        pub = r.get("pubDate")
        sal = (r.get("salarySnippet") or {}).get("text") or (r.get("extractedSalary") or {}).get("text")
        out.append(Posting(
            source="indeed", external_id=jk, url=f"{BASE}/viewjob?jk={jk}",
            title=r.get("displayTitle") or r.get("title") or "", company=r.get("company") or "",
            location=loc, salary=sal, description=html_to_text(r.get("snippet")) or None,
            remote=bool(r.get("remoteLocation")) or looks_remote(loc),
            easy_apply=bool(r.get("indeedApplyEnabled")),
            posted_at=datetime.fromtimestamp(pub / 1000, timezone.utc) if isinstance(pub, (int, float)) else None,
        ))
    return out


async def search(keywords: list[str], locations: list[str], *, max_age_days: int,
                 pages: int = 2) -> tuple[list[Posting], dict[str, Any]]:
    seen: dict[str, Posting] = {}
    stats: dict[str, Any] = {}
    async with BROWSER.lock:
        async with BROWSER.page() as page:
            for kw in keywords:
                for loc in locations:
                    got = 0
                    for n in range(pages):
                        q = {"q": kw, "l": loc, "fromage": max_age_days, "sort": "date", "start": n * 10}
                        if loc.lower() == "remote":
                            q = {"q": kw, "l": "", "fromage": max_age_days, "sort": "date", "start": n * 10,
                                 "sc": "0kf:attr(DSQF7);"}   # Indeed's "Remote" filter
                        await page.goto(f"{BASE}/jobs?{urlencode(q)}", wait_until="domcontentloaded", timeout=45000)
                        await human_pause(2, 4)
                        wall = await is_challenge(page)
                        if wall:
                            stats["blocked"] = wall
                            return list(seen.values()), stats
                        results = await page.evaluate(CARDS_JS)
                        batch = parse_cards(results or [])
                        for p in batch:
                            seen.setdefault(p.external_id, p)
                        got += len(batch)
                        await human_pause(3, 7)
                        if len(batch) < 10:
                            break
                    stats[f"{kw} @ {loc}"] = got
    return list(seen.values()), stats


async def enrich(postings: list[Posting]) -> None:
    if not postings:
        return
    async with BROWSER.lock:
        async with BROWSER.page() as page:
            for p in postings:
                await page.goto(p.url, wait_until="domcontentloaded", timeout=45000)
                await human_pause(2, 4)
                if await is_challenge(page):
                    break
                try:
                    p.description = (await page.inner_text("#jobDescriptionText", timeout=8000))[:12000]
                except Exception:
                    pass
                try:
                    body = (await page.inner_text("body", timeout=3000)).lower()
                    if "apply on company site" in body:
                        p.easy_apply = False
                        p.extra["company_site"] = True
                except Exception:
                    pass
                await human_pause(2, 5)
