"""Company ratings: 1/3/5 per criterion, name matching, and rated-1 companies filtered before scoring."""
from jobhelper import companies, db

BANDS = [3.3, 4.0]
SAMPLE = {"workLifeRating": 3.4, "overallCompanyRating": 3.4, "companyCultureRating": 3.2,
           "compensationBenefitsRating": 3.1, "skillDevelopmentRating": 3.1, "workSatisfactionRating": 3,
           "careerGrowthRating": 2.7}


def test_grade_maps_the_four_criteria_to_1_3_5():
    overall, d = companies.grade(SAMPLE, BANDS)
    assert d["scores"] == {"culture": 1, "benefits": 1, "work": 1, "work_life": 3}
    assert overall == 1
    good = {"companyCultureRating": 4.3, "compensationBenefitsRating": 4.1, "workSatisfactionRating": 4.0,
            "skillDevelopmentRating": 4.2, "workLifeRating": 4.4}
    assert companies.grade(good, BANDS)[0] == 5
    assert companies.grade({"workLifeRating": 2.0}, BANDS)[0] is None          # too little data


def test_parse_and_name_matching():
    html = 'x "ratingsData":{"workLifeRating":3.4,"companyCultureRating":3.2},"industryAvg":{}'
    assert companies.parse_ratings(html) == {"workLifeRating": 3.4, "companyCultureRating": 3.2}
    cands = [{"name": "Steril Gene", "type": "company", "reviewCount": 200, "url": "x"},
             {"name": "Sopra Steria", "type": "company", "reviewCount": 2303, "url": "sopra-steria"}]
    assert companies.pick("Sopra Steria India Pvt. Ltd.", cands, 20)["url"] == "sopra-steria"
    assert companies.pick("Round1 Jobs", cands, 20) is None
    assert companies.pick("Sopra Steria", [{**cands[1], "reviewCount": 5}], 20) is None   # too few reviews
    co = lambda n, r=500: {"name": n, "type": "company", "reviewCount": r, "url": n}  # noqa: E731
    assert companies.pick("JPMorganChase", [co("JPMorgan Chase & Co.")], 20)["name"] == "JPMorgan Chase & Co."
    assert companies.pick("A.P. Moller - Maersk", [co("Maersk"), co("Maersk Line")], 20)["name"] == "Maersk"
    assert companies.pick("Relanto", [co("Relanto Global", 77), co("Relaxo Footwear")], 20)["name"] == "Relanto Global"
    assert companies.pick("Hudson Data", [co("Hudson Data LLC", 6), co("NTT DATA")], 20) is None
    assert companies.pick("LearnTube.ai", [co("[24]7.ai"), co("Kiya.ai")], 20) is None


async def test_rated_1_companies_are_filtered_before_scoring(monkeypatch):
    async def fake_fetch(client, name):
        return (1, {"scores": {"culture": 1, "benefits": 1, "work": 1, "work_life": 3}}) if "Bad" in name \
            else (None, {"note": "no match"})

    monkeypatch.setattr(companies, "fetch", fake_fetch)
    with db.session() as s:
        for i, co in enumerate(("Bad Corp Pvt Ltd", "Unknown Startup")):
            s.add(db.Job(source="naukri", external_id=f"c{i}", url="u", title="Tech Lead", company=co, status="prefiltered"))
    out = await companies.screen_prefiltered()
    assert out == {"looked_up": 2, "filtered_out": 1}
    with db.session() as s:
        bad = s.query(db.Job).filter_by(company="Bad Corp Pvt Ltd").one()
        assert bad.status == "filtered_out" and "company rated 1/5" in bad.filter_reason
        assert s.query(db.Job).filter_by(company="Unknown Startup").one().status == "prefiltered"
    assert (await companies.screen_prefiltered())["looked_up"] == 0              # cached


def test_rating_shows_on_job_and_application_pages(monkeypatch):
    import dataclasses

    from fastapi.testclient import TestClient

    from jobhelper import config
    patched = dataclasses.replace(config.secrets(), dashboard_password="pw")
    monkeypatch.setattr(config, "secrets", lambda: patched)
    from jobhelper.web.app import app
    c = TestClient(app, base_url="https://testserver")
    c.post("/login", data={"password": "pw", "next": "/"}, follow_redirects=False)
    with db.session() as s:
        s.add(db.CompanyRating(name="acme", overall=3, detail={"scores": {"culture": 3, "benefits": 1, "work": 3, "work_life": 5}}))
        j = db.Job(source="naukri", external_id="cr1", url="u", title="Tech Lead", company="Acme", status="applied")
        s.add(j)
        s.flush()
        a = db.Application(job_id=j.id, portal="naukri", mode="auto", status="applied")
        s.add(a)
        s.flush()
        jid, aid = j.id, a.id
    want = "3/5 (culture 3, benefits 1, type of work 3, work-life 5)"
    assert want in c.get(f"/applications/{aid}").text
    assert want in c.get(f"/jobs/{jid}").text
    assert 'class="crate r3"' in c.get("/jobs?status=applied").text
    assert 'class="crate r3"' in c.get("/applications").text
