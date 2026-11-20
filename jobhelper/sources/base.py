"""Common posting shape and the upsert every source feeds into."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup
from sqlalchemy import select

from .. import db

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")


@dataclass
class Posting:
    source: str
    external_id: str
    url: str
    title: str
    company: str
    location: str | None = None
    description: str | None = None
    remote: bool = False
    experience: str | None = None
    salary: str | None = None
    skills: list[str] = field(default_factory=list)
    posted_at: datetime | None = None
    apply_url: str | None = None
    easy_apply: bool | None = None
    via: str = "search"
    priority: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def html_to_text(raw: str | None, limit: int = 12000) -> str:
    if not raw:
        return ""
    text = BeautifulSoup(html.unescape(raw), "lxml").get_text("\n")
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()[:limit]


def looks_remote(*parts: str | None) -> bool:
    blob = " ".join(p for p in parts if p).lower()
    return any(k in blob for k in ("remote", "work from home", "wfh", "anywhere"))


def upsert(postings: list[Posting]) -> dict[str, int]:
    """Insert new postings; refresh fields on ones we already have. Returns counts."""
    new = updated = 0
    with db.session() as s:
        for p in postings:
            existing = s.scalar(select(db.Job).where(db.Job.source == p.source,
                                                     db.Job.external_id == p.external_id))
            if existing:
                changed = False
                for attr in ("description", "apply_url", "salary", "experience", "location", "easy_apply"):
                    val = getattr(p, attr)
                    if val and not getattr(existing, attr):
                        setattr(existing, attr, val)
                        changed = True
                if p.priority and not existing.priority:
                    existing.priority, existing.via, changed = True, p.via, True
                updated += int(changed)
                continue
            s.add(db.Job(
                source=p.source, external_id=p.external_id, url=p.url, apply_url=p.apply_url,
                title=p.title.strip()[:300], company=(p.company or "Unknown").strip()[:200],
                location=p.location, remote=p.remote or looks_remote(p.location, p.title),
                experience=p.experience, salary=p.salary, description=p.description,
                skills=p.skills, posted_at=p.posted_at, via=p.via, priority=p.priority,
                easy_apply=p.easy_apply,
            ))
            new += 1
    return {"new": new, "updated": updated}
