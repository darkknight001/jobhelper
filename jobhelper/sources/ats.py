"""Public job-board APIs: Greenhouse, Lever, Ashby. No login, no scraping."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from .base import UA, Posting, html_to_text, looks_remote

log = logging.getLogger(__name__)

GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
LEVER = "https://api.lever.co/v0/postings/{token}?mode=json"
ASHBY = "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"


def _iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_greenhouse(token: str, data: dict[str, Any]) -> list[Posting]:
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name")
        out.append(Posting(
            source="greenhouse", external_id=f"{token}:{j['id']}", url=j.get("absolute_url", ""),
            apply_url=j.get("absolute_url"), title=j.get("title", ""),
            company=j.get("company_name") or token, location=loc,
            description=html_to_text(j.get("content")), remote=looks_remote(loc, j.get("title")),
            posted_at=_iso(j.get("first_published") or j.get("updated_at")), via="board",
        ))
    return out


def parse_lever(token: str, data: list[dict[str, Any]]) -> list[Posting]:
    out = []
    for j in data:
        cats = j.get("categories") or {}
        loc = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        desc = "\n\n".join(filter(None, [
            j.get("descriptionPlain"),
            *[f"{lst.get('text')}\n{html_to_text(lst.get('content'))}" for lst in j.get("lists") or []],
            j.get("additionalPlain"),
        ]))
        created = j.get("createdAt")
        out.append(Posting(
            source="lever", external_id=f"{token}:{j['id']}", url=j.get("hostedUrl", ""),
            apply_url=j.get("applyUrl"), title=j.get("text", ""), company=token, location=loc,
            description=desc[:12000], remote=(j.get("workplaceType") == "remote") or looks_remote(loc),
            posted_at=datetime.fromtimestamp(created / 1000, timezone.utc) if created else None, via="board",
        ))
    return out


def parse_ashby(token: str, data: dict[str, Any]) -> list[Posting]:
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        loc = j.get("location") or ""
        secondary = [x.get("location") for x in j.get("secondaryLocations") or [] if x.get("location")]
        if secondary:
            loc = ", ".join([loc, *secondary])
        comp = (j.get("compensation") or {}).get("compensationTierSummary")
        out.append(Posting(
            source="ashby", external_id=f"{token}:{j['id']}", url=j.get("jobUrl", ""),
            apply_url=j.get("applyUrl"), title=j.get("title", ""), company=token, location=loc,
            description=(j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")))[:12000],
            remote=bool(j.get("isRemote")) or looks_remote(loc), salary=comp,
            posted_at=_iso(j.get("publishedAt")), via="board",
        ))
    return out


PARSERS = {"greenhouse": (GREENHOUSE, parse_greenhouse), "lever": (LEVER, parse_lever),
           "ashby": (ASHBY, parse_ashby)}


async def fetch_board(client: httpx.AsyncClient, kind: str, token: str) -> list[Posting]:
    url_tpl, parse = PARSERS[kind]
    r = await client.get(url_tpl.format(token=token))
    if r.status_code == 404:
        log.warning("%s board %r not found", kind, token)
        return []
    r.raise_for_status()
    return parse(token, r.json())


async def discover(boards: dict[str, list[str]]) -> tuple[list[Posting], dict[str, Any]]:
    postings: list[Posting] = []
    stats: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": UA}, follow_redirects=True) as client:
        for kind, tokens in (boards or {}).items():
            if kind not in PARSERS:
                continue
            for token in tokens or []:
                try:
                    got = await fetch_board(client, kind, token)
                    postings += got
                    stats[f"{kind}:{token}"] = len(got)
                except Exception as exc:  # one bad board must not stop the rest
                    log.warning("board %s:%s failed: %s", kind, token, exc)
                    stats[f"{kind}:{token}"] = f"error: {exc}"
    return postings, stats


async def verify(boards: dict[str, list[str]]) -> dict[str, str]:
    """Report which configured board tokens resolve, and how many India/remote jobs each has."""
    out = {}
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": UA}, follow_redirects=True) as client:
        for kind, tokens in (boards or {}).items():
            for token in tokens or []:
                try:
                    got = await fetch_board(client, kind, token)
                    india = [p for p in got if p.location and any(
                        k in p.location.lower() for k in ("india", "bangalore", "bengaluru", "gurgaon",
                                                          "gurugram", "noida", "delhi", "mumbai", "remote"))]
                    out[f"{kind}:{token}"] = f"ok, {len(got)} jobs, {len(india)} India/remote" if got else "missing"
                except Exception as exc:
                    out[f"{kind}:{token}"] = f"error: {exc}"
    return out
