"""The user's email rules, enforced in code (see jobhelper/mail.py)."""
import pytest

from jobhelper import db, mail


def _conn():
    # An unconnected instance: _command must refuse before any network I/O happens.
    return object.__new__(mail.ReadOnlyIMAP)


@pytest.mark.parametrize("cmd", ["STORE", "COPY", "MOVE", "EXPUNGE", "APPEND", "DELETE", "CREATE", "RENAME",
                                 "SELECT", "CLOSE", "SETACL", "XYZZY"])
def test_mutating_imap_commands_are_refused(cmd):
    with pytest.raises(mail.GuardrailViolation):
        _conn()._command(cmd, "1", "+FLAGS", "(\\Deleted)")


@pytest.mark.parametrize("sub", ["STORE", "COPY", "MOVE", "EXPUNGE"])
def test_mutating_uid_subcommands_are_refused(sub):
    with pytest.raises(mail.GuardrailViolation):
        _conn()._command("UID", sub, "1", "+FLAGS", "(\\Seen)")


def test_fetch_must_peek():
    with pytest.raises(mail.GuardrailViolation):
        _conn()._command("FETCH", "1", "(BODY[])")


def test_sender_allowlist_matching():
    allowed = {"naukri.com", "greenhouse.io", "recruiter@acme.com"}
    assert mail.sender_allowed("jobalert@naukri.com", allowed)
    assert mail.sender_allowed("no-reply@us.greenhouse.io", allowed)
    assert mail.sender_allowed("recruiter@acme.com", allowed)
    assert not mail.sender_allowed("friend@gmail.com", allowed)
    assert not mail.sender_allowed("bank@hdfcbank.com", allowed)
    assert not mail.sender_allowed("evil@naukri.com.evil.io", allowed)
    assert not mail.sender_allowed("someone@notnaukri.com", allowed)


async def test_search_refuses_non_job_senders():
    with pytest.raises(mail.GuardrailViolation):
        await mail.search(["gmail.com"])
    with pytest.raises(mail.GuardrailViolation):
        await mail.search(["mom@gmail.com"])
    with pytest.raises(mail.GuardrailViolation):
        await mail.search([])


async def test_verification_lookup_needs_a_site_domain():
    with pytest.raises(mail.GuardrailViolation):
        await mail.latest_verification("")


def test_applied_company_domains_join_the_allowlist():
    with db.session() as s:
        job = db.Job(source="naukri", external_id="1", url="u", title="t", company="Acme")
        s.add(job)
        s.flush()
        s.add(db.Application(job_id=job.id, portal="naukri", mode="auto", status="applied",
                             final_url="https://careers.acme.co.in/jobs/42"))
    assert "acme.co.in" in mail.allowlist()


def test_registrable_domain():
    assert mail.registrable("careers.acme.co.in") == "acme.co.in"
    assert mail.registrable("jobs.lever.co") == "lever.co"
    assert mail.registrable("x@mail.example.com") == "example.com"


async def test_mailbox_providers_never_join_the_allowlist():
    # A Google Form application and an agent asked (by a page) to make a gmail.com login.
    with db.session() as s:
        job = db.Job(source="naukri", external_id="2", url="u", title="t", company="Acme")
        s.add(job)
        s.flush()
        s.add(db.Application(job_id=job.id, portal="company", mode="auto", status="applied",
                             final_url="https://docs.google.com/forms/d/e/x/viewform"))
        s.add(db.SiteLogin(domain="gmail.com", username="me", password_enc="x"))
    mail.allow_domain_temporarily("accounts.google.com")
    allowed = mail.allowlist()
    assert not allowed & mail.NEVER_AUTO and "naukri.com" in allowed
    with pytest.raises(mail.GuardrailViolation):
        await mail.latest_verification("gmail.com")


def test_company_domain_is_learned_only_when_it_carries_the_company_name():
    assert mail.company_tokens("Larkspur Labs Pvt Ltd") == ["larkspur", "labs"]
    assert mail.company_tokens('Evil" OR from:gmail.com') == ["evil", "from", "gmail"]   # no Gmail syntax survives
    assert mail.company_domain("careers@larkspur.xyz", "Larkspur Labs") == "larkspur.xyz"
    assert mail.company_domain("no-reply@talent.quillpay.com", "QuillPay") == "quillpay.com"
    assert mail.company_domain("hr.larkspur@gmail.com", "Larkspur Labs") is None          # mailbox provider
    assert mail.company_domain("jobs@randomrecruiter.com", "Larkspur Labs") is None
    assert mail.company_domain("x@india.com", "Acme India Private Limited") is None       # generic words only


async def test_learning_reads_headers_only_and_feeds_the_allowlist(monkeypatch):
    from jobhelper import config, tracking
    import dataclasses
    fetched = []

    class FakeIMAP:
        def __init__(self, *a, **k): pass
        def login(self, *a): pass
        def select(self, *a): pass
        def logout(self): pass
        def search(self, _charset, _kind, query):
            return "OK", [b"1 2" if "larkspur" in query else b""]
        def fetch(self, i, what):
            fetched.append(what)
            sender = {b"1": "Larkspur Careers <careers@larkspur.xyz>", b"2": "Ads <ads@promo.io>"}[i]
            return "OK", [(b"hdr", f"From: {sender}\r\nSubject: Your application to Larkspur\r\n\r\n".encode())]

    monkeypatch.setattr(mail, "ReadOnlyIMAP", FakeIMAP)
    patched = dataclasses.replace(config.secrets(), gmail_address="me@x.com", gmail_app_password="pw")
    monkeypatch.setattr(config, "secrets", lambda: patched)
    polled = []

    async def fake_poll(days=3, senders=None):
        polled.append(senders)
        return {"fetched": 0}

    monkeypatch.setattr(tracking, "poll", fake_poll)
    with db.session() as s:
        for i, co in enumerate(("Larkspur Labs", "Nobody Corp")):
            j = db.Job(source="linkedin", external_id=f"cs{i}", url="u", title="Tech Lead", company=co, status="applied")
            s.add(j)
            s.flush()
            s.add(db.Application(job_id=j.id, portal="linkedin", mode="auto", status="applied", applied_at=db.utcnow()))
    out = await tracking.learn_senders()
    assert out["learned"] == {"Larkspur Labs": "larkspur.xyz"} and out["checked"] == 2
    assert all("HEADER.FIELDS (FROM SUBJECT)" in w for w in fetched)                        # never a body
    assert "larkspur.xyz" in mail.allowlist() and polled == [["larkspur.xyz"]]
    assert (await tracking.learn_senders())["checked"] == 0                                 # not re-checked for a week


def test_company_mail_is_opened_only_for_job_subjects(monkeypatch):
    opened = []
    msgs = {b"1": ("news@mastercard.com", "Your card statement is ready"),
            b"2": ("careers@mastercard.com", "Your application for Senior Engineer"),
            b"3": ("no-reply@us.greenhouse-mail.io", "Security code for your account"),
            b"4": ("noreply@okta.com", "Okta: new sign-in to your account")}

    class FakeIMAP:
        def __init__(self, *a, **k): pass
        def login(self, *a): pass
        def select(self, *a): pass
        def logout(self): pass
        def search(self, *_):
            return "OK", [b"1 2 3 4"]
        def fetch(self, i, what):
            sender, subject = msgs[i]
            if "HEADER.FIELDS" in what:
                return "OK", [(b"h", f"From: {sender}\r\nSubject: {subject}\r\n\r\n".encode())]
            opened.append(i)
            return "OK", [(b"b", f"From: {sender}\r\nSubject: {subject}\r\n\r\nbody".encode())]

    import dataclasses
    from jobhelper import config
    patched = dataclasses.replace(config.secrets(), gmail_address="me@x.com", gmail_app_password="pw")
    monkeypatch.setattr(config, "secrets", lambda: patched)
    monkeypatch.setattr(mail, "ReadOnlyIMAP", FakeIMAP)
    got = mail._search_sync(["mastercard.com", "okta.com", "greenhouse-mail.io"], "", 10,
                            allowed={"mastercard.com", "okta.com", "greenhouse-mail.io"}, trusted={"greenhouse-mail.io"})
    assert sorted(opened) == [b"2", b"3"]                     # job subject, or a trusted ATS; never the rest
    assert {m.sender for m in got} == {"careers@mastercard.com", "no-reply@us.greenhouse-mail.io"}
