"""Private KB files must never reach search results or employer-facing context."""
from jobhelper import config, kb


def _write(name, body):
    config.KB_DIR.mkdir(parents=True, exist_ok=True)
    (config.KB_DIR / name).write_text(f"---\ntitle: {name}\n---\n\n{body}\n", encoding="utf-8")


def test_private_files_are_not_searchable_or_in_the_brief():
    _write("resume.md", "## Skills\nPython FastAPI Kafka zebrafish-public")
    _write("private-claude-export.md", "## Salary\nWould take 30 LPA zebrafish-secret")
    _write("claims-policy.md", "# Claims policy\nNever quote 35%.")
    kb.reindex()
    hits = kb.search("zebrafish-secret 30 LPA")
    assert all(not h["file"].startswith("private-") for h in hits)
    assert any("zebrafish-public" in h["text"] for h in kb.search("zebrafish-public"))
    brief = kb.candidate_brief()
    assert "zebrafish-secret" not in brief and "zebrafish-public" in brief
    assert brief.index("Claims policy") < brief.index("zebrafish-public")   # policy first
    assert "zebrafish-secret" in kb.candidate_brief(include_private=True)
    assert "Never quote 35%" in kb.claims_policy()


def test_dashboard_profile_is_the_single_source_for_figures():
    from jobhelper import config
    path = config.profile_path()
    original = path.read_text(encoding="utf-8")
    try:
        path.write_text("compensation:\n  current_ctc_lpa: 20.5\n  expected_ctc_lpa: 30\n"
                        "  expected_ctc_lpa_whole: 45\n  expected_ctc_inr: 4500000\n", encoding="utf-8")
        c = config.profile_for_forms()["compensation"]
        assert (c["current_ctc_lpa_whole"], c["current_ctc_inr"]) == (20, 2050000)
        assert (c["expected_ctc_lpa_whole"], c["expected_ctc_inr"]) == (30, 3000000)   # stale copies ignored
        _write("claude-export.md", "* Expected CTC: 40-45 LPA")
        kb.reindex()
        brief = kb.candidate_brief()
        assert brief.startswith("<profile") and brief.index("expected_ctc_lpa: 30") < brief.index("40-45")
    finally:
        path.write_text(original, encoding="utf-8")
