"""Review-then-submit: hold the queue after a dry-run fill until the user decides."""
from datetime import timedelta

from jobhelper import config, db
from jobhelper.apply import engine


def _queued(n=1, portal="linkedin"):
    ids = []
    with db.session() as s:
        for i in range(n):
            j = db.Job(source=portal, external_id=f"r{i}", url="u", title=f"Tech Lead {i}", company=f"Co{i}",
                       score=90.0 - i, score_detail={"location_ok": True, "red_flags": []}, status="queued_auto")
            s.add(j)
            s.flush()
            a = db.Application(job_id=j.id, portal=portal, mode="auto", status="queued")
            s.add(a)
            s.flush()
            ids.append(a.id)
    return ids


def _patch(monkeypatch, results):
    sent, runs = [], []

    async def fake_preflight():
        return None

    async def fake_run(ctx, portal, start_url):
        runs.append(ctx.application_id)
        sent.append(("resume", ctx.resume_path.name))
        return results.pop(0)

    async def fake_send(text, kind="info", **_):
        sent.append((kind, text))
        return True

    async def no_sleep(*_):
        return None

    monkeypatch.setattr(engine.agent, "preflight", fake_preflight)
    monkeypatch.setattr(engine.agent, "run", fake_run)
    monkeypatch.setattr(engine.notify, "send", fake_send)
    monkeypatch.setattr(engine.asyncio, "sleep", no_sleep)
    return sent, runs


async def test_filled_form_holds_the_queue(monkeypatch):
    first, second = _queued(2)
    sent, runs = _patch(monkeypatch, [{"status": "ready_to_submit", "evidence": "filled"},
                                      {"status": "ready_to_submit", "evidence": "filled"}])
    out = await engine.run_queue(ignore_window=True)
    assert runs == [first]                                   # batch stopped after the first fill
    assert out["results"][0]["status"] == "awaiting_review"
    assert any(kind == "review" and "Ready for your review" in t for kind, t in sent)
    again = await engine.run_queue(ignore_window=True)
    assert "held" in again and runs == [first]               # nothing else touches the browser


async def test_decisions_release_the_hold(monkeypatch):
    first, second = _queued(2)
    _patch(monkeypatch, [{"status": "ready_to_submit"}, {"status": "ready_to_submit"}])
    await engine.run_queue(ignore_window=True)
    assert "recorded" in await engine.mark_submitted(first, via="test")
    with db.session() as s:
        a = s.get(db.Application, first)
        assert a.status == "applied" and a.applied_at is not None and a.job.status == "applied"
    out = await engine.run_queue(ignore_window=True)
    assert out["results"][0]["application"] == second
    assert "skipped" in engine.remove_application(second, "Skipped after review", via="test")
    with db.session() as s:
        assert s.get(db.Application, second).status == "skipped_by_user"
        assert s.get(db.Application, second).job.status == "dismissed"


async def test_hold_lapses_after_the_window(monkeypatch):
    first, second = _queued(2)
    _patch(monkeypatch, [{"status": "ready_to_submit"}, {"status": "ready_to_submit"}])
    await engine.run_queue(ignore_window=True)
    with db.session() as s:
        s.get(db.Application, first).last_update_at = db.utcnow() - timedelta(hours=13)
    out = await engine.run_queue(ignore_window=True)
    assert out["results"][0]["application"] == second
    with db.session() as s:
        assert s.get(db.Application, first).status == "review_expired"


async def test_hold_off_keeps_old_behaviour(monkeypatch):
    from jobhelper import config
    config.set_override("apply.hold_for_review", False)
    try:
        first, second = _queued(2)
        _, runs = _patch(monkeypatch, [{"status": "ready_to_submit"}, {"status": "ready_to_submit"}])
        await engine.run_queue(ignore_window=True)
        assert runs == [first, second]
        with db.session() as s:
            assert s.get(db.Application, first).status == "dry_run_ok"
    finally:
        config.set_override("apply.hold_for_review", True)


def test_submitted_requires_a_reviewed_form():
    (aid,) = _queued(1)
    import asyncio
    assert "nothing awaiting review" in asyncio.run(engine.mark_submitted(aid))


def test_skip_any_stuck_application_with_reason():
    a_id, b_id = _queued(2)
    with db.session() as s:
        s.get(db.Application, a_id).status = "needs_user"
    assert "skipped" in engine.remove_application(a_id, "No longer accepting applications")
    assert "skipped" in engine.remove_application(b_id, "Duplicate or aggregator listing")
    with db.session() as s:
        a = s.get(db.Application, a_id)
        assert a.status == "skipped_by_user" and a.job.status == "dismissed"
        assert "No longer accepting" in a.notes and a.job.review_note == "No longer accepting applications"
        s.get(db.Application, b_id).status = "applied"
    assert "nothing to skip" in engine.remove_application(b_id, "x")   # applied ones stay put


def test_skip_as_already_applied_counts_as_applied_not_dismissed():
    (a_id,) = _queued(1)
    engine.remove_application(a_id, engine.ALREADY_APPLIED)
    with db.session() as s:
        a = s.get(db.Application, a_id)
        assert a.status == "already_applied" and a.job.status == "applied"
        assert s.query(db.Event).filter_by(kind="already_applied").count() == 1


async def test_skipping_the_held_application_releases_the_queue(monkeypatch):
    first, second = _queued(2)
    _patch(monkeypatch, [{"status": "ready_to_submit"}, {"status": "ready_to_submit"}])
    await engine.run_queue(ignore_window=True)
    engine.remove_application(first, "Posting closed or expired")
    out = await engine.run_queue(ignore_window=True)
    assert out["results"][0]["application"] == second


async def test_resume_runs_that_application_not_the_queue_head(monkeypatch):
    head, unblocked = _queued(2)          # `head` has the higher score, so it's first in line
    _, runs = _patch(monkeypatch, [{"status": "ready_to_submit"}])
    await engine.run_queue(max_runs=1, ignore_window=True, only=[unblocked])
    assert runs == [unblocked]


def _queued_with(apply_url, n=1, source="greenhouse"):
    ids = []
    with db.session() as s:
        for i in range(n):
            j = db.Job(source=source, external_id=f"w{apply_url}{i}", url=apply_url, apply_url=apply_url,
                       title=f"Backend Lead {i}", company="Acme", score=90.0 - i,
                       score_detail={"location_ok": True, "red_flags": []}, status="queued_auto")
            s.add(j)
            s.flush()
            a = db.Application(job_id=j.id, portal="company", mode="auto", status="queued")
            s.add(a)
            s.flush()
            ids.append(a.id)
    return ids


async def test_known_blocking_site_goes_to_apply_yourself_without_an_agent_run(monkeypatch):
    (aid,) = _queued_with("https://apply.workable.com/acme/j/123/apply/")
    sent, runs = _patch(monkeypatch, [])
    out = await engine.run_queue(ignore_window=True)
    assert runs == [] and out["results"][0]["status"] == "apply_yourself"
    assert any(kind == "apply_yourself" and "workable.com" in t for kind, t in sent)
    assert "recorded" in await engine.mark_submitted(aid, via="test")
    with db.session() as s:
        assert s.get(db.Application, aid).status == "applied"


async def test_bot_check_is_learned_and_does_not_hold_the_queue(monkeypatch):
    from jobhelper import config
    first, second = _queued_with("https://careers.blocky.io/jobs/1", n=2)
    results = [{"status": "needs_user", "kind": "bot_check", "details": "Cloudflare: verify you are human",
                "url": "https://careers.blocky.io/apply"}]
    sent, runs = _patch(monkeypatch, results)
    out = await engine.run_queue(ignore_window=True)
    assert runs == [first]                                           # second skipped the agent entirely
    assert [r["status"] for r in out["results"]] == ["apply_yourself", "apply_yourself"]
    assert "blocky.io" in engine.manual_sites()
    config.set_override("apply.manual_sites", [])


async def test_restart_cancels_a_live_run_and_runs_it_again(monkeypatch):
    import asyncio
    (aid,) = _queued(1)
    started = asyncio.Event()
    calls = []

    async def slow_run(ctx, portal, url):
        calls.append(ctx.application_id)
        if len(calls) == 1:
            started.set()
            await asyncio.sleep(3600)              # a hung first attempt
        return {"status": "ready_to_submit"}

    async def ok_preflight():
        return None

    async def fake_send(text, kind="info", **_):
        return True

    monkeypatch.setattr(engine.agent, "preflight", ok_preflight)
    monkeypatch.setattr(engine.agent, "run", slow_run)
    monkeypatch.setattr(engine.notify, "send", fake_send)
    batch = asyncio.create_task(engine.run_queue(ignore_window=True))
    await started.wait()
    assert "restarting" in await engine.restart_application(aid, via="test")
    out = await batch
    assert out["results"][0]["status"] == "restarted"
    again = await engine.run_after_restart(aid)
    assert calls == [aid, aid] and again["results"][0]["status"] == "awaiting_review"


async def test_hung_run_times_out(monkeypatch):
    import asyncio
    from jobhelper import config
    config.set_override("apply.run_timeout_minutes", 0.001)
    try:
        (aid,) = _queued(1)

        async def hang(ctx, portal, url):
            await asyncio.sleep(3600)

        async def ok_preflight():
            return None

        sent = []

        async def fake_send(text, kind="info", **_):
            sent.append(text)
            return True

        monkeypatch.setattr(engine.agent, "preflight", ok_preflight)
        monkeypatch.setattr(engine.agent, "run", hang)
        monkeypatch.setattr(engine.notify, "send", fake_send)
        out = await engine.run_queue(ignore_window=True)
        assert out["results"][0]["status"] == "timed_out"
        with db.session() as s:
            assert s.get(db.Application, aid).status == "needs_user"
        assert any("Restart" in t for t in sent)
    finally:
        config.set_override("apply.run_timeout_minutes", 20)


def test_stranded_applying_is_requeued_on_startup():
    (aid,) = _queued(1)
    with db.session() as s:
        s.get(db.Application, aid).status = "applying"
    assert engine.recover_stranded() == 1
    with db.session() as s:
        assert s.get(db.Application, aid).status == "queued"


async def test_main_resume_is_always_uploaded(monkeypatch):
    from jobhelper import config
    for name in ("resume.pdf", "resume TL.pdf"):
        (config.RESUME_DIR / name).write_bytes(b"%PDF-1.4 test")
    (app_id,) = _queued(1)
    with db.session() as s:
        s.get(db.Application, app_id).job.resume_variant = "resume TL.pdf"   # an old pick
    sent, _ = _patch(monkeypatch, [{"status": "failed", "notes": "x"}])
    await engine.apply_one(app_id)
    assert ("resume", "resume.pdf") in sent


async def test_resumed_run_is_told_where_the_last_one_stopped(monkeypatch):
    (aid,) = _queued(1)
    _patch(monkeypatch, [])
    seen = []

    async def fake_run(ctx, portal, start_url):
        seen.append(ctx.resumed_from)
        return ({"status": "needs_user", "kind": "otp", "details": "Enter the phone OTP on step 3"} if len(seen) == 1
                else {"status": "applied", "evidence": "done"})

    monkeypatch.setattr(engine.agent, "run", fake_run)
    await engine.run_queue(ignore_window=True)
    assert "re-queued" in engine.resume_blocked(aid)
    await engine.run_queue(ignore_window=True, only=[aid])
    assert seen[0] is None and "stopped with: otp: Enter the phone OTP on step 3" in seen[1]


async def test_answered_question_goes_to_kb_and_the_resumed_run(monkeypatch):
    from jobhelper import kb
    first, other = _queued(2)
    sent, _ = _patch(monkeypatch, [])
    seen = []

    async def fake_run(ctx, portal, start_url):
        seen.append((ctx.application_id, ctx.resumed_from))
        if len(seen) == 1:
            return {"status": "needs_user", "kind": "unanswerable_question", "details": "chatbot step 2",
                    "url": "https://naukri.example/job/1", "question": "Willing to work in shifts?",
                    "options": ["Yes", "No"]}
        return {"status": "applied", "evidence": "done"}

    monkeypatch.setattr(engine.agent, "run", fake_run)
    await engine.run_queue(ignore_window=True, max_runs=1)
    stuck = seen[0][0]
    ask = [t for k, t in sent if k == "blocker"][-1]
    assert f"#{stuck} " in ask and "asks:" in ask and "Willing to work in shifts?" in ask
    assert engine.resume_blocked(stuck, "Yes", via="test").startswith("Saved to your knowledge base")
    with db.session() as s:
        assert s.get(db.Application, stuck).status == "queued"
    assert "Answer: Yes" in (config.KB_DIR / "notes.md").read_text(encoding="utf-8")
    assert engine._next_batch(5)[0] == stuck                      # resumed one goes first
    await engine.run_queue(ignore_window=True, max_runs=1)
    assert seen[1][0] == stuck and "with: Yes" in seen[1][1] and "naukri.example/job/1" in seen[1][1]
    assert engine.resume_blocked(stuck, "x") == "nothing to resume"          # applied now
