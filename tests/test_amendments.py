import json
from pathlib import Path

import pytest

from src.amendments import (
    detect_document_role,
    extract_explicit_deadline_amendments,
    match_amendment_to_obligations,
)
from src.demo import load_demo_data, DATA, DOCS
from src.store import (
    add_amendment_candidate,
    add_document,
    add_obligation,
    get_amendment_candidate,
    initialize,
    list_amendment_candidates,
    list_obligations,
    review_amendment_candidate,
)


BASE_CANDIDATE = {
    "investor": "LP-A",
    "fund": "ABC Private Equity Fund IV",
    "obligation_type": "reporting",
    "deliverables": ["Unaudited quarterly financial statements", "Portfolio exposure schedule"],
    "frequency": "quarterly",
    "trigger": "End of each fiscal quarter",
    "deadline_value": 45,
    "deadline_unit": "calendar_days",
    "responsible_party": "General Partner",
    "conditions": ["Portfolio exposure schedule in agreed spreadsheet format"],
    "effective_from": "2025-01-01",
    "clause_reference": "Clause 4.2",
    "source_quote": "The General Partner shall provide Investor LP-A with unaudited quarterly financial statements and a portfolio exposure schedule within forty-five (45) calendar days following the end of each fiscal quarter.",
    "document_role": "side_letter",
    "requires_human_review": True,
}


def _amendment_text(filename="06_LP_A_AMENDMENT_01.txt"):
    return (DOCS / filename).read_text(encoding="utf-8")


def test_detect_amendment_from_filename_and_text():
    text = _amendment_text()
    assert detect_document_role("uploaded.txt", text, "unknown") == "amendment"


def test_detect_side_letter_and_lpa_without_cross_classifying_references():
    side_letter = (DOCS / "01_LP_A_SIDE_LETTER.txt").read_text(encoding="utf-8")
    lpa = (DOCS / "00_FUND_IV_LPA.txt").read_text(encoding="utf-8")
    assert detect_document_role("01_LP_A_SIDE_LETTER.txt", side_letter, "unknown") == "side_letter"
    assert detect_document_role("00_FUND_IV_LPA.txt", lpa, "unknown") == "lpa"


def test_extract_lp_a_deleted_and_replaced_deadline():
    text = _amendment_text()
    candidates = extract_explicit_deadline_amendments(text, "06_LP_A_AMENDMENT_01.txt")
    assert len(candidates) == 1
    item = candidates[0]
    assert item["investor"] == "LP-A"
    assert item["target_clause"] == "Clause 4.2"
    assert item["old_value"]["value"] == 45
    assert item["new_value"]["value"] == 30
    assert item["old_value"]["unit"] == item["new_value"]["unit"] == "calendar_days"
    assert item["effective_date"] == "2026-01-01"
    assert "All other terms of Clause 4.2 remain unchanged." in item["unchanged_terms"]
    assert item["source_quote"] in text


def test_extract_lp_b_changed_from_to_deadline():
    text = _amendment_text("07_LP_B_AMENDMENT_01.txt")
    candidates = extract_explicit_deadline_amendments(text, "07_LP_B_AMENDMENT_01.txt")
    assert len(candidates) == 1
    item = candidates[0]
    assert item["investor"] == "LP-B"
    assert item["target_clause"] == "Clause 3.1"
    assert item["old_value"]["value"] == 60
    assert item["new_value"]["value"] == 75
    assert item["source_quote"] in text


def test_seeded_demo_amendment_detected_as_already_applied(tmp_path):
    db = tmp_path / "seeded.db"
    load_demo_data(db)
    text = _amendment_text()
    candidate = extract_explicit_deadline_amendments(text, "06_LP_A_AMENDMENT_01.txt")[0]
    match = match_amendment_to_obligations(candidate, list_obligations(db))
    assert match["match_status"] == "already_applied"
    assert match["active_obligation_id"] is not None

    lp_b_text = _amendment_text("07_LP_B_AMENDMENT_01.txt")
    lp_b_candidate = extract_explicit_deadline_amendments(lp_b_text, "07_LP_B_AMENDMENT_01.txt")[0]
    lp_b_match = match_amendment_to_obligations(lp_b_candidate, list_obligations(db))
    assert lp_b_match["match_status"] == "already_applied"


def test_approve_amendment_atomically_versions_obligation(tmp_path):
    db = tmp_path / "approval.db"
    initialize(db)
    base_doc = add_document("01_LP_A_SIDE_LETTER.txt", BASE_CANDIDATE["source_quote"], "side_letter", "LP-A", BASE_CANDIDATE["fund"], True, db)
    old_id = add_obligation(base_doc, BASE_CANDIDATE, "approved", db)

    amendment_text = _amendment_text()
    amendment_doc = add_document("06_LP_A_AMENDMENT_01.txt", amendment_text, "amendment", "LP-A", BASE_CANDIDATE["fund"], True, db)
    candidate = extract_explicit_deadline_amendments(amendment_text, "06_LP_A_AMENDMENT_01.txt")[0]
    match = match_amendment_to_obligations(candidate, list_obligations(db))
    assert match["match_status"] == "matched"
    amendment_id = add_amendment_candidate(amendment_doc, candidate, match, db)

    new_id = review_amendment_candidate(amendment_id, "approved", "authorized-reviewer", "Verified against signed amendment", db)
    obligations = list_obligations(db)
    old = next(o for o in obligations if o["id"] == old_id)
    new = next(o for o in obligations if o["id"] == new_id)
    stored_amendment = get_amendment_candidate(amendment_id, db)

    assert old["status"] == "superseded"
    assert new["status"] == "approved"
    assert new["deadline_value"] == 30
    assert new["deadline_unit"] == "calendar_days"
    assert new["effective_from"] == "2026-01-01"
    assert new["supersedes_obligation_id"] == old_id
    assert new["amendment_candidate_id"] == amendment_id
    assert new["deliverables"] == BASE_CANDIDATE["deliverables"]
    assert stored_amendment["status"] == "approved"
    assert stored_amendment["resulting_obligation_id"] == new_id


def test_reject_amendment_does_not_change_original(tmp_path):
    db = tmp_path / "reject.db"
    initialize(db)
    base_doc = add_document("01_LP_A_SIDE_LETTER.txt", BASE_CANDIDATE["source_quote"], "side_letter", "LP-A", BASE_CANDIDATE["fund"], True, db)
    old_id = add_obligation(base_doc, BASE_CANDIDATE, "approved", db)
    text = _amendment_text()
    amendment_doc = add_document("06_LP_A_AMENDMENT_01.txt", text, "amendment", "LP-A", BASE_CANDIDATE["fund"], True, db)
    candidate = extract_explicit_deadline_amendments(text, "06_LP_A_AMENDMENT_01.txt")[0]
    match = match_amendment_to_obligations(candidate, list_obligations(db))
    amendment_id = add_amendment_candidate(amendment_doc, candidate, match, db)

    result = review_amendment_candidate(amendment_id, "rejected", "authorized-reviewer", "Not accepted", db)
    old = next(o for o in list_obligations(db) if o["id"] == old_id)
    stored_amendment = get_amendment_candidate(amendment_id, db)
    assert result is None
    assert old["status"] == "approved"
    assert old["deadline_value"] == 45
    assert stored_amendment["status"] == "rejected"


def test_cannot_approve_unmatched_amendment(tmp_path):
    db = tmp_path / "unmatched.db"
    initialize(db)
    text = _amendment_text()
    amendment_doc = add_document("06_LP_A_AMENDMENT_01.txt", text, "amendment", "LP-A", BASE_CANDIDATE["fund"], True, db)
    candidate = extract_explicit_deadline_amendments(text, "06_LP_A_AMENDMENT_01.txt")[0]
    match = match_amendment_to_obligations(candidate, [])
    amendment_id = add_amendment_candidate(amendment_doc, candidate, match, db)
    with pytest.raises(ValueError, match="not matched"):
        review_amendment_candidate(amendment_id, "approved", "reviewer", "Testing approval guard", db)


def test_repeat_approval_is_blocked(tmp_path):
    db = tmp_path / "repeat.db"
    initialize(db)
    base_doc = add_document("01_LP_A_SIDE_LETTER.txt", BASE_CANDIDATE["source_quote"], "side_letter", "LP-A", BASE_CANDIDATE["fund"], True, db)
    add_obligation(base_doc, BASE_CANDIDATE, "approved", db)
    text = _amendment_text()
    amendment_doc = add_document("06_LP_A_AMENDMENT_01.txt", text, "amendment", "LP-A", BASE_CANDIDATE["fund"], True, db)
    candidate = extract_explicit_deadline_amendments(text, "06_LP_A_AMENDMENT_01.txt")[0]
    match = match_amendment_to_obligations(candidate, list_obligations(db))
    amendment_id = add_amendment_candidate(amendment_doc, candidate, match, db)
    review_amendment_candidate(amendment_id, "approved", "reviewer", "approved", db)
    with pytest.raises(ValueError, match="cannot be reviewed again"):
        review_amendment_candidate(amendment_id, "approved", "reviewer", "duplicate", db)


def test_amendment_candidate_records_are_listed(tmp_path):
    db = tmp_path / "list.db"
    initialize(db)
    text = _amendment_text()
    doc_id = add_document("06_LP_A_AMENDMENT_01.txt", text, "amendment", "LP-A", BASE_CANDIDATE["fund"], True, db)
    candidate = extract_explicit_deadline_amendments(text, "06_LP_A_AMENDMENT_01.txt")[0]
    match = {"match_status": "no_match", "match_note": "No base record"}
    candidate_id = add_amendment_candidate(doc_id, candidate, match, db)
    rows = list_amendment_candidates(db)
    assert len(rows) == 1
    assert rows[0]["id"] == candidate_id
    assert rows[0]["old_value"]["value"] == 45


def test_parser_supports_business_day_deadline_replacements():
    text = """AMENDMENT TO LP-Z SIDE LETTER
Effective date: 1 February 2027
With effect from 1 February 2027, the deadline in Clause 8.1 is changed from ten (10) business days to fifteen (15) business days after month-end.
"""
    items = extract_explicit_deadline_amendments(text, "LP-Z_amendment.txt")
    assert len(items) == 1
    assert items[0]["investor"] == "LP-Z"
    assert items[0]["target_clause"] == "Clause 8.1"
    assert items[0]["old_value"] == {"value": 10, "unit": "business_days", "raw": "ten (10) business days"}
    assert items[0]["new_value"]["value"] == 15


def test_explicit_parser_does_not_treat_two_unrelated_deadlines_as_amendment():
    text = "The investor receives one report within 10 calendar days and another within 20 calendar days."
    assert extract_explicit_deadline_amendments(text, "misc.txt") == []


def test_duplicate_amendment_candidate_save_is_idempotent(tmp_path):
    db = tmp_path / "duplicate.db"
    initialize(db)
    text = _amendment_text()
    doc_id = add_document("06_LP_A_AMENDMENT_01.txt", text, "amendment", "LP-A", BASE_CANDIDATE["fund"], True, db)
    candidate = extract_explicit_deadline_amendments(text, "06_LP_A_AMENDMENT_01.txt")[0]
    match = {"match_status": "no_match", "match_note": "No base record"}
    first = add_amendment_candidate(doc_id, candidate, match, db)
    second = add_amendment_candidate(doc_id, candidate, match, db)
    assert first == second
    assert len(list_amendment_candidates(db)) == 1


def test_conflicting_active_old_and_approved_new_versions_are_not_auto_matched(tmp_path):
    db = tmp_path / "conflict.db"
    initialize(db)
    base_doc = add_document("01_LP_A_SIDE_LETTER.txt", BASE_CANDIDATE["source_quote"], "side_letter", "LP-A", BASE_CANDIDATE["fund"], True, db)
    add_obligation(base_doc, BASE_CANDIDATE, "approved", db)
    amended_candidate = dict(BASE_CANDIDATE)
    amended_candidate.update({
        "deadline_value": 30,
        "effective_from": "2026-01-01",
        "document_role": "amendment",
        "source_quote": "Amendment changed the deadline from 45 to 30 calendar days.",
    })
    amendment_doc = add_document("06_LP_A_AMENDMENT_01.txt", _amendment_text(), "amendment", "LP-A", BASE_CANDIDATE["fund"], True, db)
    add_obligation(amendment_doc, amended_candidate, "approved", db)
    proposal = extract_explicit_deadline_amendments(_amendment_text(), "06_LP_A_AMENDMENT_01.txt")[0]
    match = match_amendment_to_obligations(proposal, list_obligations(db))
    assert match["match_status"] == "conflicting_versions"
    assert "reconciled manually" in match["match_note"]


def test_cannot_apply_amendment_without_valid_effective_date(tmp_path):
    db = tmp_path / "no-date.db"
    initialize(db)
    base_doc = add_document("01_LP_A_SIDE_LETTER.txt", BASE_CANDIDATE["source_quote"], "side_letter", "LP-A", BASE_CANDIDATE["fund"], True, db)
    add_obligation(base_doc, BASE_CANDIDATE, "approved", db)
    text = _amendment_text()
    amendment_doc = add_document("06_LP_A_AMENDMENT_01.txt", text, "amendment", "LP-A", BASE_CANDIDATE["fund"], True, db)
    proposal = extract_explicit_deadline_amendments(text, "06_LP_A_AMENDMENT_01.txt")[0]
    proposal["effective_date"] = None
    match = match_amendment_to_obligations(proposal, list_obligations(db))
    candidate_id = add_amendment_candidate(amendment_doc, proposal, match, db)
    with pytest.raises(ValueError, match="valid ISO effective date"):
        review_amendment_candidate(candidate_id, "approved", "reviewer", "Testing effective-date guard", db)
