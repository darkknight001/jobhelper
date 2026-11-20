"""Application status read from the portals themselves: Naukri's application history and
LinkedIn's job tracker. Gmail misses most of this ("Resume viewed", "Shortlisted" often only
show on the portal).

Read-only: pages are opened in the shared, logged-in browser and read; the only clicks are
LinkedIn's page numbers. A status only ever moves forward (tracking.RANK).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import select

from . import db, notify, prep
from .browser import BROWSER
from .tracking import RANK

log = logging.getLogger(__name__)

# Checked in this order: "Not shortlisted" must read as rejected, not shortlisted.
RULES = [(re.compile(r"not shortlisted|not selected|rejected|no longer (being )?considered|not moving forward|declined", re.I), "rejected"),
         (re.compile(r"shortlisted", re.I), "shortlisted"),
         (re.compile(r"(application|resume|cv|profile) (viewed|downloaded)", re.I), "viewed")]
OPEN = ("applied", "already_applied", "viewed", "shortlisted", "assessment", "interview")
NAUKRI_PAGE = "https://www.naukri.com/myapply/historypage"
LINKEDIN_PAGE = "https://www.linkedin.com/jobs-tracker/?stage=applied"

CARDS_JS = r"""() => {
  const idOf = a => (a.href.match(/jobs\/view\/(\d+)/) || [])[1];
  const out = {};
  for (const a of document.querySelectorAll('a[href*="/jobs/view/"]')) {
    const id = idOf(a);
    if (!id || out[id]) continue;
    let el = a;   // the largest ancestor that still holds only this one job = its card
    while (el.parentElement && new Set([...el.parentElement.querySelectorAll('a[href*="/jobs/view/"]')].map(idOf)).size === 1)
      el = el.parentElement;
    out[id] = el.innerText.replace(/\s+/g, ' ').trim().slice(0, 400);
  }
  return out;
}"""


def status_of(text: str) -> str | None:
    """Portal wording → our status, or None when it says nothing new (applied, sent, closed)."""
    for rx, status in RULES:
        if rx.search(text or ""):
            return status
    return None


def naukri_statuses(data: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """{jobId: (status, Naukri's words)} from the history API, using the furthest step reached."""
    out = {}
    for item in data.get("applyDetails") or []:
        steps = [(status_of(s.get("statusValue", "")), s.get("statusValue", "")) for s in item.get("status") or []]
        steps = [x for x in steps if x[0]]
        if steps:
            out[str(item["jobId"])] = max(steps, key=lambda x: RANK.get(x[0], 0))
    return out


def linkedin_statuses(cards: dict[str, str]) -> dict[str, tuple[str, str]]:
    out = {}
    for job_id, text in cards.items():
        if (st := status_of(text)):
            out[job_id] = (st, next(rx.search(text).group(0) for rx, s in RULES if s == st))
    return out


def record(source: str, found: dict[str, tuple[str, str]]) -> list[dict[str, Any]]:
    """Move matching applications forward; returns what changed."""
    changes = []
    with db.session() as s:
        for ext_id, (new, words) in found.items():
            job = s.scalar(select(db.Job).where(db.Job.source == source, db.Job.external_id == ext_id))
            for a in (job.applications if job else []):
                if a.status in OPEN and RANK.get(new, 0) > RANK.get(a.status, 0):
                    a.status, a.last_update_at, job.status = new, db.utcnow(), new
                    db.add_event(s, f"portal_{new}", f"{source.capitalize()} shows: {words}", application=a)
                    changes.append({"application": a.id, "status": new, "title": job.title, "company": job.company,
                                    "source": source, "words": words})
    return changes


async def _naukri(ctx) -> dict[str, tuple[str, str]]:
    page = await ctx.new_page()
    req: dict[str, Any] = {}
    page.on("request", lambda r: req.setdefault("r", r) if "applyapi/v5/history?" in r.url else None)
    try:
        await page.goto(NAUKRI_PAGE, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(6000)
        if "r" not in req:
            raise RuntimeError("Naukri's history page didn't load its data (signed out?)")
        # The page asked for 10 rows; ask the same API, with the page's own headers, for all of them.
        url = re.sub(r"pageSize=\d+", "pageSize=200", req["r"].url)
        hdrs = {k: v for k, v in req["r"].headers.items() if not k.startswith(":") and k.lower() != "cookie"}
        status, body = await page.evaluate(
            "async ([u, h]) => { const r = await fetch(u, {headers: h, credentials: 'include'}); return [r.status, await r.text()]; }",
            [url, hdrs])
        if status != 200:
            raise RuntimeError(f"Naukri history API answered {status}")
        return naukri_statuses(json.loads(body))
    finally:
        await page.close()


async def _linkedin(ctx, max_pages: int = 12) -> dict[str, tuple[str, str]]:
    page = await ctx.new_page()
    cards: dict[str, str] = {}
    try:
        await page.goto(LINKEDIN_PAGE, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(6000)
        if "/login" in page.url or "checkpoint" in page.url:
            raise RuntimeError("LinkedIn isn't signed in in the remote browser")
        for _ in range(max_pages):
            got = await page.evaluate(CARDS_JS)
            new = {k: v for k, v in got.items() if k not in cards}
            cards.update(new)
            nxt = page.get_by_role("button", name=re.compile(r"^Next$"))
            if not new or not await nxt.count() or not await nxt.first.is_enabled():
                break
            await nxt.first.click()          # page numbers only; nothing on LinkedIn changes
            await page.wait_for_timeout(3000)
        return linkedin_statuses(cards)
    finally:
        await page.close()


async def check() -> dict[str, Any]:
    """Read both portals, record what moved, alert on shortlists. Scheduled daily."""
    stats: dict[str, Any] = {}
    changes: list[dict[str, Any]] = []
    with db.run_log("portal status") as run_stats:
        async with BROWSER.lock:              # never alongside an apply run or a Naukri search
            ctx = await BROWSER.context()
            for source, read in (("naukri", _naukri), ("linkedin", _linkedin)):
                try:
                    found = await read(ctx)
                    moved = record(source, found)
                    changes += moved
                    stats[source] = {"with_status": len(found), "updated": len(moved)}
                except Exception as exc:      # one portal failing mustn't stop the other
                    log.exception("portal status: %s failed", source)
                    stats[source] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        run_stats.update(stats)
    for c in changes:
        if c["status"] == "shortlisted":
            link = notify.link(f"/applications/{c['application']}")
            await notify.send(f"🎉 Shortlisted: {c['company']} — {c['title']}\n{c['source'].capitalize()} shows: "
                              f"{c['words']}\n{link}", kind="response")
            await prep.ensure_brief(c["application"], trigger=f"shortlisted on {c['source']}")
    stats["changes"] = changes
    return stats
