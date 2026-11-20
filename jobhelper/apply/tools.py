"""In-process MCP tools the apply agent gets besides the browser.

Each run gets its own RunContext so tools know which job they serve and can record the
outcome. Tools return plain text; anything secret (passwords) goes only to the agent.
OTPs and saved logins are scoped per run to that job's own sites (`RunContext.site`).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import yaml
from claude_agent_sdk import create_sdk_mcp_server, tool

from .. import config, kb, mail
from . import vault


@dataclass
class RunContext:
    application_id: int
    job: dict[str, Any]
    resume_path: Path
    dry_run: bool
    result: dict[str, Any] | None = None
    blocker: dict[str, Any] | None = None
    answers: list[dict[str, str]] = field(default_factory=list)
    domains: set[str] = field(default_factory=set)
    manual_sites: list[str] = field(default_factory=list)   # sites that block automated browsers
    resumed_from: str | None = None   # the blocker the previous run stopped on, when the user pressed Resume
    tab_ready: bool = False           # the agent opened (or selected) its own tab; set by the guard
    visited: set[str] = field(default_factory=set)       # registrable domains the guard let it open
    page_urls: Callable[[], list[str]] = field(default_factory=lambda: (lambda: []))  # this run's tabs, set by agent.run
    mail_domains: set[str] = field(default_factory=set)  # put on mail's temporary allowlist; removed when the run ends
    passwords: set[str] = field(default_factory=set)     # site passwords handed out; masked in the transcript

    def job_domains(self) -> set[str]:
        """Registrable domains of the job's own URLs (url / apply_url): the only temporarily trusted mail."""
        return {mail.registrable(h) for u in (self.job.get("url"), self.job.get("apply_url"))
                if (h := urlparse(str(u or "")).hostname)}

    def own_url(self, url: Any) -> str:
        """`url` if it is on one of this run's sites, else the job's own apply URL. The agent's reports
        feed mail allowlisting and manual_sites, and a page can make it name any URL."""
        u = str(url or "").strip()
        if not u or self.site(urlparse(u).hostname if "://" in u else ""):
            return u
        return str(self.job.get("apply_url") or self.job.get("url") or "")

    def site(self, domain: Any, *, mail_senders: bool = False) -> str | None:
        """`domain` as a registrable domain if it belongs to this run, else None.

        This run's sites: the job's URLs, pages the agent opened (or is on) and logins it created.
        A page can tell the agent to name any domain, so OTPs and saved passwords go only to these.
        """
        d = str(domain or "")
        if not mail.valid_domain(d) or (d := mail.registrable(d)) in mail.NEVER_AUTO:
            return None
        urls = [self.job.get("url"), self.job.get("apply_url"), *self.page_urls()]
        ok = self.visited | self.domains | {mail.registrable(h) for u in urls if (h := urlparse(str(u or "")).hostname)}
        if mail_senders:   # job platforms / ATS from settings (Workday mails from myworkday.com, not the site)
            ok |= {a for a in mail.trusted_senders() if "@" not in a}
        return d if d in ok else None


def _text(s: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": s}]}


def _not_this_run(domain: Any) -> dict[str, Any]:
    return _text(f"Refused: {str(domain)[:100]!r} is not this job's site (only the job's own URLs and pages you "
                 "opened in this run count). Don't retry with another domain; report a blocker if you are stuck.")


def build_server(ctx: RunContext):
    @tool("get_job", "The job you are applying to: title, company, URLs, description, scoring notes.", {})
    async def get_job(args):
        return _text(json.dumps(ctx.job, ensure_ascii=False, default=str))

    @tool("get_profile", "The candidate's structured facts (contact, CTC, notice period, education, "
          "preferences, ready-made answers). A null value means: do not answer, report a blocker.", {})
    async def get_profile(args):
        return _text(yaml.safe_dump(config.profile_for_forms(), sort_keys=False, allow_unicode=True))

    @tool("kb_search", "Search the candidate's knowledge base (resume, experience, projects, notes) "
          "for facts to answer a question. Returns the best matching passages.", {"query": str})
    async def kb_search(args):
        hits = kb.search(args["query"], limit=5)
        if not hits:
            return _text("No matching passages. Do not invent an answer.")
        return _text("\n\n---\n\n".join(f"[{h['title']}]\n{h['text']}" for h in hits))

    @tool("get_resume", "Absolute path of the resume PDF to upload for this job. Upload only this file.", {})
    async def get_resume(args):
        return _text(str(ctx.resume_path))

    @tool("get_site_login", "Existing login for a career-site domain, if JobHelper created one before.",
          {"domain": str})
    async def get_site_login(args):
        if not (dom := ctx.site(args.get("domain"))):
            return _not_this_run(args.get("domain"))
        cred = vault.get(dom)
        if cred:
            ctx.passwords.add(cred["password"])
        return _text(json.dumps(cred) if cred else "No saved login for this domain.")

    @tool("create_site_login", "Create (and save) a login for a career site that requires an account. "
          "Returns the email to use as username and a unique strong password.", {"domain": str})
    async def create_site_login(args):
        if not (dom := ctx.site(args.get("domain"))):
            return _not_this_run(args.get("domain"))
        email = (config.profile().get("email") or config.secrets().gmail_address)
        cred = vault.create(dom, email, note=f"application {ctx.application_id}")
        ctx.domains.add(cred["domain"])
        ctx.passwords.add(cred["password"])
        ctx.mail_domains.add(mail.allow_domain_temporarily(cred["domain"]))
        return _text(json.dumps(cred))

    @tool("fetch_email_code", "Get a verification code or link that the career site just emailed. "
          "Only reads mail sent from that site's own domain. Wait ~30s after triggering the email.",
          {"site_domain": str})
    async def fetch_email_code(args):
        if not (dom := ctx.site(args.get("site_domain"), mail_senders=True)):
            return _not_this_run(args.get("site_domain"))
        ctx.mail_domains.add(mail.allow_domain_temporarily(dom, trusted=dom in ctx.job_domains()))
        try:
            got = await mail.latest_verification(dom)
        except (mail.MailNotConfigured, mail.GuardrailViolation) as exc:
            return _text(f"Unavailable: {exc}. Report a blocker.")
        return _text(json.dumps(got) if got else "Nothing yet. Wait 30 seconds and try once more, then report a blocker.")

    @tool("record_answer", "Log a question you answered on the form and where the answer came from "
          "(profile, kb, or posting). Call it for every screening question.",
          {"question": str, "answer": str, "source": str})
    async def record_answer(args):
        ctx.answers.append({k: str(args.get(k, ""))[:1000] for k in ("question", "answer", "source")})
        return _text("ok")

    @tool("report_blocker", "Stop: something needs the human (CAPTCHA, phone OTP, a question you must not "
          "guess, an assessment, a broken page). Describe exactly what and where. For a question only the "
          "candidate can answer, also give `question` (its exact text) and `options` (the choices, if it has any).",
          {"type": "object", "properties": {
              "kind": {"type": "string"}, "details": {"type": "string"}, "url": {"type": "string"},
              "question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}},
           "required": ["kind", "details"]})
    async def report_blocker(args):
        ctx.blocker = {k: args.get(k, "") for k in ("kind", "details", "url")}
        ctx.blocker["url"] = ctx.own_url(ctx.blocker["url"])
        if str(args.get("question") or "").strip():
            ctx.blocker["question"] = str(args["question"]).strip()[:500]
            ctx.blocker["options"] = [str(o)[:100] for o in (args.get("options") or []) if str(o).strip()][:12]
        return _text("Blocker recorded. Stop now and end your turn with a one-line summary.")

    @tool("report_result", "Finish the run. status is one of: applied, ready_to_submit (dry run), "
          "already_applied, not_eligible, failed. evidence = the confirmation text you saw.",
          {"status": str, "evidence": str, "final_url": str, "notes": str})
    async def report_result(args):
        ctx.result = {k: args.get(k, "") for k in ("status", "evidence", "final_url", "notes")}
        ctx.result["final_url"] = ctx.own_url(ctx.result["final_url"])
        return _text("Result recorded. End your turn now.")

    return create_sdk_mcp_server("jobhelper", tools=[
        get_job, get_profile, kb_search, get_resume, get_site_login, create_site_login,
        fetch_email_code, record_answer, report_blocker, report_result,
    ])
