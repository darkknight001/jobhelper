"""Source parsers, prefilter rules, Hermes signature, alert extraction, tracking state."""
import hashlib
import hmac

import pytest

from jobhelper import config, db, mail, notify, prefilter, tracking
from jobhelper.sources import ats, email_alerts, indeed, linkedin, naukri

S = config.settings()


@pytest.mark.parametrize("title,ok", [
    ("Tech Lead - Python", True), ("Lead Backend Engineer", True), ("Senior Software Engineer", True),
    ("Staff Engineer", True), ("Principal Software Engineer", True), ("GenAI Engineer", True),
    ("Senior Java Developer", False), ("Lead Software Engineer (Java/Python)", True),
    ("Senior Solutions Architect", False), ("QA Lead", False), ("Frontend Tech Lead", False),
    ("Software Engineering Intern", False), ("Account Manager", False), ("Data Analyst", False),
    ("Forward Deployed Engineer", True), ("Forward-Deployed AI Engineer", True), ("FDE - Python", True),
    ("Solutions Engineer", False),
])
def test_title_filter(title, ok):
    assert prefilter.check_title(title, S).ok is ok


@pytest.mark.parametrize("loc,remote,ok", [
    ("Bengaluru, India", False, True), ("Gurugram", False, True), ("Noida", False, True),
    ("Mumbai, Maharashtra", False, True), ("Navi Mumbai", False, True),
    ("Remote", True, True), ("Remote, India", True, True), ("Remote - Texas", True, False),
    ("SF, NY, Remote", True, False), ("Kochi, India", False, False), ("San Francisco, CA", False, False),
    (None, False, True),
])
def test_location_filter(loc, remote, ok):
    assert prefilter.check_location(loc, remote, S).ok is ok


@pytest.mark.parametrize("exp,ok", [("5-10 Yrs", True), ("2-4 years", False), ("12+ years", False),
                                    ("8-12 Yrs", True), (None, True)])
def test_experience_filter(exp, ok):
    assert prefilter.check_experience(exp, S).ok is ok


def test_hermes_signature_matches_hermes_algorithm():
    body, ts, secret = b'{"text":"hi"}', "1794132432", "s3cret"
    expected = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    assert notify.sign(secret, body, ts) == expected


def test_long_messages_are_chunked():
    parts = notify._chunks("line\n" * 2000)
    assert len(parts) > 1 and all(len(p) <= notify.TELEGRAM_LIMIT + 10 for p in parts)


def test_greenhouse_lever_ashby_parsers():
    gh = ats.parse_greenhouse("acme", {"jobs": [{"id": 7, "title": "Staff Engineer", "absolute_url": "https://x/7",
                                                 "location": {"name": "Bengaluru"}, "content": "&lt;p&gt;Python&lt;/p&gt;",
                                                 "company_name": "Acme"}]})
    assert gh[0].external_id == "acme:7" and gh[0].description == "Python" and gh[0].company == "Acme"
    lv = ats.parse_lever("acme", [{"id": "a", "text": "Tech Lead", "hostedUrl": "h", "applyUrl": "ap",
                                   "categories": {"location": "Remote"}, "workplaceType": "remote",
                                   "descriptionPlain": "Do things", "createdAt": 1790000000000}])
    assert lv[0].remote and lv[0].apply_url == "ap"
    ab = ats.parse_ashby("acme", {"jobs": [{"id": "z", "title": "AI Engineer", "jobUrl": "j", "applyUrl": "a",
                                            "location": "Bengaluru", "isRemote": False, "descriptionPlain": "LLMs"}]})
    assert ab[0].title == "AI Engineer"


def test_naukri_search_json():
    data = {"jobDetails": [{"jobId": "123456789012", "title": "Tech Lead", "companyName": "Acme",
                            "jdURL": "/job-listings-tech-lead-acme-123456789012",
                            "placeholders": [{"type": "experience", "label": "6-10 Yrs"},
                                             {"type": "location", "label": "Noida"}],
                            "tagsAndSkills": "python,kafka", "createdDate": 1790000000000}]}
    p = naukri.parse_search_json(data)[0]
    assert p.url.startswith("https://www.naukri.com/job-listings") and p.experience == "6-10 Yrs"
    assert p.skills == ["python", "kafka"] and p.location == "Noida"


def test_indeed_cards():
    p = indeed.parse_cards([{"jobkey": "abcdef0123456789", "displayTitle": "Senior Backend Engineer",
                             "company": "Acme", "formattedLocation": "Gurgaon, Haryana", "indeedApplyEnabled": True}])[0]
    assert p.easy_apply and p.url.endswith("jk=abcdef0123456789")


def test_linkedin_card_html():
    html = ('<li><div class="base-card" data-entity-urn="urn:li:jobPosting:4486957402">'
            '<h3 class="base-search-card__title">Lead Software Engineer</h3>'
            '<h4 class="base-search-card__subtitle"><a>Acme</a></h4>'
            '<span class="job-search-card__location">Bengaluru, Karnataka, India</span>'
            '<time datetime="2026-11-06">1 day ago</time></div></li>')
    p = linkedin.parse_search(html)[0]
    assert p.external_id == "4486957402" and p.company == "Acme"


def test_alert_email_extraction():
    m = mail.Mail(message_id="<1>", sender="jobalert@naukri.com", sender_name="Naukri",
                  subject="Priority Applicant: 5 new jobs", date=None, text="",
                  html='<a href="https://www.naukri.com/job-listings-tech-lead-acme-noida-6-to-10-years-091126500123?src=alert">'
                       'Tech Lead - Python</a> <a href="https://www.linkedin.com/comm/jobs/view/4486957402/?x=1">View</a>')
    got = {(p.source, p.external_id): p for p in email_alerts.extract(m)}
    assert ("naukri", "091126500123") in got and got[("naukri", "091126500123")].priority
    assert got[("naukri", "091126500123")].title == "Tech Lead - Python"
    assert ("linkedin", "4486957402") in got and got[("linkedin", "4486957402")].title == ""


def test_status_mail_is_left_for_the_tracker():
    assert email_alerts.STATUS_SUBJECT.search("Your application was viewed by Acme")
    assert email_alerts.STATUS_SUBJECT.search("Interview invitation: Tech Lead")
    assert not email_alerts.STATUS_SUBJECT.search("10 new jobs matching Tech Lead Python")


async def test_tracking_status_only_moves_forward(monkeypatch):
    sent = []

    async def fake_send(text, kind="info", **_):
        sent.append(text)
        return True

    async def fake_prep(app_id, trigger, force=False):
        return True

    monkeypatch.setattr(tracking.notify, "send", fake_send)
    monkeypatch.setattr(tracking.prep, "ensure_brief", fake_prep)
    with db.session() as s:
        j = db.Job(source="lever", external_id="1", url="u", title="Tech Lead", company="Acme")
        s.add(j)
        s.flush()
        a = db.Application(job_id=j.id, portal="company", mode="auto", status="applied")
        s.add(a)
        s.flush()
        app_id = a.id

    def result(kind):
        return {"kind": kind, "company": "Acme", "role": "Tech Lead", "application_match": app_id, "confidence": 0.9,
                "summary": f"{kind} mail", "action_required": kind == "interview", "urgency": "normal",
                "event": {"when": "Thu 3 PM"}, "questions": [], "suggested_reply": ""}

    def m(i):
        return mail.Mail(message_id=f"<{i}>", sender="r@acme.com", sender_name="R", subject="s", date=None, text="t", html="")

    stats, preps = {"alerts": 0, "prep": 0}, []
    await tracking._apply_result(m(1), result("interview"), stats, preps)
    await tracking._apply_result(m(2), result("viewed"), stats, preps)
    with db.session() as s:
        assert s.get(db.Application, app_id).status == "interview"
    assert stats["alerts"] == 1 and [a for a, _ in preps] == [app_id]   # brief written after all alerts
    assert "I haven't replied or accepted anything." in sent[0]


def test_eval_fixtures_parse():
    import yaml
    from pathlib import Path
    fx = Path(__file__).parent / "fixtures"
    cases = yaml.safe_load((fx / "scoring_cases.yaml").read_text(encoding="utf-8"))
    emails = yaml.safe_load((fx / "emails.yaml").read_text(encoding="utf-8"))
    assert len(cases) == 8 and all({"title", "expect"} <= set(c) for c in cases)
    assert len(emails) == 6 and all({"from", "subject", "body", "expect"} <= set(e) for e in emails)


def test_tracker_leaves_job_alerts_to_job_search():
    def m(subject, html):
        return mail.Mail(message_id="<a>", sender="jobalerts-noreply@linkedin.com", sender_name="LinkedIn",
                         subject=subject, date=None, text="", html=html)
    link = '<a href="https://www.linkedin.com/comm/jobs/view/4486957402/">Tech Lead</a>'
    assert tracking.is_job_alert(m("10 new jobs for Tech Lead", link))
    assert not tracking.is_job_alert(m("Your application was viewed by Acme", link))   # status mail
    assert not tracking.is_job_alert(m("Interview next week", "no job links here"))
