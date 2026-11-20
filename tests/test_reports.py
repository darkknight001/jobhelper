"""Daily/weekly reports and the API Hermes uses."""
import dataclasses
from datetime import timedelta

from fastapi.testclient import TestClient

from jobhelper import config, db, summary


def _seed():
    now = db.utcnow()
    with db.session() as s:
        for i, (days_ago, status) in enumerate([(0, "applied"), (2, "interview"), (5, "rejected"), (12, "applied")]):
            j = db.Job(source="naukri", external_id=str(i), url="u", title=f"Tech Lead {i}", company=f"Co{i}")
            s.add(j)
            s.flush()
            s.add(db.Application(job_id=j.id, portal="naukri", mode="auto", status=status,
                                 applied_at=now - timedelta(days=days_ago), created_at=now - timedelta(days=days_ago)))


def test_weekly_report_covers_only_the_window():
    _seed()
    apps = summary.applications_in_period(7)
    assert {a["company"] for a in apps} == {"Co0", "Co1", "Co2"}
    text = summary.period_text(7)
    assert "last 7 days" in text and "Co1" in text and "Co3" not in text
    assert "1 interview" in text and "1 rejected" in text


def test_api_summary_and_applications_need_token(monkeypatch):
    _seed()
    patched = dataclasses.replace(config.secrets(), api_token="t0k")
    monkeypatch.setattr(config, "secrets", lambda: patched)
    from jobhelper.web.app import app
    c = TestClient(app)   # no `with`: skip lifespan (scheduler, browser)
    assert c.get("/api/summary?days=7").status_code == 401
    h = {"Authorization": "Bearer t0k"}
    r = c.get("/api/summary?days=7", headers=h)
    assert r.status_code == 200 and "last 7 days" in r.text
    r = c.get("/api/applications?days=7&status=interview", headers=h)
    assert [a["company"] for a in r.json()] == ["Co1"]


def test_stale_profile_save_is_refused(monkeypatch):
    patched = dataclasses.replace(config.secrets(), dashboard_password="pw")
    monkeypatch.setattr(config, "secrets", lambda: patched)
    from jobhelper.web.app import _profile_version, app
    c = TestClient(app, base_url="https://testserver")   # the session cookie is HTTPS-only in production
    assert c.post("/login", data={"password": "pw", "next": "/"}, follow_redirects=False).status_code == 303
    path = config.profile_path()
    opened = path.read_text(encoding="utf-8")
    version = _profile_version(opened)
    path.write_text(opened + "\n# changed by JobHelper after the page was opened\n", encoding="utf-8")
    r = c.post("/kb/profile", data={"profile_text": "name: {}\n", "profile_version": version}, follow_redirects=False)
    assert "Not+saved" in r.headers["location"]
    assert "changed by JobHelper" in path.read_text(encoding="utf-8")       # nothing overwritten
    fresh = _profile_version(path.read_text(encoding="utf-8"))
    edited = path.read_text(encoding="utf-8") + "\n# my edit\n"
    r = c.post("/kb/profile", data={"profile_text": edited, "profile_version": fresh}, follow_redirects=False)
    assert "Profile+saved" in r.headers["location"] and "# my edit" in path.read_text(encoding="utf-8")


def test_prep_page_drops_html_and_redirects_stay_local(monkeypatch):
    patched = dataclasses.replace(config.secrets(), dashboard_password="pw")
    monkeypatch.setattr(config, "secrets", lambda: patched)
    from jobhelper import prep
    from jobhelper.web.app import _local, app
    c = TestClient(app, base_url="https://testserver")
    r = c.post("/login", data={"password": "pw", "next": "//evil.com"}, follow_redirects=False)
    assert r.headers["location"] == "/"
    assert [_local(u) for u in ("/jobs", "//evil.com", r"/\evil.com", "https://evil.com")] == ["/jobs", "/", "/", "/"]
    _seed()
    with db.session() as s:
        app_id = s.query(db.Application).first().id
    prep.path_for(app_id).parent.mkdir(parents=True, exist_ok=True)
    prep.path_for(app_id).write_text("# Brief\n<img src=x onerror=alert(1)>\n\n**ok** <script>x()</script>",
                                     encoding="utf-8")
    r = c.get(f"/prep/{app_id}")
    assert "<strong>ok</strong>" in r.text and "<img" not in r.text and "<script>" not in r.text
    assert "script-src 'none'" in r.headers["content-security-policy"]


def test_lists_paginate_with_position_and_keep_filters(monkeypatch):
    patched = dataclasses.replace(config.secrets(), dashboard_password="pw")
    monkeypatch.setattr(config, "secrets", lambda: patched)
    from jobhelper.web.app import app
    c = TestClient(app, base_url="https://testserver")
    c.post("/login", data={"password": "pw", "next": "/"}, follow_redirects=False)
    with db.session() as s:
        for i in range(55):
            j = db.Job(source="naukri", external_id=f"p{i}", url="u", title=f"Tech Lead {i}",
                       company="Acme" if i < 3 else f"Co{i}", status="review", score=70.0)
            s.add(j)
            s.flush()
            s.add(db.Application(job_id=j.id, portal="naukri", mode="auto", status="queued"))
    one = c.get("/applications").text
    assert "1–50 of 55 applications" in one and "page=2" in one and "← Newer" not in one
    two = c.get("/applications?page=2").text
    assert "51–55 of 55 applications" in two and "Older →" not in two
    assert "51–55 of 55" in c.get("/applications?page=99").text          # past the end: last page
    acme = c.get("/applications?status=queued&q=acme").text
    assert "1–3 of 3 applications" in acme
    rev = c.get("/review?page=2").text
    assert "21–40 of 55 jobs to review" in rev and 'name="back" value="/review?page=2"' in rev
    assert "1–50 of 55 jobs" in c.get("/jobs?status=review").text


def test_summary_flags_company_site_applications_with_no_mail():
    old = db.utcnow() - timedelta(days=5)
    with db.session() as s:
        for i, (url, mailed) in enumerate([("https://acme.wd3.myworkdayjobs.com/x", False),
                                           ("https://jobs.ashbyhq.com/beta/1", True),
                                           ("https://www.linkedin.com/jobs/view/9", False)]):
            j = db.Job(source="linkedin", external_id=f"sl{i}", url="u", title=f"Lead {i}", company=f"Silent{i}")
            s.add(j)
            s.flush()
            a = db.Application(job_id=j.id, portal="linkedin", mode="auto", status="applied", applied_at=old, final_url=url)
            s.add(a)
            s.flush()
            if mailed:
                db.add_event(s, "mail_application_received", "got it", application=a)
    text = summary.daily_text()
    assert "No confirmation email yet" in text and "Silent0" in text
    assert "Silent1" not in text and "Silent2" not in text      # mailed / applied on the portal itself
