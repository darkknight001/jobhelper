"""Paths, secrets (.env) and the YAML settings/profile files.

Settings come from config/settings.yaml, falling back to the committed settings.example.yaml.
"""
from __future__ import annotations

import logging
import os
import secrets as _secrets
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

ROOT = Path(os.environ.get("JOBHELPER_ROOT", Path(__file__).resolve().parent.parent))
CONFIG_DIR = Path(os.environ.get("JOBHELPER_CONFIG_DIR", ROOT / "config"))
DATA_DIR = Path(os.environ.get("JOBHELPER_DATA_DIR", ROOT / "data"))
KB_DIR = DATA_DIR / "kb"
RESUME_DIR = DATA_DIR / "resumes"
BROWSER_DIR = DATA_DIR / "browser"
ARTIFACT_DIR = DATA_DIR / "artifacts"   # screenshots and agent transcripts per application
DB_PATH = DATA_DIR / "jobhelper.sqlite"

for _env in (ROOT / ".env", ROOT / "deploy" / ".env"):
    if _env.exists():
        load_dotenv(_env, override=False)


@dataclass(frozen=True)
class Secrets:
    claude_oauth_token: str
    dashboard_password: str
    session_secret: str
    api_token: str
    public_url: str
    bind: str
    vault_key: str
    gmail_address: str
    gmail_app_password: str
    hermes_webhook_url: str
    hermes_webhook_secret: str
    cdp_port: int
    headless: bool
    vnc_password: str
    in_container: bool
    session_secret_set: bool = True


@lru_cache(maxsize=1)
def secrets() -> Secrets:
    e = os.environ.get
    session_secret = e("JOBHELPER_SESSION_SECRET", "")
    if not session_secret:   # never a public constant: a random per-process key, so cookies can't be forged
        session_secret = _secrets.token_urlsafe(32)
        logging.getLogger(__name__).warning(
            "JOBHELPER_SESSION_SECRET is not set: using a random one, so logins reset on every restart")
    return Secrets(
        claude_oauth_token=e("CLAUDE_CODE_OAUTH_TOKEN", ""),
        dashboard_password=e("JOBHELPER_DASHBOARD_PASSWORD", ""),
        session_secret=session_secret,
        session_secret_set=bool(e("JOBHELPER_SESSION_SECRET", "")),
        api_token=e("JOBHELPER_API_TOKEN", ""),
        public_url=e("JOBHELPER_PUBLIC_URL", "http://127.0.0.1:8095").rstrip("/"),
        bind=e("JOBHELPER_BIND", "127.0.0.1:8095"),
        vault_key=e("JOBHELPER_VAULT_KEY", ""),
        gmail_address=e("GMAIL_ADDRESS", ""),
        gmail_app_password=e("GMAIL_APP_PASSWORD", "").replace(" ", ""),
        hermes_webhook_url=e("HERMES_WEBHOOK_URL", ""),
        hermes_webhook_secret=e("HERMES_WEBHOOK_SECRET", ""),
        cdp_port=int(e("JOBHELPER_CDP_PORT", "9333")),
        headless=e("JOBHELPER_HEADLESS", "0") == "1",
        vnc_password=e("JOBHELPER_VNC_PASSWORD", ""),
        in_container=e("JOBHELPER_CONTAINER", "0") == "1",
    )


def ensure_dirs() -> None:
    for d in (DATA_DIR, KB_DIR, RESUME_DIR, BROWSER_DIR, ARTIFACT_DIR):
        d.mkdir(parents=True, exist_ok=True)
    profile = DATA_DIR / "profile.yaml"
    if not profile.exists():
        shutil.copy(CONFIG_DIR / "profile.example.yaml", profile)


def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


OVERRIDES = DATA_DIR / "overrides.yaml"


def settings_file() -> Path:
    """$JOBHELPER_SETTINGS_FILE if set, else config/settings.yaml, else the committed example."""
    if env := os.environ.get("JOBHELPER_SETTINGS_FILE"):
        return Path(env)
    own = CONFIG_DIR / "settings.yaml"   # gitignored: each user's own copy
    return own if own.exists() else CONFIG_DIR / "settings.example.yaml"


def settings() -> dict[str, Any]:
    """settings file + dashboard overrides. Re-read on every call so edits apply live."""
    base = _load_yaml(settings_file())
    return _merge(base, _load_yaml(OVERRIDES)) if OVERRIDES.exists() else base


def set_override(path: str, value: Any) -> None:
    """Persist one dashboard toggle, e.g. set_override('apply.dry_run', False)."""
    data = _load_yaml(OVERRIDES) if OVERRIDES.exists() else {}
    node = data
    keys = path.split(".")
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value
    OVERRIDES.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def profile() -> dict[str, Any]:
    """The user's profile exactly as saved from the dashboard: the single source of truth."""
    return _load_yaml(DATA_DIR / "profile.yaml")


SALARY_FIELDS = ("current_ctc_lpa", "current_fixed_lpa", "expected_ctc_lpa")


def profile_for_forms() -> dict[str, Any]:
    """The profile plus every salary figure pre-converted to the formats forms ask for.

    Only the base numbers (lakhs per annum) are stored; whole-lakh and rupee versions are
    derived here on every read, so editing one number on the dashboard updates all of them.
    """
    p = profile()
    comp = dict(p.get("compensation") or {})
    for key in SALARY_FIELDS:                   # e.g. current_ctc_lpa
        whole, inr = f"{key}_whole", key.replace("_lpa", "_inr")   # current_ctc_lpa_whole, current_ctc_inr
        for stale in (whole, inr, f"{key}_inr"):
            comp.pop(stale, None)               # never trust stored copies; always re-derive
        v = comp.get(key)
        if v is None:
            continue
        try:
            lakhs = float(v)
        except (TypeError, ValueError):
            continue
        comp[whole] = int(lakhs)                             # rounded down, e.g. 20.5 -> 20
        comp[inr] = int(round(lakhs * 100000))               # e.g. 20.5 -> 2050000
    p["compensation"] = comp
    return p


def profile_path() -> Path:
    return DATA_DIR / "profile.yaml"


def tz() -> ZoneInfo:
    return ZoneInfo(settings().get("timezone", "Asia/Kolkata"))
