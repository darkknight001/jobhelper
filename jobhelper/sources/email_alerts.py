"""Turn job-alert emails (Naukri Priority Applicant, LinkedIn, Indeed) into postings.

Alert emails carry links, not full postings. We pull job IDs out of the links (anchor
text is usually the title), then the pipeline fetches details like any other posting.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

from .. import db, mail
from .base import Posting

log = logging.getLogger(__name__)

PATTERNS = [
    ("naukri", re.compile(r"^https?://(?:[a-z0-9-]+\.)*naukri\.com/job-listings-[^\s\"'?#]*?-(\d{9,14})", re.I)),
    ("naukri", re.compile(r"^https?://(?:[a-z0-9-]+\.)*naukri\.com/[^\s\"']*?[?&]jobId=(\d{9,14})", re.I)),
    ("linkedin", re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/\s\"']*-)?(\d{8,12})", re.I)),
    ("indeed", re.compile(r"indeed\.[a-z.]+/[^\s\"']*?[?&]jk=([0-9a-f]{16})", re.I)),
]
URLS = {
    "naukri": "https://www.naukri.com/job-listings-{id}",
    "linkedin": "https://www.linkedin.com/jobs/view/{id}/",
    "indeed": "https://in.indeed.com/viewjob?jk={id}",
}


def _unwrap(href: str) -> str:
    """Tracking redirects often carry the real URL in a query param."""
    href = unquote(href)
    q = parse_qs(urlparse(href).query)
    for key in ("url", "u", "redirect", "target", "dest"):
        if key in q and q[key][0].startswith("http"):
            return unquote(q[key][0])
    return href


def extract(m: mail.Mail) -> list[Posting]:
    found: dict[tuple[str, str], Posting] = {}
    priority = bool(re.search(r"priority", f"{m.subject} {m.text[:3000]}", re.I))
    via = f"alert:{m.sender_domain}" + (":priority" if priority else "")
    candidates: list[tuple[str, str]] = []
    if m.html:
        soup = BeautifulSoup(m.html, "lxml")
        for a in soup.find_all("a", href=True):
            candidates.append((_unwrap(a["href"]), a.get_text(" ", strip=True)))
    candidates += [(u, "") for u in re.findall(r"https?://\S+", m.text or "")]
    for href, anchor in candidates:
        for source, pat in PATTERNS:
            hit = pat.search(href)
            if not hit:
                continue
            key = (source, hit.group(1))
            title = anchor if 4 <= len(anchor) <= 140 and not re.search(r"apply|view|unsubscribe|see all", anchor, re.I) else ""
            if key in found:
                if title and not found[key].title:
                    found[key].title = title
                continue
            url = href if source == "naukri" and "job-listings" in href else URLS[source].format(id=hit.group(1))
            found[key] = Posting(source=source, external_id=hit.group(1), url=url.split("?")[0] if source != "indeed" else url,
                                 title=title, company="", via=via, priority=priority and source == "naukri",
                                 extra={"alert_subject": m.subject})
            break
    return list(found.values())


# Platforms send alerts AND recruiter/status mail from the same domains. Mail whose subject
# looks like a status update is left for the tracker even if it contains job links.
STATUS_SUBJECT = re.compile(r"interview|shortlist|assessment|application\b.{0,25}\b(status|update|viewed|received|"
                            r"submitted|progress)|applied|regret|unfortunately|offer|recruiter\b.{0,15}\b(message|reply|"
                            r"viewed)|next steps|viewed your|wants to connect|is hiring", re.I)


async def discover(senders: list[str], days: int = 2) -> tuple[list[Posting], dict[str, Any]]:
    mails = await mail.search(senders, f"newer_than:{days}d", limit=60)
    postings: list[Posting] = []
    stats = {"emails": 0, "alert_emails": 0, "left_for_tracker": 0}
    with db.session() as s:
        for m in mails:
            stats["emails"] += 1
            if s.get(db.EmailSeen, m.message_id):
                continue
            got = extract(m)
            if not got or STATUS_SUBJECT.search(m.subject or ""):
                stats["left_for_tracker"] += 1
                continue
            s.add(db.EmailSeen(message_id=m.message_id, kind="alert", subject=m.subject[:500],
                               sender=m.sender, result={"postings": len(got)}))
            stats["alert_emails"] += 1
            postings += got
    stats["postings"] = len(postings)
    return postings, stats
