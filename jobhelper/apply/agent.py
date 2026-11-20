"""One application = one Claude Agent SDK run driving the shared browser.

Tools: Playwright MCP (attached over CDP to the shared Chromium) + JobHelper's own
in-process tools. Guardrails are PreToolUse hooks, so they hold whatever the model decides:
no webmail / account / internal-address / non-web URLs, no portal messaging or profile pages,
no final submit in dry run, and passwords are masked in the transcript.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, HookMatcher, ResultMessage, TextBlock, query

from .. import config, kb, llm, mail, skills, usage
from ..browser import BROWSER, child_env, secret_env_names
from .tools import RunContext, build_server

log = logging.getLogger(__name__)

PLAYWRIGHT_MCP = "@playwright/mcp@0.0.83"

# Tabs left open for the user (needs_user / filled for review): application id -> the tabs its run
# opened. The next application works in a new tab; Resume continues in these. In memory only: a
# restart closes the browser anyway.
PARKED: dict[int, list[Any]] = {}


async def release_tabs(app_id: int) -> None:
    for p in PARKED.pop(app_id, []):
        with contextlib.suppress(Exception):
            await p.close()


def release_tabs_soon(app_id: int) -> None:
    """For sync callers (skip, restart): close the application's parked tabs in the background."""
    try:
        asyncio.get_running_loop().create_task(release_tabs(app_id))
    except RuntimeError:
        PARKED.pop(app_id, None)

BROWSER_TOOLS = ["browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_click",
                 "browser_type", "browser_fill_form", "browser_select_option", "browser_file_upload",
                 "browser_press_key", "browser_hover", "browser_handle_dialog", "browser_tabs",
                 "browser_take_screenshot", "browser_wait_for", "browser_find"]
JOBHELPER_TOOLS = ["get_job", "get_profile", "kb_search", "get_resume", "get_site_login", "create_site_login",
                   "fetch_email_code", "record_answer", "report_blocker", "report_result"]

WEBMAIL_HOSTS = ("mail.google.com", "gmail.com", "inbox.google.com", "outlook.live.com", "outlook.office.com",
                 "outlook.office365.com", "mail.yahoo.com", "proton.me", "protonmail.com", "mail.zoho.com",
                 "mail.zoho.in", "mail.rediff.com")


def _host(url: str) -> str:
    return (urlparse(url if "://" in url else f"https://{url}").hostname or "").lower()


def is_webmail(url: str) -> bool:
    host = _host(url)
    return any(host == h or host.endswith("." + h) for h in WEBMAIL_HOSTS) or (
        host.endswith("zoho.com") and "/mail" in url.lower())


LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


def is_internal(url: str) -> bool:
    """True unless `url` is a public http(s) site: other schemes (file:, javascript:, data:, chrome:...),
    localhost, LAN names, and loopback / private / link-local / CGNAT addresses in any spelling."""
    p = urlparse(url.strip())
    if p.scheme not in ("http", "https") or "\\" in p.netloc:
        return True
    host = unquote(p.hostname or "").rstrip(".")
    if not host.isascii():
        try:
            host = host.encode("idna").decode()   # fullwidth digits/dots become ASCII, as in the browser
        except UnicodeError:
            return True
    if not host or "." not in host or host == "localhost" or host.endswith(LOCAL_SUFFIXES):
        return True
    # Browsers read a host whose last label is a number as IPv4 (127.1, 0x7f.0.0.1, 2130706433).
    if re.fullmatch(r"\d+|0x[0-9a-f]*", host.rsplit(".", 1)[-1]):
        try:
            return not ipaddress.ip_address(host).is_global
        except ValueError:
            return True
    return False


# The user decided (2026-11-09) never to put a Google session in the agent's browser and
# never to use social sign-in. Account / identity / personal-data pages are off limits;
# Google Forms stay allowed because some companies take applications through them.
ACCOUNT_HOSTS = ("accounts.google.com", "myaccount.google.com", "drive.google.com", "photos.google.com",
                 "pay.google.com", "payments.google.com", "takeout.google.com", "admin.google.com",
                 "contacts.google.com", "calendar.google.com", "chat.google.com", "keep.google.com",
                 "passwords.google.com", "appleid.apple.com", "account.apple.com", "login.live.com",
                 "account.microsoft.com", "login.microsoftonline.com")
SOCIAL_SIGNIN = re.compile(r"(sign|log)\s*(in|up|on)\s*(with|using|via)\s*(google|apple|microsoft|gmail)|"
                           r"continue\s*(with|using)\s*(google|apple|microsoft|gmail)|google\s+sign[- ]?in", re.I)


def is_account_page(url: str) -> bool:
    host = _host(url)
    if host == "docs.google.com":
        return "/forms/" not in url
    return any(host == h or host.endswith("." + h) for h in ACCOUNT_HOSTS)


# The browser is logged in to LinkedIn and Naukri, so a page could steer the agent into messaging people,
# posting or editing the profile. Applying needs none of these areas: job pages, search and Easy Apply stay open.
PORTAL_OFF_LIMITS = {
    "linkedin.com": re.compile(r"/(messaging|mynetwork|feed|post|notifications|settings|psettings|mypreferences)(/|$)|"
                               r"^/in/[^/]+/(edit|overlay)(/|$)", re.I),
    "naukri.com": re.compile(r"^/(mnjuser/(profile|inbox|homepage)|mailers)(/|$)|setting", re.I),
}
# Social actions on those portals; never part of an application. Denied in dry run ("Follow company"
# checkboxes in Easy Apply can still be unticked).
PORTAL_SOCIAL_CLICK = re.compile(r"^\W*(send|post|repost|message|connect|follow|comment)\b(?!.*\b(checkbox|untick|uncheck))",
                                 re.I)


def is_portal_off_limits(url: str) -> bool:
    p = urlparse(url if "://" in url else f"https://{url}")
    host = (p.hostname or "").lower()
    return any((host == d or host.endswith("." + d)) and rx.search(p.path or "/") for d, rx in PORTAL_OFF_LIMITS.items())


# Buttons that finalise an application. Denied in dry-run mode. On Naukri the plain
# "Apply" button (and the questionnaire's "Save") submits immediately.
FINAL_SUBMIT = re.compile(r"submit|send application|finish|complete application|confirm and apply|"
                          r"review and submit|i'?m interested", re.I)
FINAL_SUBMIT_BY_PORTAL = {
    "naukri": re.compile(r"^\s*(apply|save)(\s+(button|now))?\s*$", re.I),
}
# "Submit" buttons that belong to sign-in / sign-up / verification forms, not the application.
# Workday, for one, labels its sign-in button "Submit". The agent is told to name such buttons
# for what they do (e.g. "Sign in (Submit) button").
AUTH_STEP = re.compile(r"sign[\s-]?in|log[\s-]?in|create (an )?account|sign[\s-]?up|register|verif|"
                       r"password|one[\s-]?time|\botp\b|security code|email code", re.I)
# Buttons like "I'm interested" or "Apply" that only OPEN the application form on a company
# career site (Zoho Recruit, Darwinbox...). On naukri.com itself the same words submit, so they
# stay blocked unless the agent describes them as opening the form.
OPENS_FORM = re.compile(r"opens?\b.*\b(form|application)|start(s|ing)? (the |an )?application|"
                        r"to (the |an )?(application )?form|begin(s)? (the )?application", re.I)

PORTAL_NOTES = {
    "naukri": """Naukri: the job page has an "Apply" button (one click may submit directly) or
"Apply on company site" (follow it, then you're on a company site). After Apply, a chatbot
drawer may ask recruiter questions one at a time: answer each from profile/KB, record_answer,
then "Save". Success shows "Applied" / "You have successfully applied".""",
    "linkedin": """LinkedIn: "Easy Apply" opens a multi-step modal (contact info, resume, questions,
review). Use the resume already selected if it's the same file; otherwise upload get_resume.
Untick "Follow company". The final button is "Submit application". If the button says "Apply"
(not Easy Apply), it goes to a company site: follow it.""",
    "indeed": """Indeed: "Apply now" opens Indeed Apply (multi-step) or "Apply on company site".
The final button is "Submit your application". Indeed may show a Cloudflare check: that's a blocker.""",
    "company": """Company career site / ATS (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, iCIMS,
Darwinbox, Keka, Zoho Recruit...): the form is usually one page (Greenhouse/Lever/Ashby) or a
multi-step wizard with an account (Workday and most Indian ATSs). Look for an existing
account first; create one only if required.""",
}


def system_prompt(ctx: RunContext, portal: str) -> str:
    mode = ("DRY RUN: fill everything but do NOT click the final submit/apply button. When the "
            "application is ready, call report_result(status='ready_to_submit') with what you filled. "
            "Buttons that only sign in, create the account or verify a code are allowed even if labelled "
            "'Submit': describe them for what they do in `element` (e.g. 'Sign in (Submit) button')."
            if ctx.dry_run else
            "LIVE: submit the application when every field is correct, then confirm success on the page.")
    return f"""{skills.body("job-application-filling")}

## This run
You are the JobHelper apply agent driving a real, already-logged-in Chrome through the
browser tools. You apply to exactly ONE job: call get_job first. Mode: {mode}

{PORTAL_NOTES.get(portal, PORTAL_NOTES['company'])}

{('## Claims policy (overrides everything else)' + chr(10) + kb.claims_policy()) if kb.claims_policy() else ''}

Working rules for the browser tools:
- Actions do NOT return the page. Call browser_snapshot after each navigation or page change,
  when you need fresh element refs, and for the field check before Next/Submit. Don't snapshot
  after every small action: snapshots are large and this runs on a shared usage budget.
- Prefer browser_fill_form to fill several fields in one call.
- Tabs: other tabs in this browser hold other applications waiting for the user. Start by opening
  your own tab, browser_tabs(action='new', url=<start URL>), and work only there (plus tabs the site
  opens for this application). Never select, type in or navigate another application's tab, and
  don't close tabs: JobHelper does that.
- A question only the candidate can answer (no grounded answer in the profile or KB):
  report_blocker(kind='unanswerable_question', question=<its exact text>, options=<its choices, if a
  dropdown / radio / yes-no>, url=<page URL>, details=<where you are and what's already done>) and leave
  the page as it is: the user's answer comes back to you there.
- Call record_answer for every screening question you answer.
- Finish with exactly one call to report_result or report_blocker, then stop.
- Never visit webmail; use fetch_email_code for verification mail.
- Human checks: if a page shows a Cloudflare / Turnstile "verify you are human" check, an
  hCaptcha or reCAPTCHA, stop at once (don't try to solve it, and don't fill the form first):
  report_blocker(kind='bot_check', details=<what you saw>, url=<that page's URL>). The user
  will apply in their own browser.
- These sites are known to block this browser: {', '.join(ctx.manual_sites) or 'none yet'}. If
  the apply link lands you on one, report_blocker(kind='bot_check', ...) with its URL straight away.
- Never use "Sign in with Google/Apple/Microsoft". LinkedIn sign-in is fine (the browser is
  already logged in to LinkedIn); otherwise use email + password."""


def _deny(reason: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def make_guard(ctx: RunContext, transcript: Path, portal: str):
    resume_dir = config.RESUME_DIR.resolve()
    portal_final = FINAL_SUBMIT_BY_PORTAL.get(portal)

    async def guard(input_data, tool_use_id, context):
        name = input_data.get("tool_name", "")
        args = input_data.get("tool_input") or {}
        line = json.dumps({"tool": name, "input": args}, ensure_ascii=False, default=str)
        for pw in ctx.passwords:   # site passwords typed into forms stay out of the transcript
            line = line.replace(json.dumps(pw, ensure_ascii=False)[1:-1], "***")
        with open(transcript, "a", encoding="utf-8") as f:
            f.write(line[:4000] + "\n")
        short = name.split("__")[-1]
        if name.startswith("mcp__browser__") and short not in BROWSER_TOOLS:
            return _deny(f"{short} is not allowed for the apply agent")
        if short == "browser_tabs":
            action, url = str(args.get("action", "")), str(args.get("url") or "")
            if action == "close":
                return _deny("Don't close tabs; JobHelper closes them when the application is done.")
            if action == "new" and url and (is_webmail(url) or is_account_page(url) or is_internal(url)
                                            or is_portal_off_limits(url)):
                return _deny("That page is off limits (only public http(s) career sites).")
            if action in ("new", "select"):
                ctx.tab_ready = True
        elif name.startswith("mcp__browser__") and not ctx.tab_ready:
            return _deny("First open your own tab: browser_tabs(action='new', url=<start URL>). On a resumed "
                         "attempt, browser_tabs(action='list') and then action='select' this application's "
                         "tab. Other tabs hold other applications waiting for the user.")
        if short == "browser_navigate" and is_internal(str(args.get("url", ""))):
            return _deny("Only public http(s) sites are allowed: no local, private-network or non-web URLs.")
        if short == "browser_navigate" and is_webmail(str(args.get("url", ""))):
            return _deny("Webmail is off limits. Use fetch_email_code for verification mail.")
        if short == "browser_navigate" and is_account_page(str(args.get("url", ""))):
            return _deny("Google/Apple/Microsoft account pages are off limits. Use email sign-up on the career "
                         "site; if it only offers social sign-in, report_blocker(kind='sso_only').")
        if short == "browser_navigate" and is_portal_off_limits(str(args.get("url", ""))):
            return _deny("LinkedIn / Naukri messaging, network, feed, profile and settings pages are off limits; "
                         "only job pages, search and the application itself.")
        if short in ("browser_click", "browser_hover") and SOCIAL_SIGNIN.search(str(args.get("element", ""))):
            return _deny("Social sign-in is not allowed. Use email + password (create_site_login); if the site "
                         "offers nothing else, report_blocker(kind='sso_only').")
        if short == "browser_file_upload":
            allowed = ctx.resume_path.resolve()
            for p in args.get("paths") or []:
                try:
                    rp = Path(p).resolve()
                    ok = rp == allowed or rp.is_relative_to(resume_dir)
                except Exception:
                    ok = False
                if not ok:
                    return _deny(f"Only the resume returned by get_resume may be uploaded: {allowed}")
        if ctx.dry_run:
            label = str(args.get("element", "")).strip()
            is_final = FINAL_SUBMIT.search(label) or (portal_final and portal_final.search(label))
            if short == "browser_click" and is_final and not AUTH_STEP.search(label) and not OPENS_FORM.search(label):
                return _deny("Dry run: the final application submit is disabled. If this button only signs you "
                             "in, creates the account, verifies a code, or OPENS the application form (e.g. "
                             "\"I'm interested\" on a company careers site, not on naukri.com), click it again "
                             "describing it that way (e.g. 'Sign in (Submit) button', \"I'm interested (opens the "
                             "application form) button\"). Otherwise call report_result(status='ready_to_submit').")
            if short == "browser_click" and portal in ("linkedin", "naukri") and PORTAL_SOCIAL_CLICK.search(label):
                return _deny("Dry run: sending, posting, messaging, connecting and following are not part of applying.")
            if short == "browser_type" and args.get("submit"):
                return _deny("Dry run: don't submit forms with Enter.")
            values = ([args.get("text")] if short == "browser_type" else
                      [f.get("value") for f in (args.get("fields") or []) if isinstance(f, dict)])
            if short in ("browser_type", "browser_fill_form") and any(ch in str(v or "") for v in values for ch in "\r\n"):
                return _deny("Dry run: a line break can submit the form. Type the value on one line.")
            if short == "browser_press_key" and re.search(r"enter|return", str(args.get("key", "")), re.I):
                return _deny("Dry run: Enter can submit the form. Click the button or option instead.")
        if short == "browser_navigate" or (short == "browser_tabs" and args.get("action") == "new"):
            if host := _host(str(args.get("url") or "")):
                ctx.visited.add(mail.registrable(host))
        return {}

    return guard


def mcp_command(out_dir: Path) -> tuple[str, list[str]]:
    """How to start Playwright MCP against the shared browser. The Docker image pre-installs
    the pinned package (JOBHELPER_PLAYWRIGHT_MCP=playwright-mcp); elsewhere npx fetches it."""
    preinstalled = os.environ.get("JOBHELPER_PLAYWRIGHT_MCP")
    command, prefix = ((preinstalled, []) if preinstalled else (shutil.which("npx") or "npx", ["-y", PLAYWRIGHT_MCP]))
    return command, [*prefix, "--cdp-endpoint", BROWSER.cdp_url, "--output-dir", str(out_dir),
                     "--image-responses", "omit", "--timeout-action", "15000",
                     # Don't attach a full page snapshot to every action's result: those pile up
                     # in the context and dominate token use. The agent snapshots when it needs to.
                     "--snapshot-mode", "none",
                     # Webmail, plus the container's own noVNC / CDP and Hermes, so a click can't reach them either.
                     "--blocked-origins", "https://mail.google.com;https://outlook.live.com;https://outlook.office.com;"
                     "http://127.0.0.1:6080;http://localhost:6080;http://127.0.0.1:9333;http://localhost:9333;"
                     "http://host.docker.internal:8644"]


async def preflight(timeout: float = 45) -> str | None:
    """Start the browser and do a real MCP handshake with Playwright MCP.

    Returns None when the agent would get working browser tools, else a short reason.
    Run before each apply batch, so an infrastructure fault pauses the queue once instead
    of burning a model run (and a "needs you" alert) per application.
    """
    try:
        await BROWSER.context()
    except Exception as exc:
        return f"browser failed to start: {exc}"[:300]
    out_dir = config.ARTIFACT_DIR / "preflight"
    out_dir.mkdir(parents=True, exist_ok=True)
    command, args = mcp_command(out_dir)
    try:
        proc = await asyncio.create_subprocess_exec(command, *args, stdin=asyncio.subprocess.PIPE, env=child_env(),
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except Exception as exc:
        return f"could not start Playwright MCP ({command}): {exc}"[:300]
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "jobhelper-preflight", "version": "1"}}}
    try:
        proc.stdin.write((json.dumps(init) + "\n").encode())
        await proc.stdin.drain()
        line = await asyncio.wait_for(proc.stdout.readline(), timeout)
        if line and b'"result"' in line:
            return None
        err = (await asyncio.wait_for(proc.stderr.read(2000), 3)).decode(errors="replace").strip()
        return f"Playwright MCP did not answer the handshake: {err or line.decode(errors='replace')}"[:300]
    except asyncio.TimeoutError:
        return f"Playwright MCP gave no handshake reply within {int(timeout)}s"
    except Exception as exc:
        return f"Playwright MCP handshake failed: {exc}"[:300]
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def run(ctx: RunContext, portal: str, start_url: str) -> dict[str, Any]:
    """Run the agent. Returns {status, ...} built from report_result / report_blocker."""
    s = config.settings()
    art_dir = config.ARTIFACT_DIR / f"app-{ctx.application_id}"
    art_dir.mkdir(parents=True, exist_ok=True)
    transcript = art_dir / "transcript.jsonl"
    # Playwright MCP only uploads files from inside its output dir, so hand the agent a copy
    # of the chosen resume there; the guard then allows exactly that file.
    if ctx.resume_path.is_file():
        copy = art_dir / ctx.resume_path.name
        shutil.copyfile(ctx.resume_path, copy)
        ctx.resume_path = copy

    command, args = mcp_command(art_dir)
    mcp_servers = {
        # The CLI merges this onto its own env (which keeps CLAUDE_CODE_OAUTH_TOKEN): blank every secret here.
        "browser": {"type": "stdio", "command": command, "args": args, "env": {k: "" for k in secret_env_names()}},
        "jobhelper": build_server(ctx),
    }
    options = ClaudeAgentOptions(
        model=s["llm"]["apply_model"],
        system_prompt=system_prompt(ctx, portal),
        tools=[],                                   # no built-in file/shell tools at all
        allowed_tools=[f"mcp__browser__{t}" for t in BROWSER_TOOLS] + [f"mcp__jobhelper__{t}" for t in JOBHELPER_TOOLS],
        permission_mode="dontAsk",                  # anything not allowlisted is denied
        mcp_servers=mcp_servers,
        hooks={"PreToolUse": [HookMatcher(hooks=[make_guard(ctx, transcript, portal)], on_failure="block")]},
        max_turns=int(s["apply"].get("max_agent_turns", 80)),
        setting_sources=[],
        cwd=str(art_dir),
        env=llm.base_env(),
        effort="medium",
        extra_args={"no-session-persistence": None},   # no transcript of pages / form data left under ~/.claude
    )
    prompt = (f"Apply to this job. Start at: {start_url}\n"
              f"Job #{ctx.job.get('id')}: {ctx.job.get('title')} at {ctx.job.get('company')}.")
    if ctx.resumed_from:
        prompt += (f"\n\nThis is a resumed attempt. {ctx.resumed_from[:1500]}\n"
                   "First call browser_tabs(action='list') and select this application's tab (the page above) "
                   "if it is still open; continue there, keeping what is already filled or uploaded. If it's "
                   "gone, open a new tab at the start URL. Don't create a second account (use get_site_login), "
                   "and redo a step only if the page shows it isn't done.")
    last_text, result_msg, out = "", None, None
    async with BROWSER.lock:
        bctx = await BROWSER.context()   # make sure Chromium (and its CDP port) is up
        before = set(bctx.pages)
        parked = PARKED.get(ctx.application_id, [])   # a resumed run continues in its parked tabs
        ctx.page_urls = lambda: [p.url for p in [*bctx.pages, *parked] if p not in before or p in parked]
        try:
            async for msg in query(prompt=prompt, options=options):
                usage.observe(msg)
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            last_text = b.text
                elif isinstance(msg, ResultMessage):
                    result_msg = msg
        except Exception as exc:
            log.exception("apply agent crashed")
            usage.record(result_msg, task="apply", ref=f"app-{ctx.application_id}")
            if usage.LIMIT_TEXT.search(str(exc)):
                usage.note_limit_hit(str(exc))
                if not (ctx.result or ctx.blocker):
                    # Not the job's fault: it goes back in the queue, no failure alert.
                    out = {"status": "deferred", "notes": f"Claude usage limit: {exc}"}
            if out is None and not (ctx.result or ctx.blocker):
                out = {"status": "failed", "notes": f"agent error: {exc}"}
        else:
            usage.record(result_msg, task="apply", ref=f"app-{ctx.application_id}")
        own = [p for p in bctx.pages if p not in before] or [
            p for p in PARKED.get(ctx.application_id, []) if not p.is_closed()]
        if out is None:
            await _screenshot(art_dir / "final.png", own[-1] if own else None)
        # Keep the tabs open while the application waits for the user; otherwise close them.
        # shortcut: a run cancelled mid-way (Restart, timeout) leaves its unparked tabs open until the next
        # container restart; add cleanup in a finally if tabs pile up.
        keep = out is None and (ctx.blocker or (ctx.result or {}).get("status") == "ready_to_submit")
        old = PARKED.pop(ctx.application_id, [])
        if keep:
            PARKED[ctx.application_id] = list(dict.fromkeys(old + own))
        else:
            for p in dict.fromkeys(old + own):
                with contextlib.suppress(Exception):
                    await p.close()
    if out is not None:
        return out

    if ctx.blocker:
        return {"status": "needs_user", **ctx.blocker}
    if ctx.result:
        return ctx.result
    sub = getattr(result_msg, "subtype", "unknown")
    return {"status": "failed", "notes": f"agent ended without a result ({sub}): {last_text[:500]}"}


async def _screenshot(path: Path, page: Any = None) -> None:
    try:
        ctx = await BROWSER.context()
        page = page or (ctx.pages[-1] if ctx.pages else None)
        if page is not None:
            await page.screenshot(path=str(path), full_page=False)
    except Exception:
        pass
