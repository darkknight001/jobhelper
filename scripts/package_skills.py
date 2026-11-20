"""Zip each skill in .claude/skills/ for upload to claude.ai (Settings → Capabilities → Skills).

    python scripts/package_skills.py      → dist/skills/<name>.zip
"""
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dist" / "skills"
OUT.mkdir(parents=True, exist_ok=True)
for skill in sorted((ROOT / ".claude" / "skills").glob("*/SKILL.md")):
    folder = skill.parent
    target = OUT / f"{folder.name}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for f in folder.rglob("*"):
            if f.is_file():
                z.write(f, Path(folder.name) / f.relative_to(folder))
    print(f"{target.relative_to(ROOT)}")
