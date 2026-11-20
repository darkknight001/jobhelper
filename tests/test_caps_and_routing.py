"""Daily per-portal caps and score routing."""
from jobhelper import config, db, scoring
from jobhelper.apply import engine


def _job(s, i, source="naukri", score=90.0, **detail):
    j = db.Job(source=source, external_id=str(i), url=f"u{i}", title=f"Tech Lead {i}", company="C",
               score=score, score_detail={"location_ok": True, "red_flags": [], **detail}, status="prefiltered")
    s.add(j)
    s.flush()
    return j


def test_routing_thresholds():
    with db.session() as s:
        assert scoring.route(s, _job(s, 1, score=92)) == "queued_auto"
        assert scoring.route(s, _job(s, 2, score=70)) == "review"
        assert scoring.route(s, _job(s, 3, score=40)) == "skipped"
        assert scoring.route(s, _job(s, 4, score=95, red_flags=["staffing agency"])) == "review"
        assert scoring.route(s, _job(s, 5, score=95, location_ok=False)) == "review"


def test_auto_apply_over_cap_goes_to_review():
    cap = config.settings()["apply"]["daily_cap_per_portal"]
    with db.session() as s:
        routes = [scoring.route(s, _job(s, i, score=95)) for i in range(cap + 3)]
    assert routes.count("queued_auto") == cap
    assert routes.count("review") == 3


def test_caps_are_per_portal():
    cap = config.settings()["apply"]["daily_cap_per_portal"]
    with db.session() as s:
        for i in range(cap):
            j = _job(s, i)
            s.add(db.Application(job_id=j.id, portal="naukri", mode="auto", status="applied", applied_at=db.utcnow()))
        assert scoring.route(s, _job(s, 100, score=95)) == "review"
        assert scoring.route(s, _job(s, 101, source="linkedin", score=95)) == "queued_auto"


def test_queue_runner_respects_cap_including_approved():
    cap = config.settings()["apply"]["daily_cap_per_portal"]
    with db.session() as s:
        for i in range(cap - 2):
            j = _job(s, i)
            s.add(db.Application(job_id=j.id, portal="naukri", mode="auto", status="applied", applied_at=db.utcnow()))
        for i in range(5):
            j = _job(s, 50 + i)
            s.add(db.Application(job_id=j.id, portal="naukri", mode="approved", status="queued"))
        j = _job(s, 99, source="indeed")
        s.add(db.Application(job_id=j.id, portal="indeed", mode="auto", status="queued"))
    picked = engine._next_batch(limit=20)
    with db.session() as s:
        portals = [s.get(db.Application, i).portal for i in picked]
    assert portals.count("naukri") == 2
    assert portals.count("indeed") == 1


def test_approve_and_dismiss():
    with db.session() as s:
        j = _job(s, 1, score=70)
        j.status = "review"
        jid = j.id
    assert "queued" in engine.approve(jid)
    with db.session() as s:
        assert s.get(db.Job, jid).status == "approved"
        assert len(s.get(db.Job, jid).applications) == 1
    assert "not awaiting review" in engine.approve(jid)


async def test_broken_browser_pauses_queue_with_one_alert(monkeypatch):
    sent = []

    async def bad_preflight():
        return "Playwright requires Node.js 20 or higher"

    async def fake_send(text, kind="info", **_):
        sent.append(text)
        return True

    monkeypatch.setattr(engine.agent, "preflight", bad_preflight)
    monkeypatch.setattr(engine.notify, "send", fake_send)
    with db.session() as s:
        j = _job(s, 1)
        s.add(db.Application(job_id=j.id, portal="naukri", mode="auto", status="queued"))
    first = await engine.run_queue(ignore_window=True)
    second = await engine.run_queue(ignore_window=True)
    assert "Node.js 20" in first["paused"] and "paused" in second
    with db.session() as s:
        assert [a.status for a in s.query(db.Application)] == ["queued"]   # untouched, not needs_user
    assert len(sent) == 1                                                  # one alert, not one per run


async def test_postings_older_than_a_week_are_never_applied():
    from datetime import timedelta
    from jobhelper import pipeline
    from jobhelper.apply import engine
    old = db.utcnow() - timedelta(days=8)
    with db.session() as s:
        j = db.Job(source="lever", external_id="old1", url="u", title="Tech Lead - Python", company="Acme",
                   location="Bengaluru", posted_at=old, status="approved")
        undated = db.Job(source="naukri", external_id="new1", url="u", title="Tech Lead - Python",
                         company="Beta", location="Bengaluru", status="new")
        stale = db.Job(source="lever", external_id="old2", url="u", title="Tech Lead - Python", company="Gamma",
                       location="Bengaluru", posted_at=old, status="new")
        s.add_all([j, undated, stale])
        s.flush()
        a = db.Application(job_id=j.id, portal="company", mode="approved", status="queued")
        s.add(a)
        s.flush()
        app_id, undated_id, stale_id = a.id, undated.id, stale.id
    assert (await engine.apply_one(app_id))["status"] == "not_eligible"     # no agent run
    pipeline.prefilter_new()
    with db.session() as s:
        assert s.get(db.Application, app_id).status == "not_eligible"
        assert s.get(db.Job, undated_id).status == "prefiltered"           # just seen: fresh
        assert s.get(db.Job, stale_id).filter_reason == "posted over 7 days ago"


def test_company_rating_decides_after_scoring():
    with db.session() as s:
        for name, overall in (("five", 5), ("three", 3)):
            s.add(db.CompanyRating(name=name, overall=overall, detail={"scores": {}}))
        s.flush()

        def job(i, company, score, **detail):
            j = _job(s, 200 + i, score=score, **detail)
            j.company = company
            return j
        assert scoring.route(s, job(1, "Five", 80)) == "queued_auto"          # rated 5: auto from 80
        assert scoring.route(s, job(2, "Five", 79)) == "review"
        assert scoring.route(s, job(3, "Five", 90, red_flags=["agency"])) == "review"
        assert scoring.route(s, job(4, "Three", 84)) == "review"              # rated 3: the usual 85
        assert scoring.route(s, job(5, "Three", 85)) == "queued_auto"
        assert scoring.route(s, job(6, "Unrated", 84)) == "review"            # unrated: the usual 85
