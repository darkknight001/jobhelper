"""Point the harness at a throwaway data dir BEFORE jobhelper is imported."""
import os
import shutil
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="jobhelper-test-"))
os.environ["JOBHELPER_DATA_DIR"] = str(_TMP)
os.environ["JOBHELPER_NO_SCHEDULER"] = "1"
# Tests run against the committed example, never the owner's own config/settings.yaml.
os.environ["JOBHELPER_SETTINGS_FILE"] = str(Path(__file__).resolve().parent.parent / "config" / "settings.example.yaml")
os.environ.setdefault("JOBHELPER_VAULT_KEY", "3q2-7wiaFCXbNe2rbvjJ7Vb1A0sT0jUGzxHp6QeJqs4=")
os.environ["HERMES_WEBHOOK_URL"] = ""
os.environ["GMAIL_APP_PASSWORD"] = ""

ROOT = Path(__file__).resolve().parent.parent
(_TMP / "resumes").mkdir(parents=True, exist_ok=True)
(_TMP / "resumes" / "resume.pdf").write_bytes(b"%PDF-1.4 test")
(_TMP / "overrides.yaml").write_text("apply:\n  resume: resume.pdf\n", encoding="utf-8")   # pin the main resume, as a real setup does
shutil.copy(ROOT / "config" / "profile.example.yaml", _TMP / "profile.yaml")

import pytest  # noqa: E402

from jobhelper import db  # noqa: E402


@pytest.fixture(autouse=True)
def no_company_lookups(monkeypatch):
    """Scoring screens companies on AmbitionBox; tests never touch the network."""
    from jobhelper import companies

    async def offline(client, name):
        return None, {"note": "offline in tests"}

    monkeypatch.setattr(companies, "fetch", offline)
    monkeypatch.setattr(companies, "DELAY", 0)


@pytest.fixture(autouse=True)
def clean_db():
    db.engine()
    with db.session() as s:
        for model in (db.Event, db.Application, db.Job, db.EmailSeen, db.SiteLogin, db.Run, db.CompanyRating):
            s.query(model).delete()
    yield
