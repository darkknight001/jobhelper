"""Knowledge base: Markdown files in data/kb/, indexed in SQLite FTS5.

Each file may start with YAML front matter (title, tags, source). Notes added from the
dashboard or Hermes are appended to data/kb/notes.md so they stay human-editable.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text

from . import config, db

FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def _parse(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_text(encoding="utf-8")
    meta: dict[str, Any] = {}
    m = FRONT_MATTER.match(raw)
    if m:
        meta = yaml.safe_load(m.group(1)) or {}
        raw = raw[m.end():]
    return meta, raw


def files() -> list[Path]:
    config.KB_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(p for p in config.KB_DIR.glob("*.md"))


def _chunks(body: str) -> list[tuple[str, str]]:
    """Split on ## / ### headings so search returns focused passages."""
    parts: list[tuple[str, str]] = []
    heading, buf = "", []
    for line in body.splitlines():
        if re.match(r"^#{1,3}\s", line):
            if "".join(buf).strip():
                parts.append((heading, "\n".join(buf).strip()))
            heading, buf = line.lstrip("#").strip(), [line]
        else:
            buf.append(line)
    if "".join(buf).strip():
        parts.append((heading, "\n".join(buf).strip()))
    return parts


def reindex() -> int:
    n = 0
    with db.engine().begin() as conn:
        conn.execute(text("DELETE FROM kb_fts"))
        for p in files():
            meta, body = _parse(p)
            tags = " ".join(meta.get("tags", []) or [])
            doc_title = meta.get("title") or p.stem
            for heading, chunk in _chunks(body):
                conn.execute(text("INSERT INTO kb_fts(path, title, body, tags) VALUES (:p, :t, :b, :g)"),
                             {"p": p.name, "t": f"{doc_title} › {heading}" if heading else doc_title,
                              "b": chunk, "g": tags})
                n += 1
    return n


PRIVATE_PREFIX = "private-"   # never searchable, never in employer-facing context


def is_private(name: str) -> bool:
    return name.startswith(PRIVATE_PREFIX)


def search(query: str, limit: int = 6) -> list[dict[str, str]]:
    """Search shareable KB passages. Private files are never returned."""
    terms = [t for t in re.findall(r"[\w+#.]+", query.lower()) if len(t) > 1]
    if not terms:
        return []
    fts_query = " OR ".join(f'"{t}"' for t in terms)
    with db.engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT path, title, body FROM kb_fts WHERE kb_fts MATCH :q AND path NOT LIKE :priv "
            "ORDER BY rank LIMIT :n"),
            {"q": fts_query, "priv": f"{PRIVATE_PREFIX}%", "n": limit}).fetchall()
    return [{"file": r.path, "title": r.title, "text": r.body[:1500]} for r in rows]


def claims_policy() -> str:
    p = config.KB_DIR / "claims-policy.md"
    return _parse(p)[1].strip() if p.exists() else ""


def add_note(text_: str, *, source: str = "dashboard", topic: str | None = None) -> Path:
    path = config.KB_DIR / "notes.md"
    if not path.exists():
        path.write_text("---\ntitle: Notes added later\ntags: [notes]\n---\n\n", encoding="utf-8")
    stamp = datetime.now(config.tz()).strftime("%Y-%m-%d %H:%M")
    heading = topic.strip() if topic else f"Note {stamp}"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n## {heading}\n_{stamp} · via {source}_\n\n{text_.strip()}\n")
    reindex()
    return path


def import_document(name: str, content: str, *, title: str | None = None, tags: list[str] | None = None) -> Path:
    """Write (or replace) a whole KB document, e.g. the claude.ai export."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "doc"
    path = config.KB_DIR / f"{slug}.md"
    meta = {"title": title or name, "tags": tags or [], "imported": datetime.now(config.tz()).isoformat()}
    body = FRONT_MATTER.sub("", content, count=1)
    path.write_text(f"---\n{yaml.safe_dump(meta, sort_keys=False)}---\n\n{body.strip()}\n", encoding="utf-8")
    reindex()
    return path


def resume_text(pdf: Path) -> str:
    from pypdf import PdfReader
    return "\n".join((page.extract_text() or "") for page in PdfReader(str(pdf)).pages)


def profile_block() -> str:
    """The dashboard profile, rendered first in every prompt. It is the single source of truth
    for compensation, notice, location, relocation and contact details."""
    p = config.profile_for_forms()
    return ("<profile source=\"dashboard\" authority=\"overrides every KB document below\">\n"
            + yaml.safe_dump(p, sort_keys=False, allow_unicode=True).strip()
            + "\n</profile>")


def candidate_brief(max_chars: int = 30000, *, include_private: bool = False) -> str:
    """Everything the scorer / tracker should know, in one block.

    The dashboard profile comes first and wins over anything else; then the claims policy,
    the resume, the claude.ai export and notes. Private files are included only for the
    user's own prep briefs.
    """
    order = {"claims-policy.md": 0, "resume.md": 1, "claude-export.md": 2, "notes.md": 3}
    docs = sorted((p for p in files() if include_private or not is_private(p.name)),
                  key=lambda p: (order.get(p.name, 4), p.name))
    out = [profile_block()]
    used = len(out[0])
    for p in docs:
        _, body = _parse(p)
        chunk = f"<kb file=\"{p.name}\">\n{body.strip()}\n</kb>"
        if used + len(chunk) > max_chars:
            chunk = chunk[: max(0, max_chars - used)]
        out.append(chunk)
        used += len(chunk)
        if used >= max_chars:
            break
    return "\n\n".join(out)


def list_docs() -> list[dict[str, Any]]:
    out = []
    for p in files():
        meta, body = _parse(p)
        out.append({"file": p.name, "title": meta.get("title") or p.stem, "tags": meta.get("tags") or [],
                    "chars": len(body), "modified": datetime.fromtimestamp(p.stat().st_mtime)})
    return out
