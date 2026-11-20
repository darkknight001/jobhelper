"""Security fixes for the apply agent: per-run domain scoping, egress limits, dry-run Enter, transcript, env."""
import dataclasses
import json

import pytest

from jobhelper import browser, config, mail
from jobhelper.apply import agent, tools, vault
from jobhelper.apply.tools import RunContext

JOB = {"id": 1, "url": "https://www.linkedin.com/jobs/view/1", "apply_url": "https://boards.greenhouse.io/acme/jobs/1"}


def _ctx(**kw):
    return RunContext(application_id=1, job=dict(JOB), resume_path=config.RESUME_DIR / "resume.pdf",
                      dry_run=kw.pop("dry_run", True), tab_ready=True, **kw)


async def _decide(guard, tool, args):
    out = await guard({"tool_name": f"mcp__browser__{tool}", "tool_input": args, "hook_event_name": "PreToolUse"},
                      "id", None)
    return (out.get("hookSpecificOutput") or {}).get("permissionDecision", "allow")


def _handlers(ctx):
    """build_server's tool coroutines by name."""
    real = tools.create_sdk_mcp_server
    tools.create_sdk_mcp_server = lambda name, tools: {t.name: t.handler for t in tools}
    try:
        return tools.build_server(ctx)
    finally:
        tools.create_sdk_mcp_server = real


def _text(res):
    return res["content"][0]["text"]


# ---------------------------------------------------------------- H1 / M2 / L2: per-run domains


async def test_fetch_email_code_refuses_domains_outside_the_run(monkeypatch, tmp_path):
    calls = []

    async def fake_latest(dom, minutes=20):
        calls.append(dom)
        return {"code": "123456"}

    monkeypatch.setattr(mail, "latest_verification", fake_latest)
    ctx = _ctx()
    h = _handlers(ctx)
    for evil in ("github.com", "gmail.com", 'acme.com" OR from:bank.com', "accounts.google.com"):
        assert _text(await h["fetch_email_code"]({"site_domain": evil})).startswith("Refused")
    assert calls == [] and not mail.trusted_senders() & {"github.com"}
    # The job's own site, and a site the agent actually opened this run, still work.
    assert "123456" in _text(await h["fetch_email_code"]({"site_domain": "greenhouse.io"}))
    g = agent.make_guard(ctx, tmp_path / "t.jsonl", "company")
    assert await _decide(g, "browser_navigate", {"url": "https://careers.acme.co.in/apply"}) == "allow"
    assert "123456" in _text(await h["fetch_email_code"]({"site_domain": "acme.co.in"}))
    assert calls == ["greenhouse.io", "acme.co.in"]


async def test_live_tabs_count_as_this_runs_sites(monkeypatch):
    async def fake_latest(dom, minutes=20):
        return {"code": "1"}

    monkeypatch.setattr(mail, "latest_verification", fake_latest)
    ctx = _ctx()
    ctx.page_urls = lambda: ["https://acme.wd3.myworkdayjobs.com/en-US/careers/login"]   # reached by a click
    h = _handlers(ctx)
    assert "1" in _text(await h["fetch_email_code"]({"site_domain": "myworkdayjobs.com"}))
    assert _text(await h["fetch_email_code"]({"site_domain": "paypal.com"})).startswith("Refused")


async def test_site_logins_only_for_this_runs_sites(monkeypatch):
    from cryptography.fernet import Fernet
    patched = dataclasses.replace(config.secrets(), vault_key=Fernet.generate_key().decode())
    monkeypatch.setattr(config, "secrets", lambda: patched)
    vault.create("workday.com", "me@x.com")                       # saved by an earlier application
    ctx = _ctx()
    h = _handlers(ctx)
    assert _text(await h["get_site_login"]({"domain": "workday.com"})).startswith("Refused")
    assert _text(await h["create_site_login"]({"domain": "github.com"})).startswith("Refused")
    ctx.visited.add("workday.com")                                # the agent is on that site now
    assert json.loads(_text(await h["get_site_login"]({"domain": "workday.com"})))["username"] == "me@x.com"
    with pytest.raises(ValueError):
        vault.create('evil.com"', "me@x.com")


async def test_temporary_mail_domains_end_with_the_run(monkeypatch):
    from jobhelper.apply import engine
    seen = {}

    async def fake_run(ctx, portal, start_url):
        seen["during"] = set(mail.trusted_senders())
        return {"status": "failed", "notes": "x"}

    monkeypatch.setattr(agent, "run", fake_run)
    from jobhelper import db
    with db.session() as s:
        j = db.Job(source="greenhouse", external_id="sec1", url="https://careers.acmesec.io/1",
                   apply_url="https://jobs.acmesec.io/1", title="t", company="AcmeSec", posted_at=db.utcnow())
        s.add(j)
        s.flush()
        a = db.Application(job_id=j.id, portal="company", mode="auto", status="queued")
        s.add(a)
        s.flush()
        app_id = a.id
    await engine.apply_one(app_id)
    assert "acmesec.io" in seen["during"] and "acmesec.io" not in mail.trusted_senders()


def test_temporary_domains_never_take_mailbox_providers_or_junk():
    assert mail.allow_domain_temporarily("accounts.google.com") == ""
    assert mail.allow_domain_temporarily('x.com" OR from:bank.com') == ""
    assert mail.allow_domain_temporarily("careers.acme.co.in") == "acme.co.in"
    mail.forget_temporary_domains({"acme.co.in"})
    assert "acme.co.in" not in mail.trusted_senders()


@pytest.mark.parametrize("bad", ['acme.com"', "acme.com OR from:x", "a b.com", "{x}.com", "github"])
async def test_verification_lookup_rejects_non_hostnames(bad):
    with pytest.raises(mail.GuardrailViolation):
        await mail.latest_verification(bad)


# ---------------------------------------------------------------- M1: no internal / non-web targets


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "data:text/html,x", "chrome://version",
                                 "view-source:https://x.com", "http://127.0.0.1:9333/json", "http://localhost:6080/",
                                 "http://192.168.1.1/", "http://10.0.0.5/", "http://172.20.0.1/", "http://169.254.169.254/",
                                 "http://[::1]/", "http://[fd00::1]/", "http://host.docker.internal:8644/",
                                 "http://dockge.local/", "http://router/", "http://2130706433/", "http://127.1/",
                                 "http://0x7f.0.0.1/", "http://127.0.0.%31/", "http://100.100.1.1/",
                                 "http://127.0.0.1\\@example.com/"])
async def test_internal_and_non_web_urls_are_denied(tmp_path, url):
    g = agent.make_guard(_ctx(dry_run=False), tmp_path / "t.jsonl", "company")
    assert await _decide(g, "browser_navigate", {"url": url}) == "deny"
    assert await _decide(g, "browser_tabs", {"action": "new", "url": url}) == "deny"


@pytest.mark.parametrize("url", ["https://boards.greenhouse.io/acme/jobs/1", "https://acme.wd3.myworkdayjobs.com/x",
                                 "http://careers.acme.co.in/apply", "https://jobs.lever.co/acme/1"])
async def test_public_career_sites_still_allowed(tmp_path, url):
    g = agent.make_guard(_ctx(dry_run=False), tmp_path / "t.jsonl", "company")
    assert await _decide(g, "browser_navigate", {"url": url}) == "allow"
    assert await _decide(g, "browser_tabs", {"action": "new", "url": url}) == "allow"


# ---------------------------------------------------------------- M8: Enter in dry run


@pytest.mark.parametrize("key", ["Enter", "NumpadEnter", "Control+Enter"])
async def test_dry_run_denies_enter(tmp_path, key):
    g = agent.make_guard(_ctx(), tmp_path / "t.jsonl", "company")
    assert await _decide(g, "browser_press_key", {"key": key}) == "deny"
    assert await _decide(g, "browser_press_key", {"key": "Tab"}) == "allow"
    live = agent.make_guard(_ctx(dry_run=False), tmp_path / "t.jsonl", "company")
    assert await _decide(live, "browser_press_key", {"key": key}) == "allow"


# ---------------------------------------------------------------- L1: no passwords in the transcript


async def test_site_passwords_are_masked_in_the_transcript(tmp_path):
    ctx = _ctx()
    ctx.passwords.add("s3cretPw123456Aa9!")
    g = agent.make_guard(ctx, tmp_path / "t.jsonl", "company")
    await _decide(g, "browser_type", {"element": "Password", "ref": "e1", "text": "s3cretPw123456Aa9!"})
    await _decide(g, "browser_fill_form", {"fields": [{"name": "Password", "value": "s3cretPw123456Aa9!"}]})
    text = (tmp_path / "t.jsonl").read_text(encoding="utf-8")
    assert "s3cretPw" not in text and text.count("***") == 2 and "Password" in text


# ---------------------------------------------------------------- M3: no secrets in child processes


def test_chromium_and_mcp_get_no_secrets(monkeypatch):
    for k in ("CLAUDE_CODE_OAUTH_TOKEN", "GMAIL_APP_PASSWORD", "JOBHELPER_VAULT_KEY", "JOBHELPER_SESSION_SECRET",
              "JOBHELPER_API_TOKEN", "JOBHELPER_DASHBOARD_PASSWORD", "TELEGRAM_BOT_TOKEN", "HERMES_WEBHOOK_SECRET",
              "JOBHELPER_VNC_PASSWORD"):
        monkeypatch.setenv(k, "s")
    monkeypatch.setenv("DISPLAY", ":99")
    env = browser.child_env()
    assert env.get("DISPLAY") == ":99" and "PATH" in {k.upper() for k in env}
    assert not [k for k in env if k.upper().endswith(("TOKEN", "PASSWORD", "SECRET", "_KEY"))]
    assert {"CLAUDE_CODE_OAUTH_TOKEN", "GMAIL_APP_PASSWORD", "HERMES_WEBHOOK_SECRET"} <= {
        k.upper() for k in browser.secret_env_names()}


async def test_chromium_is_launched_with_the_scrubbed_env(monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "s")
    seen = {}

    class FakeCtx:
        pages = []
        def on(self, *a): pass

    class FakeChromium:
        async def connect_over_cdp(self, *a, **k): raise RuntimeError("none running")
        async def launch_persistent_context(self, *a, **k):
            seen.update(k)
            return FakeCtx()

    class FakePW:
        chromium = FakeChromium()

    b = browser.SharedBrowser()
    b._pw = FakePW()
    await b.context()
    assert "GMAIL_APP_PASSWORD" not in {k.upper() for k in seen["env"]}


# ---------------------------------------------------------------- WP5d: injection loopholes


def _fake_mailbox(monkeypatch, mails):
    """mails: {sender: subject}; every body carries a code. Returns the senders whose mail was opened."""
    opened = []
    ids = {str(i + 1).encode(): m for i, m in enumerate(mails.items())}

    class FakeIMAP:
        def __init__(self, *a, **k): pass
        def login(self, *a): pass
        def select(self, *a): pass
        def logout(self): pass
        def search(self, _c, _k, query):
            return "OK", [b" ".join(i for i, (snd, _) in ids.items() if snd.split("@")[1] in query)]
        def fetch(self, i, what):
            sender, subject = ids[i]
            if "HEADER.FIELDS" in what:
                return "OK", [(b"h", f"From: {sender}\r\nSubject: {subject}\r\n\r\n".encode())]
            opened.append(sender)
            return "OK", [(b"b", f"From: {sender}\r\nSubject: {subject}\r\n\r\nYour code is 482913\r\n".encode())]

    from cryptography.fernet import Fernet
    patched = dataclasses.replace(config.secrets(), gmail_address="me@x.com", gmail_app_password="pw",
                                  vault_key=Fernet.generate_key().decode())
    monkeypatch.setattr(config, "secrets", lambda: patched)
    monkeypatch.setattr(mail, "ReadOnlyIMAP", FakeIMAP)
    return opened


async def test_visited_site_otp_is_not_returned_but_career_login_code_is(monkeypatch, tmp_path):
    opened = _fake_mailbox(monkeypatch, {"noreply@github.com": "[GitHub] Please verify your device",
                                         "no-reply@acme.co.in": "Verify your email"})
    ctx = _ctx()
    h, g = _handlers(ctx), agent.make_guard(ctx, tmp_path / "t.jsonl", "company")
    try:
        # A page sent the agent to github.com: it may look, but GitHub's OTP mail stays closed.
        assert await _decide(g, "browser_navigate", {"url": "https://github.com/login"}) == "allow"
        assert _text(await h["fetch_email_code"]({"site_domain": "github.com"})).startswith("Nothing yet")
        assert "github.com" not in mail.trusted_senders() and opened == []
        # A career site the agent signed up on still gets its "Verify your email" code.
        assert await _decide(g, "browser_navigate", {"url": "https://careers.acme.co.in/register"}) == "allow"
        await h["create_site_login"]({"domain": "careers.acme.co.in"})
        got = json.loads(_text(await h["fetch_email_code"]({"site_domain": "acme.co.in"})))
        assert got["code"] == "482913" and opened == ["no-reply@acme.co.in"]
        assert "acme.co.in" not in mail.trusted_senders()          # gated, not trusted
    finally:
        mail.forget_temporary_domains(ctx.mail_domains)
    assert "github.com" not in mail.allowlist()


@pytest.mark.parametrize("url", ["https://www.linkedin.com/messaging/thread/1/", "https://www.linkedin.com/mynetwork/",
                                 "https://www.linkedin.com/feed/", "https://www.linkedin.com/in/me/edit/intro/",
                                 "https://www.linkedin.com/psettings/", "https://www.linkedin.com/mypreferences/d/",
                                 "https://www.naukri.com/mnjuser/profile", "https://www.naukri.com/mnjuser/inbox",
                                 "https://www.naukri.com/mailers", "https://www.naukri.com/nlogin/settings"])
async def test_portal_messaging_profile_and_settings_are_off_limits(tmp_path, url):
    g = agent.make_guard(_ctx(dry_run=False), tmp_path / "t.jsonl", "linkedin")
    assert await _decide(g, "browser_navigate", {"url": url}) == "deny"
    assert await _decide(g, "browser_tabs", {"action": "new", "url": url}) == "deny"


@pytest.mark.parametrize("url", ["https://www.linkedin.com/jobs/view/123/", "https://www.linkedin.com/jobs/search/?keywords=go",
                                 "https://www.linkedin.com/company/acme/", "https://www.naukri.com/job-listings-x-123",
                                 "https://www.naukri.com/python-jobs-in-pune"])
async def test_portal_job_pages_stay_open(tmp_path, url):
    g = agent.make_guard(_ctx(dry_run=False), tmp_path / "t.jsonl", "linkedin")
    assert await _decide(g, "browser_navigate", {"url": url}) == "allow"


async def test_dry_run_denies_portal_social_clicks(tmp_path):
    g = agent.make_guard(_ctx(), tmp_path / "t.jsonl", "linkedin")
    for label in ("Send", "Message Priya button", "Connect", "Follow button", "Post"):
        assert await _decide(g, "browser_click", {"element": label, "ref": "e1"}) == "deny", label
    for label in ("Easy Apply button", "Next button", "Follow company checkbox (untick)", "Messaging settings help"):
        assert await _decide(g, "browser_click", {"element": label, "ref": "e1"}) == "allow", label
    company = agent.make_guard(_ctx(), tmp_path / "t.jsonl", "company")
    assert await _decide(company, "browser_click", {"element": "Send verification code", "ref": "e1"}) == "allow"


async def test_dry_run_denies_line_breaks_in_typed_values(tmp_path):
    g = agent.make_guard(_ctx(), tmp_path / "t.jsonl", "company")
    assert await _decide(g, "browser_type", {"element": "Name", "ref": "e1", "text": "Asha\n"}) == "deny"
    assert await _decide(g, "browser_fill_form", {"fields": [{"name": "A", "value": "ok"},
                                                              {"name": "B", "value": "x\ry"}]}) == "deny"
    assert await _decide(g, "browser_type", {"element": "Name", "ref": "e1", "text": "Asha"}) == "allow"
    live = agent.make_guard(_ctx(dry_run=False), tmp_path / "t.jsonl", "company")
    assert await _decide(live, "browser_type", {"element": "Cover", "ref": "e1", "text": "a\nb"}) == "allow"


async def test_reported_urls_must_be_on_this_runs_sites(tmp_path):
    ctx = _ctx()
    h = _handlers(ctx)
    await h["report_result"]({"status": "applied", "evidence": "x", "final_url": "https://evil.com/collect", "notes": ""})
    assert ctx.result["final_url"] == JOB["apply_url"]
    await h["report_blocker"]({"kind": "bot_check", "details": "x", "url": "javascript:alert(1)"})
    assert ctx.blocker["url"] == JOB["apply_url"]
    g = agent.make_guard(ctx, tmp_path / "t.jsonl", "company")
    await _decide(g, "browser_navigate", {"url": "https://careers.acme.co.in/apply"})
    for url in ("https://careers.acme.co.in/thanks", "https://boards.greenhouse.io/acme/jobs/1/confirm"):
        await h["report_result"]({"status": "applied", "evidence": "x", "final_url": url, "notes": ""})
        assert ctx.result["final_url"] == url


def test_claude_cli_env_blanks_secrets_but_keeps_the_oauth_token(monkeypatch):
    from jobhelper import llm
    names = ("GMAIL_APP_PASSWORD", "JOBHELPER_VAULT_KEY", "JOBHELPER_SESSION_SECRET", "JOBHELPER_API_TOKEN",
             "JOBHELPER_DASHBOARD_PASSWORD", "TELEGRAM_BOT_TOKEN", "HERMES_WEBHOOK_SECRET", "JOBHELPER_VNC_PASSWORD")
    for k in names:
        monkeypatch.setenv(k, "s")
    patched = dataclasses.replace(config.secrets(), claude_oauth_token="tok")
    monkeypatch.setattr(config, "secrets", lambda: patched)
    env = {k.upper(): v for k, v in llm.base_env().items()}
    assert all(env[k] == "" for k in names) and env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"


async def test_apply_agent_does_not_persist_sessions(monkeypatch):
    import asyncio
    seen = {}

    class FakeCtx:
        pages = []

    class FakeBrowser:
        lock, cdp_url = asyncio.Lock(), "http://127.0.0.1:9333"
        async def context(self): return FakeCtx()

    async def fake_query(prompt, options):
        seen["options"] = options
        return
        yield

    monkeypatch.setattr(agent, "BROWSER", FakeBrowser())
    monkeypatch.setattr(agent, "query", fake_query)
    await agent.run(_ctx(), "company", JOB["apply_url"])
    assert seen["options"].extra_args == {"no-session-persistence": None}
