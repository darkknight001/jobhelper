"""Portal status: wording → status, forward-only updates, matched by the portal's job id."""
from jobhelper import db, portal_status as ps


def _naukri_item(job_id, *steps):
    return {"jobId": job_id, "status": [{"statusValue": v} for v in steps]}


def test_portal_wording_maps_to_status():
    assert ps.status_of("Application Sent") is None and ps.status_of("Applied") is None
    assert ps.status_of("Resume Viewed") == ps.status_of("Application viewed") == "viewed"
    assert ps.status_of("Resume downloaded") == "viewed"
    assert ps.status_of("Shortlisted") == "shortlisted"
    assert ps.status_of("Not Shortlisted") == "rejected"                 # not read as shortlisted
    assert ps.status_of("Applied 1w ago (No longer accepting applications)") is None   # posting closed, not you


def test_naukri_takes_the_furthest_step_and_linkedin_reads_cards():
    data = {"applyDetails": [_naukri_item(1, "Applied", "Application Sent", "Application Viewed", "Shortlisted"),
                             _naukri_item(2, "Applied", "Application Sent"),
                             _naukri_item(3, "Applied", "Resume Viewed")]}
    assert ps.naukri_statuses(data) == {"1": ("shortlisted", "Shortlisted"), "3": ("viewed", "Resume Viewed")}
    cards = {"4489091302": "Lead Backend Engineer MeshDefend · Bengaluru Applied 1w ago(Posted 1w ago)Application viewed Notes",
             "4490651712": "Senior Software Engineer Cloudwick Applied 3h ago(Posted 3d ago)Notes Did you hear back?YesNo"}
    assert ps.linkedin_statuses(cards) == {"4489091302": ("viewed", "Application viewed")}


def test_record_moves_forward_only_and_logs_an_event():
    with db.session() as s:
        ids = {}
        for ext, status in (("A", "applied"), ("B", "shortlisted"), ("C", "skipped_by_user")):
            j = db.Job(source="naukri", external_id=ext, url="u", title=f"Lead {ext}", company=f"Co{ext}", status="applied")
            s.add(j)
            s.flush()
            a = db.Application(job_id=j.id, portal="naukri", mode="auto", status=status)
            s.add(a)
            s.flush()
            ids[ext] = a.id
    moved = ps.record("naukri", {"A": ("viewed", "Resume Viewed"), "B": ("viewed", "Resume Viewed"),
                                 "C": ("viewed", "Resume Viewed"), "Z": ("shortlisted", "Shortlisted")})
    assert [(c["application"], c["status"]) for c in moved] == [(ids["A"], "viewed")]
    with db.session() as s:
        assert s.get(db.Application, ids["A"]).status == "viewed"
        assert s.get(db.Application, ids["B"]).status == "shortlisted"            # never moves back
        assert s.get(db.Application, ids["C"]).status == "skipped_by_user"        # closed ones untouched
        ev = s.query(db.Event).filter_by(kind="portal_viewed").one()
        assert ev.summary == "Naukri shows: Resume Viewed"
    assert ps.record("naukri", {"A": ("viewed", "Resume Viewed")}) == []          # nothing twice
