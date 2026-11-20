"""Pausing the workflow: durations, expiry, what a pause holds back, bot + dashboard controls."""
import dataclasses
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from jobhelper import config, pause, scheduler, summary, telegram_bot
from jobhelper.apply import engine


@pytest.fixture(autouse=True)
def fresh_pause(monkeypatch, tmp_path):
    monkeypatch.setattr(pause, "STATE", tmp_path / "pause.json")


@pytest.mark.parametrize("text,minutes", [("30m", 30), ("2h", 120), ("1h30m", 90), ("1d", 1440),
                                          ("90", 90), ("2 hours", 120), ("45 min", 45)])
def test_durations(text, minutes):
    assert pause.parse_duration(text) == timedelta(minutes=minutes)


def test_open_ended_and_morning():
    assert pause.parse_duration("") is None and pause.parse_duration("off") is None
    left = pause.parse_duration("tomorrow")
    assert timedelta() < left <= timedelta(hours=24)
    assert (datetime.now(config.tz()) + left).strftime("%H:%M") in ("09:00", "08:59")


@pytest.mark.parametrize("bad", ["soon", "2x", "0m", "40d"])
def test_bad_durations(bad):
    with pytest.raises(ValueError):
        pause.parse_duration(bad)


def test_pause_resume_and_expiry():
    assert not pause.is_paused()
    p = pause.pause(timedelta(hours=2), via="test")
    assert pause.is_paused() and p["label"].startswith("until ")
    assert pause.resume() and not pause.is_paused() and not pause.resume()
    pause.pause(None)
    assert pause.current()["label"] == "until you resume" and not pause.expired()
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    pause._write({"since": past.isoformat(), "until": past.isoformat(), "via": "t"})
    assert not pause.is_paused()
    assert pause.expired() and not pause.expired()          # reported once, then cleared


async def test_pause_holds_back_search_and_applying_but_not_email():
    ran = []

    async def job(name):
        ran.append(name)

    pause.pause(timedelta(hours=1))
    for name in ("discover", "apply", "catch-up scoring", "prep retry", "email", "summary", "weekly"):
        await scheduler._safe(name, job, name)
    assert ran == ["email", "summary", "weekly"]
    assert "paused" in await engine.run_queue()
    assert "paused" not in await engine.run_queue(ignore_pause=True)   # the dashboard's Run now
    pause.resume()
    assert "paused" not in await engine.run_queue()


def test_summary_mentions_the_pause():
    pause.pause(timedelta(hours=3))
    assert "⏸ Paused until" in summary.daily_text()


async def test_bot_pause_commands(monkeypatch):
    sent = []

    async def fake_send(text, buttons=None):
        sent.append((text, buttons))
        return True

    async def nothing(*_):
        return None

    monkeypatch.setattr(telegram_bot, "send", fake_send)
    monkeypatch.setattr(pause, "_continue_soon", nothing)
    await telegram_bot.handle_command("/pause")
    assert sent[-1][1] == telegram_bot.PAUSE_BUTTONS and not pause.is_paused()
    assert (await telegram_bot.handle_action("pse:60")).startswith("⏸ Paused")
    assert pause.is_paused()
    await telegram_bot.handle_command("/resume")
    assert sent[-1][0].startswith("▶ Resumed") and not pause.is_paused()
    await telegram_bot.handle_command("/pause 2h")
    left = pause.current()["until"] - datetime.now(timezone.utc)
    assert timedelta(minutes=119) < left <= timedelta(hours=2)
    await telegram_bot.handle_command("/pause whenever")
    assert sent[-1][0].startswith("Not paused")
    assert "/pause" in telegram_bot.HELP and ("pause", "Pause job search and applying") in telegram_bot.COMMANDS


def test_dashboard_banner_and_buttons(monkeypatch):
    patched = dataclasses.replace(config.secrets(), dashboard_password="pw")
    monkeypatch.setattr(config, "secrets", lambda: patched)

    async def quiet(*_, **__):
        return True

    from jobhelper.web import app as web
    monkeypatch.setattr(web.notify, "send", quiet)
    monkeypatch.setattr(pause, "_continue_soon", lambda *_: quiet())
    c = TestClient(web.app, base_url="https://testserver")
    c.post("/login", data={"password": "pw", "next": "/"})
    assert ">1 hour</button>" in c.get("/settings").text and "Paused until" not in c.get("/").text
    c.post("/pause", data={"duration": "180"})
    assert pause.is_paused() and "Resume now" in c.get("/").text
    c.post("/resume", data={"back": "/settings"})
    assert not pause.is_paused()
