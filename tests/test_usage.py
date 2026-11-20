"""The usage governor: JobHelper must back off before it eats the user's Claude limits."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from jobhelper import db, llm, scoring, usage
from jobhelper.apply import engine


def _reading(utilization, status="allowed", kind="five_hour", resets_in=timedelta(hours=2)):
    with db.session() as s:
        s.merge(db.RateLimitState(limit_type=kind, status=status, utilization=utilization,
                                  resets_at=db.utcnow() + resets_in, updated_at=db.utcnow()))


@pytest.fixture(autouse=True)
def _clear_limits():
    with db.session() as s:
        s.query(db.RateLimitState).delete()
        s.query(db.LLMUsage).delete()
    yield


def test_no_readings_means_go():
    assert usage.allowed("scoring")[0] and usage.allowed("apply")[0] and usage.allowed("email")[0]


def test_tiers_pause_in_priority_order():
    _reading(0.65)
    assert not usage.allowed("scoring")[0] and not usage.allowed("prep")[0]
    assert usage.allowed("apply")[0] and usage.allowed("email")[0]
    _reading(0.80)
    assert not usage.allowed("apply")[0] and usage.allowed("email")[0]
    _reading(0.97)
    assert not usage.allowed("email")[0]


def test_rejected_pauses_everything_and_says_when():
    _reading(None, status="rejected", kind="seven_day")
    ok, why = usage.allowed("email")
    assert not ok and "seven day" in why and "resets" in why


def test_reset_windows_are_ignored():
    _reading(0.99, status="rejected", resets_in=timedelta(minutes=-1))
    assert usage.allowed("scoring")[0]


def test_observe_stores_sdk_rate_limit_events():
    info = SimpleNamespace(status="allowed_warning", utilization=0.82, resets_at=int((db.utcnow() + timedelta(hours=1)).timestamp()),
                           rate_limit_type="seven_day_opus")
    usage.observe(SimpleNamespace(rate_limit_info=info))
    r = {x["type"]: x for x in usage.readings()}["seven_day_opus"]
    assert r["status"] == "allowed_warning" and r["utilization"] == 0.82


def test_record_and_limit_hit():
    res = SimpleNamespace(is_error=False, num_turns=12, duration_ms=5000, errors=None, result="ok", api_error_status=None,
                          model_usage={"claude-opus-5-5": {"inputTokens": 100, "outputTokens": 50, "cacheReadInputTokens": 9000,
                                                           "cacheCreationInputTokens": 300, "costUSD": 0.42}})
    usage.record(res, task="apply", ref="app-1")
    t = usage.today()
    assert t["by_task"]["apply"]["cache_read"] == 9000 and t["apply_runs"] == 1
    usage.record(SimpleNamespace(is_error=True, num_turns=1, duration_ms=1, errors=["429 rate limit"], result="",
                                 api_error_status=429, model_usage={}, usage={}, total_cost_usd=0), task="scoring")
    assert not usage.allowed("email")[0]          # a hard limit hit pauses everything for a while


def test_daily_apply_ceiling():
    with db.session() as s:
        for i in range(usage._gov()["max_apply_runs_per_day"]):
            s.add(db.LLMUsage(task="apply", ref=f"app-{i}"))
    ok, why = usage.allowed("apply")
    assert not ok and "ceiling" in why


async def test_structured_refuses_without_calling_claude(monkeypatch):
    _reading(0.9)

    def boom(*a, **k):
        raise AssertionError("must not call Claude while paused")

    monkeypatch.setattr(llm, "query", boom)
    with pytest.raises(llm.LLMDeferred):
        await llm.structured("x", {"type": "object"}, system="s", model="m", task="scoring")


async def test_scoring_defers_and_keeps_jobs(monkeypatch):
    with db.session() as s:
        for i in range(3):
            s.add(db.Job(source="lever", external_id=str(i), url="u", title="Tech Lead", company="C", status="prefiltered"))
    sent = []

    async def deferred(*a, **k):
        raise llm.LLMDeferred("five hour window 70% used")

    async def fake_paused(task, why):
        sent.append(why)

    monkeypatch.setattr(scoring.llm, "structured", deferred)
    monkeypatch.setattr(scoring.usage, "notify_paused", fake_paused)
    out = await scoring.score_pending()
    assert out["deferred"] == 3 and out["scored"] == 0 and len(sent) == 1
    with db.session() as s:
        assert s.query(db.Job).filter(db.Job.status == "prefiltered").count() == 3


async def test_apply_queue_defers_when_paused(monkeypatch):
    _reading(0.9)
    called = []

    async def no_run(*a, **k):
        called.append(1)

    async def fake_paused(task, why):
        called.append(why)

    monkeypatch.setattr(engine.agent, "run", no_run)
    monkeypatch.setattr(engine.usage, "notify_paused", fake_paused)
    out = await engine.run_queue(ignore_window=True)
    assert "deferred" in out and 1 not in called


REAL = "Claude Code returned an error result: You've hit your 5-hour limit · resets 1:30pm (Asia/Kolkata) (exit code: 1)"


def test_recognises_claude_codes_real_limit_message():
    assert usage.LIMIT_TEXT.search(REAL)
    at = usage._parse_reset(REAL).astimezone(usage.config.tz())
    assert (at.hour, at.minute) == (13, 30)
    usage.note_limit_hit(REAL)
    ok, why = usage.allowed("email")
    assert not ok and "13:30" in why


async def test_limit_mid_run_requeues_instead_of_failing(monkeypatch):
    with db.session() as s:
        j = db.Job(source="naukri", external_id="x", url="u", title="Tech Lead", company="C", score=90.0,
                   score_detail={"location_ok": True, "red_flags": []}, status="queued_auto")
        s.add(j)
        s.flush()
        a = db.Application(job_id=j.id, portal="naukri", mode="auto", status="queued")
        s.add(a)
        s.flush()
        app_id = a.id
    sent = []

    async def ok_preflight():
        return None

    async def limited(ctx, portal, url):
        return {"status": "deferred", "notes": REAL}

    async def fake_send(text, kind="info", **_):
        sent.append(kind)
        return True

    monkeypatch.setattr(engine.agent, "preflight", ok_preflight)
    monkeypatch.setattr(engine.agent, "run", limited)
    monkeypatch.setattr(engine.notify, "send", fake_send)
    monkeypatch.setattr(usage, "notify_paused", lambda *a, **k: _noop())
    out = await engine.run_queue(ignore_window=True)
    with db.session() as s:
        assert s.get(db.Application, app_id).status == "queued"      # not "failed"
    assert out["results"][0]["status"] == "deferred" and "failure" not in sent


async def _noop():
    return None


async def test_queued_calls_recheck_the_governor(monkeypatch):
    """Calls waiting for a slot must see usage spent meanwhile, not a check made before waiting."""
    import asyncio
    from jobhelper import llm
    ran = []

    async def fake_query(**_):
        ran.append(1)
        await asyncio.sleep(0.01)
        raise RuntimeError("boom")
        yield   # pragma: no cover  (makes this an async generator)

    monkeypatch.setattr(llm, "_sem", asyncio.Semaphore(1))
    monkeypatch.setattr(llm, "query", fake_query)
    monkeypatch.setattr(llm.usage, "allowed", lambda task: (not ran, "window full"))
    out = await asyncio.gather(*(llm.structured("x", {}, system="s", model="m") for _ in range(2)),
                               return_exceptions=True)
    assert type(out[0]) is llm.LLMError and type(out[1]) is llm.LLMDeferred and len(ran) == 1
