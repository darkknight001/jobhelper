"""Harness verifier: deterministic test suite + live config/DB audits + optional LLM evals.

`python -m jobhelper verify` (add --llm to run scoring/triage evals on your subscription).
The `harness-verifier` Claude Code agent (.claude/agents/) drives this and reviews the rest.
"""
from __future__ import annotations

import asyncio
import re
import socket
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from typing import Any

import yaml
from sqlalchemy import select

from . import config, db, kb, mail, skills

FIXTURES = config.ROOT / "tests" / "fixtures"


def _check(name: str, ok: bool, detail: str = "", *, severity: str = "error") -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail, "severity": "info" if ok else severity}


# ------------------------------------------------------------------ 1. test suite

def run_tests() -> dict[str, Any]:
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(config.ROOT / "tests")],
                          cwd=str(config.ROOT), capture_output=True, text=True, timeout=600)
    tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-15:])
    return _check("pytest suite (guardrails, caps, parsers, signing)", proc.returncode == 0, tail)


# ------------------------------------------------------------------ 2. static guardrail scan

def static_scan() -> list[dict[str, Any]]:
    pkg = config.ROOT / "jobhelper"
    out = []
    smtp, imap_users, mutating = [], [], []
    for f in pkg.rglob("*.py"):
        src = f.read_text(encoding="utf-8")
        rel = f.relative_to(config.ROOT).as_posix()
        if re.search(r"\bsmtplib\b|\bSMTP\(|sendmail\(|send_message\(", src):
            smtp.append(rel)
        if re.search(r"^\s*(import\s+imaplib|from\s+imaplib\s+import)", src, re.M) and f.name != "mail.py":
            imap_users.append(rel)
        if f.name == "mail.py":
            hits = re.findall(r"\bconn\.(store|expunge|copy|move|append|delete|uid|create|rename|close)\(", src)
            if hits:
                mutating.append(f"{rel}: {sorted(set(hits))}")
    out.append(_check("no code can send email (no SMTP)", not smtp, ", ".join(smtp)))
    out.append(_check("mail.py is the only IMAP user", not imap_users, ", ".join(imap_users)))
    out.append(_check("mail.py never calls a mutating IMAP method", not mutating, ", ".join(mutating)))
    agent_src = (pkg / "apply" / "agent.py").read_text(encoding="utf-8")
    out.append(_check("apply agent has no built-in tools (tools=[])", "tools=[]," in agent_src))
    out.append(_check("apply agent runs in dontAsk permission mode", 'permission_mode="dontAsk"' in agent_src))
    from .apply import agent
    risky = {t for t in agent.BROWSER_TOOLS if re.search(r"evaluate|run_code|cookie|storage|route|install|close", t)}
    out.append(_check("browser tool allowlist excludes JS/cookie/storage tools", not risky, ", ".join(sorted(risky))))
    return out


# ------------------------------------------------------------------ 3. live configuration

def config_checks() -> list[dict[str, Any]]:
    s = config.settings()
    out = []
    sc, ap = s["scoring"], s["apply"]
    out.append(_check("review_min < auto_apply_min", sc["review_min"] < sc["auto_apply_min"],
                      f"{sc['review_min']} vs {sc['auto_apply_min']}"))
    out.append(_check("daily cap per portal ≤ 10 (your rule)", ap["daily_cap_per_portal"] <= 10,
                      str(ap["daily_cap_per_portal"]), severity="warn"))
    out.append(_check("dry run status", True, "ON (nothing is submitted)" if ap.get("dry_run", True) else "OFF (live submits)"))
    missing = [n for n in ("job-fit-scoring", "job-application-filling", "recruiter-email-triage", "interview-prep-brief")
               if n not in skills.names()]
    out.append(_check("all four skills present", not missing, ", ".join(missing)))
    resumes = list(config.RESUME_DIR.glob("*.pdf"))
    out.append(_check("at least one resume PDF", bool(resumes), ", ".join(p.name for p in resumes)))
    docs = {d["file"] for d in kb.list_docs()}
    out.append(_check("KB has resume.md", "resume.md" in docs))
    out.append(_check("KB has the claude.ai export", "claude-export.md" in docs,
                      "import it via Knowledge → Import", severity="warn"))
    from .web.app import _missing_profile_fields
    gaps = _missing_profile_fields(config.profile())
    out.append(_check("profile fields the agent needs are filled", not gaps, ", ".join(gaps), severity="warn"))
    sec = config.secrets()
    for name, val, sev in (("CLAUDE_CODE_OAUTH_TOKEN", sec.claude_oauth_token, "error"),
                           ("JOBHELPER_DASHBOARD_PASSWORD", sec.dashboard_password, "error"),
                           ("JOBHELPER_API_TOKEN", sec.api_token, "error"),
                           ("JOBHELPER_VAULT_KEY", sec.vault_key, "error"),
                           ("GMAIL_APP_PASSWORD", sec.gmail_app_password, "warn"),
                           ("HERMES_WEBHOOK_SECRET", sec.hermes_webhook_secret, "warn")):
        out.append(_check(f"secret {name} set", bool(val), "", severity=sev))
    if not sec.session_secret_set:
        out.append(_check("JOBHELPER_SESSION_SECRET set", False, "unset: random per start, logins reset on restart"))
    out.append(_check("secret JOBHELPER_VNC_PASSWORD set (remote browser locked)", bool(sec.vnc_password),
                      "", severity="warn"))
    host = sec.bind.split(":")[0]
    if sec.public_url.startswith("https") and host not in ("127.0.0.1", "localhost") and not (
            sec.in_container and host == "0.0.0.0"):
        out.append(_check("dashboard bound to localhost behind Caddy", False, sec.bind))
    out.append(_compose_isolation())
    return out


def _compose_isolation() -> dict[str, Any]:
    """The container must not share the host network, and must publish only on loopback."""
    f = config.ROOT / "compose.yaml"
    if not f.exists():
        return _check("compose isolation", True, "compose.yaml not present here")
    svc = (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("services", {}).get("jobhelper", {})
    problems = []
    if svc.get("network_mode") == "host":
        problems.append("network_mode: host exposes Chromium's control port to the whole server")
    for p in svc.get("ports") or []:
        if not str(p).startswith("127.0.0.1:"):
            problems.append(f"port {p} is not bound to 127.0.0.1")
    return _check("browser isolated in its own network; ports on loopback only", not problems, "; ".join(problems))


# ------------------------------------------------------------------ 4. audits of real data

def audits() -> list[dict[str, Any]]:
    out = []
    tzinfo = config.tz()
    cap = config.settings()["apply"]["daily_cap_per_portal"]
    allowed = mail.allowlist()
    with db.session() as s:
        per_day: dict[tuple[str, str], int] = defaultdict(int)
        no_evidence, ungrounded = [], []
        for a in s.scalars(select(db.Application).where(db.Application.applied_at.is_not(None))):
            per_day[(a.portal, a.applied_at.astimezone(tzinfo).date().isoformat())] += 1
            if a.status == "applied" and not (a.notes or "").strip():
                no_evidence.append(a.id)
        for a in s.scalars(select(db.Application).where(db.Application.answers != [])):
            for ans in a.answers or []:
                if ans.get("source") not in ("profile", "kb", "posting"):
                    ungrounded.append(f"#{a.id}: {ans.get('question', '')[:60]} (source={ans.get('source')!r})")
        over = {f"{p} {d}": n for (p, d), n in per_day.items() if n > cap}
        out.append(_check("no portal ever exceeded its daily cap", not over, str(over)))
        out.append(_check("every applied application has confirmation evidence", not no_evidence,
                          f"applications {no_evidence[:10]}", severity="warn"))
        out.append(_check("every recorded answer cites profile/kb/posting", not ungrounded,
                          "; ".join(ungrounded[:8]), severity="warn"))
        outside = [r.sender for r in s.scalars(select(db.EmailSeen)) if r.sender and not mail.sender_allowed(r.sender, allowed)]
        out.append(_check("only allowlisted senders were ever processed", not outside, ", ".join(sorted(set(outside))[:10])))
        bad_auto = []
        for a, j in s.execute(select(db.Application, db.Job).join(db.Job).where(db.Application.mode == "auto")).all():
            d = j.score_detail or {}
            if (j.score or 0) < config.settings()["scoring"]["auto_apply_min"] or d.get("red_flags") or d.get("location_ok") is False:
                bad_auto.append(a.id)
        out.append(_check("auto-applies all met the threshold with no red flags", not bad_auto,
                          f"applications {bad_auto[:10]}", severity="warn"))
    # Transcript scan: denied tool calls are fine (the guard worked) but worth knowing about.
    attempts = []
    for t in config.ARTIFACT_DIR.glob("app-*/transcript.jsonl"):
        for line in t.read_text(encoding="utf-8", errors="replace").splitlines():
            if re.search(r"mail\.google|gmail\.com/|outlook\.(live|office)|browser_evaluate|browser_cookie|run_code", line):
                attempts.append(f"{t.parent.name}: {line[:120]}")
    out.append(_check("no apply agent tried webmail / JS / cookie tools", not attempts, "; ".join(attempts[:5]),
                      severity="warn"))
    return out


# ------------------------------------------------------------------ 5. LLM evals (opt-in)

async def llm_evals() -> list[dict[str, Any]]:
    from . import llm, scoring, tracking
    out = []
    cases = yaml.safe_load((FIXTURES / "scoring_cases.yaml").read_text(encoding="utf-8"))
    blocks = []
    for i, c in enumerate(cases, start=1):
        blocks.append(f"<job id=\"{i}\">\nTitle: {c['title']}\nCompany: {c['company']}\nLocation: {c['location']}\n"
                      f"Experience: {c.get('experience', 'unknown')}\n\n{c['description']}\n</job>")
    resume_files = sorted(p.name for p in config.RESUME_DIR.glob("*.pdf"))
    try:
        res = await llm.structured("Score each job below for this candidate. Return one result per job id.\n\n"
                                   + "\n\n".join(blocks), scoring._schema(resume_files),
                                   system=scoring.system_prompt(), model=config.settings()["llm"]["scoring_model"],
                                   task="health")
    except Exception as exc:
        return [_check("scoring eval ran", False, str(exc))]
    by_id = {r["job_id"]: r for r in res.get("results", [])}
    passed, rows = 0, []
    for i, c in enumerate(cases, start=1):
        r = by_id.get(i)
        lo, hi = c["expect"]["min"], c["expect"]["max"]
        ok = bool(r) and lo <= r["score"] <= hi
        if "location_ok" in c["expect"] and r:
            ok = ok and r["location_ok"] == c["expect"]["location_ok"]
        passed += ok
        rows.append(f"{'✓' if ok else '✗'} {c['id']}: {r['score'] if r else '—'} (want {lo}-{hi})")
    scores = [r["score"] for r in by_id.values()]
    spread = (max(scores) - min(scores)) if scores else 0
    out.append(_check(f"scoring calibration {passed}/{len(cases)}", passed >= len(cases) - 1, "\n".join(rows)))
    out.append(_check("scores discriminate (spread ≥ 40)", spread >= 40, f"spread {spread}"))

    emails = yaml.safe_load((FIXTURES / "emails.yaml").read_text(encoding="utf-8"))
    msgs = "\n\n".join(f"<message index=\"{i}\">\nFrom: {e['from']}\nSubject: {e['subject']}\n\n{e['body']}\n</message>"
                       for i, e in enumerate(emails))
    try:
        res = await llm.structured(f"<applications>\n[]\n</applications>\n\n{msgs}", tracking.SCHEMA,
                                   system=tracking._system(), model=config.settings()["llm"]["email_model"], effort="low",
                                   task="health")
    except Exception as exc:
        return out + [_check("triage eval ran", False, str(exc))]
    got = {r["message_index"]: r for r in res.get("results", [])}
    passed, rows = 0, []
    for i, e in enumerate(emails):
        r = got.get(i)
        ok = bool(r) and r["kind"] in e["expect"]["kind"] and r["action_required"] == e["expect"]["action_required"]
        passed += ok
        rows.append(f"{'✓' if ok else '✗'} {e['id']}: {r['kind'] if r else '—'} (want {e['expect']['kind']})")
    out.append(_check(f"email triage {passed}/{len(emails)}", passed >= len(emails) - 1, "\n".join(rows)))
    return out


# ------------------------------------------------------------------ doctor (connectivity)

async def doctor() -> dict[str, Any]:
    from . import llm
    out: dict[str, Any] = {}
    try:
        out["claude"] = await llm.check()
    except Exception as exc:
        out["claude"] = f"FAILED: {exc}"
    sec = config.secrets()
    if sec.gmail_app_password:
        def _imap():
            conn = mail.ReadOnlyIMAP("imap.gmail.com", 993, timeout=30)
            conn.login(sec.gmail_address, sec.gmail_app_password)
            conn.select("INBOX")
            conn.logout()
            return "ok (read-only login)"
        try:
            out["gmail"] = await asyncio.to_thread(_imap)
        except Exception as exc:
            out["gmail"] = f"FAILED: {exc}"
    else:
        out["gmail"] = "not configured"
    url = sec.hermes_webhook_url
    if url:
        m = re.match(r"https?://([^:/]+):?(\d+)?", url)
        host, port = m.group(1), int(m.group(2) or 80)
        try:
            with socket.create_connection((host, port), timeout=5):
                out["hermes"] = f"port {host}:{port} reachable (use `notify-test` to send a message)"
        except OSError as exc:
            out["hermes"] = f"FAILED: {exc}"
    else:
        out["hermes"] = "not configured"
    import shutil
    out["npx"] = shutil.which("npx") or "MISSING (needed for Playwright MCP)"
    return out


# ------------------------------------------------------------------ entry

async def run(*, llm: bool = False) -> dict[str, Any]:
    checks = [run_tests(), *static_scan(), *config_checks(), *audits()]
    if llm:
        checks += await llm_evals()
    errors = [c for c in checks if not c["ok"] and c["severity"] == "error"]
    warns = [c for c in checks if not c["ok"] and c["severity"] == "warn"]
    return {"ok": not errors, "at": datetime.now(config.tz()).isoformat(timespec="seconds"),
            "errors": len(errors), "warnings": len(warns), "checks": checks}


def render(report: dict[str, Any]) -> str:
    lines = [f"JobHelper verify · {report['at']} · {'PASS' if report['ok'] else 'FAIL'} "
             f"({report['errors']} errors, {report['warnings']} warnings)"]
    for c in report["checks"]:
        mark = "✓" if c["ok"] else ("✗" if c["severity"] == "error" else "!")
        lines.append(f" {mark} {c['name']}" + (f"\n     {c['detail'].replace(chr(10), chr(10) + '     ')}" if c["detail"] and (not c["ok"] or c["name"].startswith(("dry run", "scoring", "email triage"))) else ""))
    return "\n".join(lines)
