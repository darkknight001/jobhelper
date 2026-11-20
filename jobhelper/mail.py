"""Read-only, allowlisted Gmail access over IMAP (app password).

This module is the ONLY code that touches the mailbox, and it enforces the user's rules
(README.md → Guardrails):

1. Only recruiter / job-platform mail is read. Every search is restricted server-side with
   a `from:` clause built from the sender allowlist, and each hit's From header is checked
   again before its body is fetched. Messages that fail the check are never opened.
   Companies (career sites applied on, their logins, learned company domains) can also send
   you other mail, so theirs is opened only when the Subject is about a job application;
   job platforms and ATS systems in settings are trusted as they are.
2. Nothing is ever deleted, moved, flagged or replied to. The mailbox is opened with
   EXAMINE (read-only), bodies are fetched with BODY.PEEK (no \\Seen flag), and the IMAP
   connection refuses every command outside a read-only allowlist. There is no SMTP code.
3. Gmail is used only for job alerts, application status and site verification codes.
   Verification lookups are scoped to the career site's own domain.
4. To learn which domain a company you applied to writes from, only the From and Subject
   headers of mail whose sender matches that company's name are read. A domain is learned only
   from a job-related subject, if it carries the company's name and isn't a mailbox provider;
   nothing else is opened.

The app password itself can do more than this; these checks are what limit it.
"""
from __future__ import annotations

import asyncio
import email
import hashlib
import imaplib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Iterable

from sqlalchemy import select

from . import config, db
from .sources.base import html_to_text


class MailNotConfigured(RuntimeError):
    pass


class GuardrailViolation(RuntimeError):
    """Raised when code asks the mail layer for something the guardrails forbid."""


# ---------------------------------------------------------------- read-only IMAP connection

READ_ONLY_COMMANDS = {"CAPABILITY", "LOGIN", "EXAMINE", "SEARCH", "FETCH", "LOGOUT", "NOOP"}
READ_ONLY_UID_SUBCOMMANDS = {"SEARCH", "FETCH"}


class ReadOnlyIMAP(imaplib.IMAP4_SSL):
    """IMAP4_SSL that refuses any command able to change the mailbox."""

    def _command(self, name, *args):  # type: ignore[override]
        cmd = name.upper()
        if cmd == "UID":
            sub = (args[0] if args else "").upper()
            if sub not in READ_ONLY_UID_SUBCOMMANDS:
                raise GuardrailViolation(f"IMAP UID {sub} is not allowed (read-only mail access)")
        elif cmd not in READ_ONLY_COMMANDS:
            raise GuardrailViolation(f"IMAP {cmd} is not allowed (read-only mail access)")
        if cmd == "FETCH" and len(args) > 1 and re.search(r"BODY\[", str(args[1]), re.I):
            raise GuardrailViolation("FETCH must use BODY.PEEK so messages stay unread")
        return super()._command(name, *args)

    def select(self, mailbox="INBOX", readonly=False):  # type: ignore[override]
        return super().select(mailbox, readonly=True)   # always EXAMINE


# ---------------------------------------------------------------- sender allowlist

_temporary_domains: set[str] = set()   # the job's own sites while it is applied to: trusted
_gated_domains: set[str] = set()       # other sites the run reached (a page can send it anywhere): subject-gated

# Personal mailbox and sign-in providers. Mail from them is account alerts, calendar invites,
# OTPs and friends, so a career site, apply link or agent request can never add them. A
# recruiter's exact address can still be listed in settings (email.extra_senders).
NEVER_AUTO = {"google.com", "gmail.com", "googlemail.com", "microsoft.com", "outlook.com", "hotmail.com",
              "live.com", "apple.com", "icloud.com", "yahoo.com", "proton.me", "protonmail.com", "zoho.com",
              "zoho.in", "rediffmail.com"}


def _norm_domain(d: str) -> str:
    d = d.strip().lower().lstrip("@")
    if "@" in d:
        d = d.rsplit("@", 1)[1]
    return d


# What a sender entry may look like before it goes into a Gmail X-GM-RAW query (no quotes, spaces, syntax).
SAFE_SENDER = re.compile(r"([a-z0-9._%+-]+@)?[a-z0-9-]+(\.[a-z0-9-]+)+")


def valid_domain(d: str) -> bool:
    """A plain hostname (letters, digits, dots, hyphens). Agent-supplied domains must pass this."""
    return bool(re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", _norm_domain(d or "")))


def registrable(host: str) -> str:
    """careers.acme.co.in -> acme.co.in ; jobs.acme.com -> acme.com (good enough for mail)."""
    parts = _norm_domain(host).split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def trusted_senders() -> set[str]:
    """Job platforms and ATS systems from settings, plus the career site being applied to right now."""
    s = config.settings().get("email", {})
    configured = {_norm_domain(x) for x in (s.get("alert_senders") or []) + (s.get("tracking_domains") or [])
                  + (s.get("extra_senders") or [])}
    return {a for a in configured | (_temporary_domains - NEVER_AUTO) if SAFE_SENDER.fullmatch(a)}


# Subjects of mail about an application. Company domains outside trusted_senders() are read only
# when the subject matches, so a company you also buy from or log in to never has its other mail read.
JOB_SUBJECT = re.compile(r"appli(ed|cation|cant)|apply|candida|interview|assessment|assignment|test invit|"
                         r"position|role\b|opening|opportunit|career|recruit|hiring|shortlist|offer letter|"
                         r"next steps|your profile|job|talent|thank(s| you) for (applying|your (application|interest))",
                         re.I)


def allowlist() -> set[str]:
    """Domains (or full addresses) whose mail JobHelper may read (company ones subject-gated)."""
    configured = trusted_senders()
    derived: set[str] = set(_gated_domains)
    with db.session() as sess:
        for (host,) in sess.execute(select(db.SiteLogin.domain)).all():
            derived.add(registrable(host))
        for (dom,) in sess.execute(select(db.CompanySender.domain).where(db.CompanySender.domain.is_not(None))).all():
            derived.add(dom)
        for (url,) in sess.execute(select(db.Application.final_url).where(db.Application.final_url.is_not(None))).all():
            host = re.sub(r"^https?://", "", url or "").split("/")[0]
            if host:
                derived.add(registrable(host))
    return configured | {a for a in derived - NEVER_AUTO if SAFE_SENDER.fullmatch(a)}


def allow_domain_temporarily(domain: str, *, trusted: bool = False) -> str:
    """Allow `domain`'s mail while an apply run is on it: trusted (no subject gate) only for the
    job's own URLs, subject-gated for anything else the run reached. Returns what was added ("" if
    nothing); the run must hand it back to forget_temporary_domains when it ends."""
    dom = registrable(domain) if valid_domain(domain) else ""
    if not dom or dom in NEVER_AUTO:
        return ""
    (_temporary_domains if trusted else _gated_domains).add(dom)
    return dom


def forget_temporary_domains(domains: Iterable[str]) -> None:
    domains = set(domains)
    _temporary_domains.difference_update(domains)
    _gated_domains.difference_update(domains)


def sender_allowed(sender: str, allowed: Iterable[str]) -> bool:
    sender = sender.lower()
    dom = sender.rsplit("@", 1)[-1]
    for a in allowed:
        if "@" in a:
            if sender == a:
                return True
        elif dom == a or dom.endswith("." + a):
            return True
    return False


# ---------------------------------------------------------------- messages


@dataclass
class Mail:
    message_id: str
    sender: str
    sender_name: str
    subject: str
    date: datetime | None
    text: str
    html: str

    @property
    def sender_domain(self) -> str:
        return self.sender.rsplit("@", 1)[-1].lower() if "@" in self.sender else ""


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _parse(raw: bytes) -> Mail:
    msg = email.message_from_bytes(raw)
    text_parts, html_parts = [], []
    for part in msg.walk():
        # Attachments (resumes, .ics invites, PDFs) are never read.
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        (html_parts if part.get_content_type() == "text/html" else text_parts).append(body)
    name, addr = parseaddr(msg.get("From", ""))
    try:
        date = parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else None
    except (TypeError, ValueError):
        date = None
    html = "\n".join(html_parts)
    text = "\n".join(text_parts) or html_to_text(html, limit=40000)
    return Mail(message_id=(msg.get("Message-ID") or "").strip() or f"nomid-{hashlib.sha1(raw).hexdigest()}",
                sender=addr.lower(), sender_name=_decode(name), subject=_decode(msg.get("Subject")),
                date=date, text=text, html=html)


def _from_clause(senders: list[str]) -> str:
    return "(" + " OR ".join(f"from:{s}" for s in senders) + ")"


def _search_sync(senders: list[str], extra_query: str, limit: int, allowed: set[str],
                 trusted: set[str], gate: re.Pattern = JOB_SUBJECT) -> list[Mail]:
    sec = config.secrets()
    if not (sec.gmail_address and sec.gmail_app_password):
        raise MailNotConfigured("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set")
    conn = ReadOnlyIMAP("imap.gmail.com", 993, timeout=60)
    out: list[Mail] = []
    try:
        conn.login(sec.gmail_address, sec.gmail_app_password)
        conn.select('"[Gmail]/All Mail"')   # forced read-only (EXAMINE)
        ids: list[bytes] = []
        for i in range(0, len(senders), 20):   # keep each Gmail query a sane length
            q = f"{_from_clause(senders[i:i + 20])} {extra_query}".strip()
            typ, data = conn.search(None, "X-GM-RAW", f'"{q}"')
            if typ == "OK" and data and data[0]:
                ids += data[0].split()
        for i in sorted(set(ids), key=int, reverse=True)[:limit]:    # newest first
            # Look at sender and subject first; only open the message if they pass (rule 1).
            typ, hdr = conn.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
            if typ != "OK" or not hdr or not isinstance(hdr[0], tuple):
                continue
            head = email.message_from_bytes(hdr[0][1])
            _, addr = parseaddr(head.get("From", ""))
            if not sender_allowed(addr, allowed):
                continue
            if not sender_allowed(addr, trusted) and not gate.search(_decode(head.get("Subject"))):
                continue
            typ, msg_data = conn.fetch(i, "(BODY.PEEK[])")
            if typ == "OK" and msg_data and isinstance(msg_data[0], tuple):
                out.append(_parse(msg_data[0][1]))
        return out
    finally:
        try:
            conn.logout()
        except Exception:
            pass


async def search(senders: list[str], extra_query: str = "", limit: int = 50,
                 gate: re.Pattern = JOB_SUBJECT) -> list[Mail]:
    """Search mail from `senders` (each must be on the allowlist) plus a Gmail filter.

    `extra_query` may narrow the search (dates, keywords) but cannot widen it: the
    sender clause is always ANDed in, and From is re-checked before any body is read.
    """
    allowed = allowlist()
    senders = [_norm_domain(x) for x in senders if x]
    bad = [x for x in senders if not sender_allowed(x if "@" in x else f"x@{x}", allowed)]
    if bad:
        raise GuardrailViolation(f"senders not on the job-mail allowlist: {bad}")
    if not senders:
        raise GuardrailViolation("a mail search needs at least one allowlisted sender")
    if re.search(r"(^|\s)(OR|-|\{)\s*$|\bin:(spam|trash)\b", extra_query, re.I):
        raise GuardrailViolation("extra_query may only narrow the search")
    return await asyncio.to_thread(_search_sync, senders, extra_query, limit, allowed, trusted_senders(), gate)


async def search_job_mail(extra_query: str = "", limit: int = 80) -> list[Mail]:
    """All allowlisted job mail (platforms, ATS, companies applied to)."""
    return await search(sorted(allowlist()), extra_query, limit)


# ---------------------------------------------------------------- company sender domains

GENERIC_WORDS = {"india", "private", "limited", "technologies", "technology", "solutions", "services", "software",
                 "global", "group", "consulting", "systems", "digital", "the", "company", "corporation", "careers",
                 "jobs", "talent", "hiring", "partners", "center", "centre"}


def company_tokens(company: str) -> list[str]:
    """Distinctive words of a company name, letters and digits only (also safe inside a Gmail query)."""
    return [t for t in re.findall(r"[a-z0-9]+", (company or "").lower()) if len(t) >= 4 and t not in GENERIC_WORDS]


def company_domain(addr: str, company: str) -> str | None:
    """The sender's domain if it plainly belongs to `company` (its name is in the domain), else None."""
    dom = registrable(addr.rsplit("@", 1)[-1].lower()) if "@" in (addr or "") else ""
    if not dom or dom in NEVER_AUTO:
        return None
    label = dom.split(".")[0]
    toks = company_tokens(company)
    return dom if any(t in label or (len(label) >= 5 and label in t) for t in toks) else None


def _learn_sync(companies: list[str], days: int) -> dict[str, str]:
    sec = config.secrets()
    if not (sec.gmail_address and sec.gmail_app_password):
        raise MailNotConfigured("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set")
    conn = ReadOnlyIMAP("imap.gmail.com", 993, timeout=60)
    found: dict[str, str] = {}
    try:
        conn.login(sec.gmail_address, sec.gmail_app_password)
        conn.select('"[Gmail]/All Mail"')   # forced read-only (EXAMINE)
        for company in companies:
            toks = company_tokens(company)
            if not toks:
                continue
            typ, data = conn.search(None, "X-GM-RAW", f'"from:{max(toks, key=len)} newer_than:{days}d"')
            ids = data[0].split()[-15:] if typ == "OK" and data and data[0] else []
            for i in reversed(ids):          # newest first; headers only, never the body
                typ, hdr = conn.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
                if typ != "OK" or not hdr or not isinstance(hdr[0], tuple):
                    continue
                head = email.message_from_bytes(hdr[0][1])
                _, addr = parseaddr(head.get("From", ""))
                if not JOB_SUBJECT.search(_decode(head.get("Subject"))):
                    continue
                if (dom := company_domain(addr, company)):
                    found[company] = dom
                    break
        return found
    finally:
        try:
            conn.logout()
        except Exception:
            pass


async def learn_company_domains(companies: list[str], days: int = 60) -> dict[str, str]:
    """{company: email domain} for companies whose mail is in Gmail under their own domain."""
    return await asyncio.to_thread(_learn_sync, companies, days)


CODE_RE = re.compile(r"(?<![\d#])(\d{4,8})(?!\d)")
LINK_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
VERIFY_WORDS = re.compile(r"verif|confirm|activate|validate|one[- ]time|otp|code", re.I)
# Subject gate for a lookup on a career site JobHelper holds a login for (not the job's own domain):
# its sign-up / sign-in mail. Sites merely visited keep JOB_SUBJECT, so a page that sends the agent
# to github.com can't harvest "[GitHub] Please verify your device".
VERIFY_SUBJECT = re.compile(rf"{JOB_SUBJECT.pattern}|verif|code\b|\botp\b|confirm|activat|one[- ]time|"
                            r"sign[- ]?in|log[- ]?in", re.I)


async def latest_verification(site_domain: str, minutes: int = 20) -> dict[str, str] | None:
    """A verification code or link from `site_domain` (the career site being applied to).

    The caller decides the domain belongs to the current application and puts it on the
    allowlist (allow_domain_temporarily); this never widens the allowlist itself. Mail from a
    domain that isn't trusted is opened only with a job subject, or a verification subject when
    JobHelper holds a login there.
    """
    if not valid_domain(site_domain) or registrable(site_domain) in NEVER_AUTO:
        raise GuardrailViolation("verification lookups must name the career site's domain")
    dom = registrable(site_domain)
    with db.session() as sess:
        has_login = sess.get(db.SiteLogin, dom) is not None
    mails = await search([dom], "newer_than:1d", limit=10, gate=VERIFY_SUBJECT if has_login else JOB_SUBJECT)
    now = datetime.now(timezone.utc)
    for m in mails:
        if m.date and (now - m.date).total_seconds() > minutes * 60:
            continue
        body = m.text or ""
        code = None
        for line in body.splitlines():
            if VERIFY_WORDS.search(line) or len(line.strip()) <= 10:
                hit = CODE_RE.search(line)
                if hit:
                    code = hit.group(1)
                    break
        link = next((u for u in LINK_RE.findall(m.html or body) if VERIFY_WORDS.search(u)), None)
        if code or link:
            return {"from": m.sender, "subject": m.subject, "code": code or "", "link": link or "",
                    "received": m.date.isoformat() if m.date else ""}
    return None
