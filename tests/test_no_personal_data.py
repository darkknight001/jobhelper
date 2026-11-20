"""Fail if any private string from data/private_patterns.txt is in a file git would commit.

The patterns file is gitignored: data/private_patterns.txt, one case-insensitive
substring per line (blank lines and lines starting with '#' are ignored). Clones
without it skip. Patterns are never printed except as the match label on failure.
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PATTERNS = ROOT / "data" / "private_patterns.txt"
MAX_BYTES = 2 * 1024 * 1024
MAX_HITS = 50


def _patterns():
    if not PATTERNS.exists():
        pytest.skip("no data/private_patterns.txt")
    lines = PATTERNS.read_text(encoding="utf-8", errors="ignore").splitlines()
    pats = (ln.strip().lower() for ln in lines)
    return [p for p in pats if p and not p.startswith("#")]


def _committable_files():
    # Temp bare git dir so the repo's own .git (if any) is not touched; read-only on the work tree.
    tmp = tempfile.mkdtemp()
    try:
        subprocess.run(["git", "init", "--bare", "-q", tmp], check=True, capture_output=True)
        out = subprocess.run(
            ["git", f"--git-dir={tmp}", f"--work-tree={ROOT}", "ls-files",
             "--others", "--cached", "--exclude-standard"],
            check=True, capture_output=True, text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git not available")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return [f for f in out.splitlines() if f]


def test_no_private_strings_in_committable_files():
    patterns = _patterns()
    hits = []
    for rel in _committable_files():
        path = ROOT / rel
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw:
            continue
        text = raw.decode("utf-8", errors="ignore")
        for n, line in enumerate(text.splitlines(), 1):
            low = line.lower()
            for pat in patterns:
                if pat in low:
                    hits.append(f"{rel.replace(os.sep, '/')}:{n} matches pattern {pat!r}")
                    break
    assert not hits, (
        f"{len(hits)} private-string hit(s) in committable files"
        f" (showing up to {MAX_HITS}):\n" + "\n".join(hits[:MAX_HITS])
    )
