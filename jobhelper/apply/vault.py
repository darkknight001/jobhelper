"""Career-site logins the apply agent creates: one unique password per domain, encrypted."""
from __future__ import annotations

import secrets
import string

from cryptography.fernet import Fernet, InvalidToken

from .. import config, db
from ..mail import registrable, valid_domain


class VaultNotConfigured(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = config.secrets().vault_key
    if not key:
        raise VaultNotConfigured("JOBHELPER_VAULT_KEY is not set")
    return Fernet(key.encode())


def _password() -> str:
    # Satisfies the usual career-site rules: length, upper, lower, digit, symbol.
    alphabet = string.ascii_letters + string.digits
    core = "".join(secrets.choice(alphabet) for _ in range(14))
    return f"{core}Aa9!"


def get(domain: str) -> dict[str, str] | None:
    dom = registrable(domain)
    with db.session() as s:
        row = s.get(db.SiteLogin, dom)
        if not row:
            return None
        try:
            pw = _fernet().decrypt(row.password_enc.encode()).decode()
        except InvalidToken:
            return None
        return {"domain": dom, "username": row.username, "password": pw}


def create(domain: str, username: str, note: str | None = None) -> dict[str, str]:
    if not valid_domain(domain):
        raise ValueError(f"not a plain domain name: {domain!r}")
    dom = registrable(domain)
    existing = get(dom)
    if existing:
        return existing
    pw = _password()
    with db.session() as s:
        s.add(db.SiteLogin(domain=dom, username=username, password_enc=_fernet().encrypt(pw.encode()).decode(),
                           note=note))
    return {"domain": dom, "username": username, "password": pw}


def list_logins() -> list[dict[str, str]]:
    with db.session() as s:
        return [{"domain": r.domain, "username": r.username, "created_at": r.created_at.isoformat(),
                 "note": r.note or ""} for r in s.query(db.SiteLogin).order_by(db.SiteLogin.created_at.desc())]
