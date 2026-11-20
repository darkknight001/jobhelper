"""PreToolUse guard of the apply agent."""
from pathlib import Path

import pytest

from jobhelper import config
from jobhelper.apply import agent
from jobhelper.apply.tools import RunContext


def _guard(tmp_path, *, dry_run=True, portal="linkedin", tab_ready=True):
    ctx = RunContext(application_id=1, job={"id": 1}, resume_path=config.RESUME_DIR / "resume.pdf", dry_run=dry_run,
                     tab_ready=tab_ready)
    return agent.make_guard(ctx, tmp_path / "t.jsonl", portal)


async def _decide(guard, tool, args):
    out = await guard({"tool_name": f"mcp__browser__{tool}", "tool_input": args, "hook_event_name": "PreToolUse"},
                      "id", None)
    return (out.get("hookSpecificOutput") or {}).get("permissionDecision", "allow")


@pytest.mark.parametrize("url", ["https://mail.google.com/mail/u/0/", "https://outlook.live.com/mail",
                                 "https://gmail.com"])
async def test_webmail_blocked(tmp_path, url):
    assert await _decide(_guard(tmp_path), "browser_navigate", {"url": url}) == "deny"


async def test_career_site_allowed(tmp_path):
    assert await _decide(_guard(tmp_path), "browser_navigate", {"url": "https://boards.greenhouse.io/x/jobs/1"}) == "allow"


@pytest.mark.parametrize("tool", ["browser_evaluate", "browser_run_code_unsafe", "browser_cookie_list",
                                  "browser_storage_state", "browser_close", "browser_route"])
async def test_dangerous_browser_tools_denied(tmp_path, tool):
    assert await _decide(_guard(tmp_path), tool, {}) == "deny"


async def test_upload_only_from_resume_dir(tmp_path):
    g = _guard(tmp_path)
    assert await _decide(g, "browser_file_upload", {"paths": [str(config.RESUME_DIR / "resume.pdf")]}) == "allow"
    assert await _decide(g, "browser_file_upload", {"paths": [str(Path.home() / ".ssh" / "id_rsa")]}) == "deny"
    assert await _decide(g, "browser_file_upload", {"paths": [str(config.RESUME_DIR / ".." / "profile.yaml")]}) == "deny"


@pytest.mark.parametrize("label", ["Submit application button", "Submit your application", "Review and submit"])
async def test_dry_run_blocks_final_submit(tmp_path, label):
    assert await _decide(_guard(tmp_path), "browser_click", {"element": label, "ref": "e1"}) == "deny"


async def test_dry_run_allows_navigation_clicks(tmp_path):
    g = _guard(tmp_path)
    assert await _decide(g, "browser_click", {"element": "Easy Apply button", "ref": "e1"}) == "allow"
    assert await _decide(g, "browser_click", {"element": "Next", "ref": "e2"}) == "allow"


async def test_dry_run_naukri_apply_is_final(tmp_path):
    assert await _decide(_guard(tmp_path, portal="naukri"), "browser_click", {"element": "Apply button", "ref": "e1"}) == "deny"


async def test_live_mode_allows_submit(tmp_path):
    g = _guard(tmp_path, dry_run=False)
    assert await _decide(g, "browser_click", {"element": "Submit application button", "ref": "e1"}) == "allow"


async def test_every_call_is_transcribed(tmp_path):
    g = _guard(tmp_path)
    await _decide(g, "browser_snapshot", {})
    assert "browser_snapshot" in (tmp_path / "t.jsonl").read_text()


@pytest.mark.parametrize("url", ["https://accounts.google.com/o/oauth2/v2/auth?client_id=x",
                                 "https://myaccount.google.com/security", "https://drive.google.com/drive/my-drive",
                                 "https://docs.google.com/document/d/abc", "https://appleid.apple.com",
                                 "https://login.microsoftonline.com/common/oauth2"])
async def test_account_pages_blocked(tmp_path, url):
    assert await _decide(_guard(tmp_path, dry_run=False), "browser_navigate", {"url": url}) == "deny"


async def test_google_forms_still_allowed(tmp_path):
    g = _guard(tmp_path, dry_run=False)
    assert await _decide(g, "browser_navigate", {"url": "https://docs.google.com/forms/d/e/abc/viewform"}) == "allow"


@pytest.mark.parametrize("label", ["Sign in with Google button", "Continue with Google", "Sign up with Apple",
                                   "Log in using Microsoft", "Google Sign-In"])
async def test_social_signin_clicks_blocked(tmp_path, label):
    assert await _decide(_guard(tmp_path, dry_run=False), "browser_click", {"element": label, "ref": "e1"}) == "deny"


async def test_linkedin_and_email_signin_allowed(tmp_path):
    g = _guard(tmp_path, dry_run=False)
    assert await _decide(g, "browser_click", {"element": "Apply with LinkedIn", "ref": "e1"}) == "allow"
    assert await _decide(g, "browser_click", {"element": "Sign in with email", "ref": "e2"}) == "allow"


async def test_upload_allows_the_run_copy_of_the_resume(tmp_path):
    copy = tmp_path / "resume.pdf"          # Playwright MCP only uploads from the run's own folder
    copy.write_bytes(b"%PDF")
    ctx = RunContext(application_id=1, job={"id": 1}, resume_path=copy, dry_run=False, tab_ready=True)
    g = agent.make_guard(ctx, tmp_path / "t.jsonl", "company")
    allow = await g({"tool_name": "mcp__browser__browser_file_upload", "tool_input": {"paths": [str(copy)]},
                     "hook_event_name": "PreToolUse"}, "id", None)
    other = tmp_path / "secrets.txt"
    deny = await g({"tool_name": "mcp__browser__browser_file_upload", "tool_input": {"paths": [str(other)]},
                    "hook_event_name": "PreToolUse"}, "id", None)
    assert allow == {} and deny["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("label", ["Sign in (Submit) button", "Submit - sign in", "Create Account submit button",
                                   "Submit verification code", "Submit OTP"])
async def test_dry_run_allows_auth_submit_buttons(tmp_path, label):
    assert await _decide(_guard(tmp_path), "browser_click", {"element": label, "ref": "e1"}) == "allow"


@pytest.mark.parametrize("label", ["Submit", "Submit application", "Submit button", "Review and submit"])
async def test_dry_run_still_blocks_application_submit(tmp_path, label):
    assert await _decide(_guard(tmp_path), "browser_click", {"element": label, "ref": "e1"}) == "deny"


async def test_dry_run_allows_buttons_that_open_the_form(tmp_path):
    g = _guard(tmp_path, portal="naukri")
    assert await _decide(g, "browser_click", {"element": "I'm interested (opens the application form) button", "ref": "e1"}) == "allow"
    assert await _decide(g, "browser_click", {"element": "I'm interested", "ref": "e2"}) == "deny"
    assert await _decide(g, "browser_click", {"element": "Apply button", "ref": "e3"}) == "deny"


async def test_agent_must_open_its_own_tab_first_and_never_close_tabs(tmp_path):
    g = _guard(tmp_path, tab_ready=False)
    assert await _decide(g, "browser_navigate", {"url": "https://boards.greenhouse.io/x/jobs/1"}) == "deny"
    assert await _decide(g, "browser_snapshot", {}) == "deny"
    assert await _decide(g, "browser_tabs", {"action": "list"}) == "allow"
    assert await _decide(g, "browser_tabs", {"action": "new", "url": "https://mail.google.com/"}) == "deny"
    assert await _decide(g, "browser_tabs", {"action": "close", "index": 0}) == "deny"
    assert await _decide(g, "browser_tabs", {"action": "new", "url": "https://boards.greenhouse.io/x/jobs/1"}) == "allow"
    assert await _decide(g, "browser_snapshot", {}) == "allow"
