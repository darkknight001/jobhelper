"""The harness's prompts come from the same SKILL.md files Claude Code / claude.ai use.

Editing `.claude/skills/<name>/SKILL.md` changes both the interactive skill and the
harness's behaviour, so there's one source of truth.
"""
from __future__ import annotations

import re
from functools import lru_cache

from . import config

SKILLS_DIR = config.ROOT / ".claude" / "skills"
_FM = re.compile(r"^---\s*\n.*?\n---\s*\n", re.S)


@lru_cache(maxsize=None)
def body(name: str) -> str:
    raw = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
    return _FM.sub("", raw, count=1).strip()


def names() -> list[str]:
    return sorted(p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md"))
