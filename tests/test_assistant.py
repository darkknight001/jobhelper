"""Chat assistant: nothing changes without a confirmed, validated proposal."""
import dataclasses

import pytest
import yaml
from fastapi.testclient import TestClient

from jobhelper import assistant, config, db, pause


@pytest.fixture(autouse=True)
def restore_files():
    prof = config.profile_path().read_text(encoding="utf-8")
    over = config.OVERRIDES.read_text(encoding="utf-8") if config.OVERRIDES.exists() else None
    yield
    config.profile_path().write_text(prof, encoding="utf-8")
    if over is None:
        config.OVERRIDES.unlink(missing_ok=True)
    else:
        config.OVERRIDES.write_text(over, encoding="utf-8")
    pause.resume()
    assistant.PENDING.clear()


def test_only_listed_preferences_can_be_proposed():
    for bad in ([{"path": "apply.dry_run", "value": True}], [{"path": "email.alert_senders", "value": ["x.com"]}],
                [{"path": "profile.expected_ctc_lpa", "value": 99}], [{"path": "scoring.review_min", "value": 101}],
                [{"path": "targets.roles", "value": ["(unclosed"]}], [{"path": "search.keywords", "value": "x"}]):
        with pytest.raises(Exception):
            assistant.propose_change(bad)
    with pytest.raises(ValueError, match="nothing would change"):
        assistant.propose_change([{"path": "search.max_age_days", "value": config.settings()["search"]["max_age_days"]}])
    assert assistant.PENDING == {}


async def test_location_change_waits_for_confirm_and_keeps_profile_comments():
    s = config.settings()
    p = assistant.propose_change([
        {"path": "targets.locations", "value": s["targets"]["locations"] + ["kochi"]},
        {"path": "search.locations.naukri", "value": s["search"]["locations"]["naukri"] + ["kochi"]},
        {"path": "profile.preferences.preferred_locations", "value": ["Delhi NCR", "Pune"]},
    ])
    assert "kochi" not in config.settings()["targets"]["locations"]          # proposing changes nothing
    assert "targets.locations: + kochi" in p["summary"]
    comments = [ln for ln in config.profile_path().read_text(encoding="utf-8").splitlines() if ln.strip().startswith("#")]
    out = await assistant.execute(p["id"])
    assert out.startswith("Saved")
    assert "kochi" in config.settings()["targets"]["locations"]
    assert "kochi" in config.settings()["search"]["locations"]["naukri"]
    text = config.profile_path().read_text(encoding="utf-8")
    assert yaml.safe_load(text)["preferences"]["preferred_locations"] == ["Delhi NCR", "Pune"]
    assert all(c in text for c in comments)
    assert "expired or was already used" in await assistant.execute(p["id"])   # single use
    with db.session() as sess:
        assert sess.query(db.Event).filter_by(kind="assistant_action").count() == 1


async def test_actions_are_validated_then_run_on_confirm():
    with pytest.raises(ValueError):
        assistant.propose_action("delete_everything")
    with pytest.raises(ValueError, match="no job #999"):
        assistant.propose_action("approve_job", 999)
    with pytest.raises(ValueError):
        assistant.propose_action("pause", text="soonish")
    p = assistant.propose_action("pause", text="2h")
    assert not pause.is_paused()
    assert "Paused" in await assistant.execute(p["id"])
    assert pause.is_paused()


def test_chat_routes_need_login_and_confirm_runs_the_proposal(monkeypatch):
    patched = dataclasses.replace(config.secrets(), dashboard_password="pw")
    monkeypatch.setattr(config, "secrets", lambda: patched)
    from jobhelper.web.app import app
    c = TestClient(app, base_url="https://testserver")
    assert c.post("/assistant/chat", json={"messages": [{"role": "user", "text": "hi"}]},
                  follow_redirects=False).status_code == 303
    assert c.post("/login", data={"password": "pw", "next": "/"}, follow_redirects=False).status_code == 303

    async def fake_reply(messages):
        p = assistant.propose_action("pause", text="30m")
        return {"reply": "Tap Confirm to pause.", "proposals": [{"id": p["id"], "summary": p["summary"]}]}

    monkeypatch.setattr(assistant, "reply", fake_reply)
    out = c.post("/assistant/chat", json={"messages": [{"role": "user", "text": "pause 30m"}]}).json()
    assert not pause.is_paused()
    r = c.post(f"/assistant/confirm/{out['proposals'][0]['id']}").json()
    assert "Paused" in r["result"] and pause.is_paused()
    assert 'src="/static/chat.js?v=' in c.get("/").text


def test_transcript_rejects_bad_input():
    for bad in (None, [], [{"role": "system", "text": "x"}], [{"role": "user", "text": "a"}, {"role": "assistant", "text": "b"}]):
        with pytest.raises(ValueError):
            assistant._transcript(bad)
