"""Cheap, deterministic filter that runs before any LLM call."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

INDIA_PLACES = ("india", "delhi", "ncr", "gurgaon", "gurugram", "noida", "bangalore", "bengaluru",
                "hyderabad", "pune", "mumbai", "chennai")


@dataclass
class Verdict:
    ok: bool
    reason: str = ""


def parse_experience(text: str | None) -> tuple[int | None, int | None]:
    """'5-10 Yrs' -> (5, 10); '8+ years' -> (8, None); otherwise (None, None)."""
    if not text:
        return None, None
    m = re.search(r"(\d{1,2})\s*(?:-|to|–)\s*(\d{1,2})\s*(?:\+)?\s*(?:yrs?|years?)", text, re.I)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d{1,2})\s*\+?\s*(?:yrs?|years?)", text, re.I)
    if m:
        return int(m.group(1)), None
    return None, None


def too_old(posted_at, discovered_at, settings: dict[str, Any]) -> bool:
    """Older than apply.max_job_age_days. Undated postings count from when we first saw them."""
    seen = posted_at or discovered_at or datetime.now(timezone.utc)
    return seen < datetime.now(timezone.utc) - timedelta(days=settings["apply"].get("max_job_age_days", 7))


def check_title(title: str, settings: dict[str, Any]) -> Verdict:
    t = title.lower()
    for pat in settings["targets"].get("exclude_titles", []):
        if re.search(pat, t, re.I):
            return Verdict(False, f"title excluded ({pat})")
    for pat in settings["targets"]["roles"]:
        if re.search(pat, t, re.I):
            return Verdict(True)
    return Verdict(False, "title is not a target role")


def check_location(location: str | None, remote: bool, settings: dict[str, Any]) -> Verdict:
    if not location:
        return Verdict(True)  # unknown: let the scorer judge from the description
    loc = location.lower()
    in_india = any(p in loc for p in INDIA_PLACES)
    if remote or "remote" in loc or "anywhere" in loc:
        # Pass remote roles only when they name India, or say nothing but "Remote"
        # (the scorer then judges eligibility from the description).
        leftover = re.sub(r"remote|anywhere|work from home|wfh|hybrid|[^a-z]+", "", loc)
        if in_india or not leftover:
            return Verdict(True)
        return Verdict(False, f"remote outside India ({location})")
    wanted = [w.lower() for w in settings["targets"]["locations"] if w.lower() not in ("india", "remote")]
    if any(w in loc for w in wanted):
        return Verdict(True)
    if in_india:
        return Verdict(False, f"India but not a target city ({location})")
    return Verdict(False, f"location not targeted ({location})")


def check_experience(text: str | None, settings: dict[str, Any]) -> Verdict:
    years = settings["candidate"]["years_experience"]
    stretch = settings["candidate"].get("stretch_years", 3)
    lo, hi = parse_experience(text)
    if lo is not None and lo > years + stretch:
        return Verdict(False, f"needs {lo}+ years")
    if hi is not None and hi < years - 1:
        return Verdict(False, f"capped at {hi} years")
    return Verdict(True)


def evaluate(title: str, location: str | None, remote: bool, experience: str | None,
             description: str | None, settings: dict[str, Any]) -> Verdict:
    for v in (check_title(title, settings), check_location(location, remote, settings)):
        if not v.ok:
            return v
    # Portals often put the band only in the description ("Experience: 8-12 years").
    exp_text = experience
    if not parse_experience(exp_text)[0] and description:
        m = re.search(r"(\d{1,2}\s*(?:-|to|–)\s*\d{1,2}|\d{1,2}\s*\+)\s*(?:yrs?|years?)", description, re.I)
        exp_text = m.group(0) if m else None
    return check_experience(exp_text, settings)
