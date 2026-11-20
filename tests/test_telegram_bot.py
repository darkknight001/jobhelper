"""JobHelper's own Telegram bot: pairing, only-you, buttons, routing with Hermes fallback."""
import pytest

from jobhelper import db, notify, telegram_bot


@pytest.fixture
def bot(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(telegram_bot, "STATE", tmp_path / "telegram.json")
    calls = []

    async def fake_call(method, payload, timeout=20):
        calls.append((method, payload))
        return {"message_id": 1, "username": "jobhelper_test_bot"}

    monkeypatch.setattr(telegram_bot, "_call", fake_call)
    return calls


def _msg(text, chat=111, user="Asha"):
    return {"update_id": 1, "message": {"text": text, "chat": {"id": chat}, "from": {"first_name": user}}}


async def test_pairing_needs_the_dashboard_code(bot):
    code = telegram_bot.pairing_code()
    await telegram_bot._handle_update(_msg("/start WRONG"))
    assert telegram_bot.paired_chat() is None
    await telegram_bot._handle_update(_msg(f"/start {code.lower()}"))
    assert telegram_bot.paired_chat() == 111 and telegram_bot.active()


async def test_strangers_are_ignored(bot):
    await telegram_bot._handle_update(_msg(f"/start {telegram_bot.pairing_code()}"))
    bot.clear()
    await telegram_bot._handle_update(_msg("/status", chat=999))
    await telegram_bot._handle_update({"update_id": 2, "callback_query": {
        "id": "x", "data": "apr:1", "message": {"chat": {"id": 999}, "message_id": 5}}})
    assert bot == []                                        # nothing sent, nothing done


async def test_buttons_run_actions_for_the_paired_chat(bot):
    await telegram_bot._handle_update(_msg(f"/start {telegram_bot.pairing_code()}"))
    with db.session() as s:
        j = db.Job(source="naukri", external_id="t1", url="u", title="Tech Lead", company="Acme", score=70.0,
                   status="review", score_detail={})
        s.add(j)
        s.flush()
        jid = j.id
    await telegram_bot._handle_update({"update_id": 3, "callback_query": {
        "id": "cb", "data": f"apr:{jid}", "message": {"chat": {"id": 111}, "message_id": 9}}})
    with db.session() as s:
        assert s.get(db.Job, jid).status == "approved"
    methods = [m for m, _ in bot]
    assert "answerCallbackQuery" in methods and "editMessageReplyMarkup" in methods


async def test_notify_prefers_the_bot_and_falls_back_to_hermes(bot, monkeypatch):
    hermes = []

    async def fake_hermes(text, kind="info"):
        hermes.append(text)
        return True

    monkeypatch.setattr(notify, "_send_hermes", fake_hermes)
    assert await notify.send("hello", kind="test")          # not paired yet -> Hermes
    assert hermes == ["hello"]
    await telegram_bot._handle_update(_msg(f"/start {telegram_bot.pairing_code()}"))
    bot.clear()
    await notify.send("with buttons", buttons=[[("✅ I submitted it", "sub:7")]])
    sent = [p for m, p in bot if m == "sendMessage"]
    assert sent and sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "sub:7"
    assert hermes == ["hello"]                               # Hermes not used once the bot works

    async def broken(*a, **k):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(telegram_bot, "send", broken)
    await notify.send("fallback please")
    assert hermes[-1] == "fallback please"


def test_http_request_logging_cannot_leak_the_bot_token():
    import logging
    from jobhelper import cli
    try:
        cli.main(["kb-reindex"])              # any command: logging is configured in main()
    except SystemExit:
        pass
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


async def test_network_errors_never_carry_the_token(monkeypatch):
    import httpx
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:SECRETSECRETSECRETSECRETSECRET")

    async def boom(self, url, **_):
        raise httpx.ReadTimeout(f"timed out on {url}")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    with pytest.raises(RuntimeError) as err:
        await telegram_bot._call("getMe", {})
    assert "SECRET" not in str(err.value) and "ReadTimeout" in str(err.value)


async def test_call_returns_the_result(monkeypatch):
    import httpx
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:x")

    async def ok(self, url, **_):
        return httpx.Response(200, json={"ok": True, "result": {"username": "b"}})

    monkeypatch.setattr(httpx.AsyncClient, "post", ok)
    assert (await telegram_bot._call("getMe", {}))["username"] == "b"


def _stuck_application(question="Willing to work in shifts?", options=("Yes", "No")):
    with db.session() as s:
        j = db.Job(source="naukri", external_id=question[:20], url="u", title="Tech Lead", company="Acme", status="needs_user")
        s.add(j)
        s.flush()
        a = db.Application(job_id=j.id, portal="naukri", mode="auto", status="needs_user", blocker="unanswerable_question: x")
        s.add(a)
        s.flush()
        db.add_event(s, "apply_needs_user", "x", application=a,
                     detail={"question": question, "options": list(options), "url": "https://n.example/1"})
        return a.id


async def test_question_is_answered_by_button_or_by_reply(bot, monkeypatch):
    from jobhelper.apply import engine
    runs = []

    async def fake_run_when_free(app_id, wait_seconds=1800):
        runs.append(app_id)

    monkeypatch.setattr(engine, "run_when_free", fake_run_when_free)
    await telegram_bot._handle_update(_msg(f"/start {telegram_bot.pairing_code()}"))
    aid = _stuck_application()
    rows = telegram_bot.question_buttons(aid, ["Yes", "No"])
    assert rows[0] == [("Yes", f"ans:{aid}:0"), ("No", f"ans:{aid}:1")]
    await telegram_bot._handle_update({"update_id": 4, "callback_query": {
        "id": "cb", "data": f"ans:{aid}:1", "message": {"chat": {"id": 111}, "message_id": 9}}})
    with db.session() as s:
        assert s.get(db.Application, aid).status == "queued"
        assert engine.last_block(s, aid)["question"] == "Willing to work in shifts?"
    aid2 = _stuck_application("Notice period in days?", ())
    reply = _msg("45")
    reply["message"]["reply_to_message"] = {"text": f"❓ #{aid2} Tech Lead at Acme asks:\nNotice period in days?"}
    await telegram_bot._handle_update(reply)
    with db.session() as s:
        assert s.get(db.Application, aid2).status == "queued"
    import asyncio
    await asyncio.sleep(0)
    assert runs == [aid, aid2]


def test_application_page_shows_the_question_and_takes_the_answer(monkeypatch):
    import dataclasses

    from fastapi.testclient import TestClient

    from jobhelper import config
    from jobhelper.apply import engine
    patched = dataclasses.replace(config.secrets(), dashboard_password="pw")
    monkeypatch.setattr(config, "secrets", lambda: patched)

    async def fake_run_when_free(app_id, wait_seconds=1800):
        return None

    monkeypatch.setattr(engine, "run_when_free", fake_run_when_free)
    from jobhelper.web.app import app
    c = TestClient(app, base_url="https://testserver")
    c.post("/login", data={"password": "pw", "next": "/"}, follow_redirects=False)
    aid = _stuck_application("Comfortable with on-call?", ("Yes", "No"))
    page = c.get(f"/applications/{aid}").text
    assert "Comfortable with on-call?" in page and 'name="choice" value="No"' in page
    r = c.post(f"/applications/{aid}/answer", data={"choice": "Yes"}, follow_redirects=False)
    assert "Saved" in r.headers["location"]
    with db.session() as s:
        assert s.get(db.Application, aid).status == "queued"


async def test_stuck_application_can_move_to_apply_yourself(bot):
    from jobhelper.apply import engine
    await telegram_bot._handle_update(_msg(f"/start {telegram_bot.pairing_code()}"))
    aid = _stuck_application("Upload a video introduction?", ())
    assert ("🖐 Apply myself", f"self:{aid}") in sum(telegram_bot.application_buttons(aid, "needs_user"), [])
    await telegram_bot._handle_update({"update_id": 7, "callback_query": {
        "id": "cb", "data": f"self:{aid}", "message": {"chat": {"id": 111}, "message_id": 9}}})
    with db.session() as s:
        a = s.get(db.Application, aid)
        assert (a.status, a.job.status, a.final_url, a.blocker) == ("apply_yourself", "apply_yourself", "https://n.example/1", None)
        assert "Was waiting on: unanswerable_question" in a.notes
    assert engine.move_to_apply_yourself(aid) == "nothing waiting on you with that id"       # only from needs_user
    assert "recorded" in await engine.mark_submitted(aid, via="test")                        # then "I submitted it" works
