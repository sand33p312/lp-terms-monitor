from __future__ import annotations

import json
import sqlite3

import pytest

from src.store import (
    add_amendment_candidate,
    add_document,
    add_obligation,
    initialize,
    list_audit_events,
    list_amendment_candidates,
    list_obligations,
    review_amendment_candidate,
)
from src.amendments import extract_explicit_deadline_amendments, match_amendment_to_obligations
from src.demo import DOCS


BASE = {
    "investor": "LP-A",
    "fund": "ABC Private Equity Fund IV",
    "obligation_type": "reporting",
    "deliverables": ["unaudited quarterly financial statements", "portfolio exposure schedule"],
    "frequency": "quarterly",
    "trigger": "end of each fiscal quarter",
    "deadline_value": 45,
    "deadline_unit": "calendar_days",
    "responsible_party": "General Partner",
    "conditions": [],
    "effective_from": "2025-01-01",
    "clause_reference": "Clause 4.2",
    "source_quote": "The General Partner shall provide Investor LP-A with unaudited quarterly financial statements and a portfolio exposure schedule within forty-five (45) calendar days following the end of each fiscal quarter.",
    "document_role": "side_letter",
    "requires_human_review": True,
}


def test_document_obligation_and_review_events_are_recorded(tmp_path):
    db = tmp_path / "audit.db"
    initialize(db)
    doc_id = add_document("base.txt", BASE["source_quote"], "side_letter", "LP-A", BASE["fund"], True, db)
    obligation_id = add_obligation(doc_id, BASE, "pending_review", db)

    from src.store import review_obligation
    review_obligation(obligation_id, "approved", "reviewer-1", "Compared with synthetic source", db)

    events = list_audit_events(db)
    actions = [event["action"] for event in events]
    assert "document_saved" in actions
    assert "obligation_candidate_created" in actions
    assert "obligation_approved" in actions
    approved_event = next(event for event in events if event["action"] == "obligation_approved")
    assert approved_event["actor"] == "reviewer-1"


def test_amendment_approval_audit_events_are_in_same_database(tmp_path):
    db = tmp_path / "amendment-audit.db"
    initialize(db)
    original_doc = add_document("01_LP_A_SIDE_LETTER.txt", BASE["source_quote"], "side_letter", "LP-A", BASE["fund"], True, db)
    old_id = add_obligation(original_doc, BASE, "approved", db)
    amendment_text = (DOCS / "06_LP_A_AMENDMENT_01.txt").read_text(encoding="utf-8")
    amendment_doc = add_document("06_LP_A_AMENDMENT_01.txt", amendment_text, "amendment", "LP-A", BASE["fund"], True, db)
    candidate = extract_explicit_deadline_amendments(amendment_text, "06_LP_A_AMENDMENT_01.txt")[0]
    match = match_amendment_to_obligations(candidate, list_obligations(db))
    candidate_id = add_amendment_candidate(amendment_doc, candidate, match, db)

    new_id = review_amendment_candidate(candidate_id, "approved", "reviewer-2", "Verified signed amendment", db)
    events = list_audit_events(db)
    actions = [event["action"] for event in events]
    assert "amendment_candidate_saved" in actions
    assert "obligation_superseded" in actions
    assert "obligation_version_created" in actions
    assert "amendment_approved" in actions
    superseded_event = next(event for event in events if event["action"] == "obligation_superseded")
    created_event = next(event for event in events if event["action"] == "obligation_version_created")
    assert superseded_event["entity_id"] == old_id
    assert created_event["entity_id"] == new_id
    assert all(event["actor"] != "" for event in events)


def test_v03_database_gets_audit_table_on_initialize(tmp_path):
    db = tmp_path / "old-v03.db"
    # Create the complete v0.3 schema, then remove the v0.4-only audit table.
    initialize(db)
    doc_id = add_document("legacy.txt", BASE["source_quote"], "side_letter", "LP-A", BASE["fund"], True, db)
    obligation_id = add_obligation(doc_id, BASE, "approved", db)
    with sqlite3.connect(db) as con:
        con.execute("DROP TABLE audit_events")
        con.commit()

    initialize(db)
    with sqlite3.connect(db) as con:
        table_names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "audit_events" in table_names
    assert any(item["id"] == obligation_id for item in list_obligations(db))


def test_main_and_simulator_databases_are_separate():
    from src.store import DEFAULT_DB as MAIN_DB
    from src.amendment_lab import DEFAULT_DB as SIMULATOR_DB
    assert MAIN_DB.resolve() != SIMULATOR_DB.resolve()


def test_review_note_is_required_for_auditability(tmp_path):
    db = tmp_path / "note-required.db"
    initialize(db)
    doc_id = add_document("base.txt", BASE["source_quote"], "side_letter", "LP-A", BASE["fund"], True, db)
    add_obligation(doc_id, BASE, "approved", db)
    amendment_text = (DOCS / "06_LP_A_AMENDMENT_01.txt").read_text(encoding="utf-8")
    amendment_doc = add_document("06_LP_A_AMENDMENT_01.txt", amendment_text, "amendment", "LP-A", BASE["fund"], True, db)
    candidate = extract_explicit_deadline_amendments(amendment_text, "06_LP_A_AMENDMENT_01.txt")[0]
    match = match_amendment_to_obligations(candidate, list_obligations(db))
    candidate_id = add_amendment_candidate(amendment_doc, candidate, match, db)
    with pytest.raises(ValueError, match="review note is required"):
        review_amendment_candidate(candidate_id, "approved", "reviewer", "   ", db)
    stored = next(item for item in list_amendment_candidates(db) if item["id"] == candidate_id)
    assert stored["status"] == "pending_review"
    events = list_audit_events(db)
    assert all(json.dumps(event["details"], sort_keys=True) for event in events)


def test_amendment_rejection_records_audit_event_and_preserves_old_version(tmp_path):
    db = tmp_path / "reject-audit.db"
    initialize(db)
    original_doc = add_document("01_LP_A_SIDE_LETTER.txt", BASE["source_quote"], "side_letter", "LP-A", BASE["fund"], True, db)
    old_id = add_obligation(original_doc, BASE, "approved", db)
    amendment_text = (DOCS / "06_LP_A_AMENDMENT_01.txt").read_text(encoding="utf-8")
    amendment_doc = add_document("06_LP_A_AMENDMENT_01.txt", amendment_text, "amendment", "LP-A", BASE["fund"], True, db)
    candidate = extract_explicit_deadline_amendments(amendment_text, "06_LP_A_AMENDMENT_01.txt")[0]
    match = match_amendment_to_obligations(candidate, list_obligations(db))
    candidate_id = add_amendment_candidate(amendment_doc, candidate, match, db)

    review_amendment_candidate(candidate_id, "rejected", "reviewer-3", "Not accepted after document review", db)
    old = next(item for item in list_obligations(db) if item["id"] == old_id)
    assert old["status"] == "approved"
    assert old["deadline_value"] == 45
    assert any(event["action"] == "amendment_rejected" for event in list_audit_events(db))


def test_main_amendment_transaction_rolls_back_audit_and_version_changes(tmp_path):
    db = tmp_path / "rollback-audit.db"
    initialize(db)
    original_doc = add_document("01_LP_A_SIDE_LETTER.txt", BASE["source_quote"], "side_letter", "LP-A", BASE["fund"], True, db)
    old_id = add_obligation(original_doc, BASE, "approved", db)
    amendment_text = (DOCS / "06_LP_A_AMENDMENT_01.txt").read_text(encoding="utf-8")
    amendment_doc = add_document("06_LP_A_AMENDMENT_01.txt", amendment_text, "amendment", "LP-A", BASE["fund"], True, db)
    candidate = extract_explicit_deadline_amendments(amendment_text, "06_LP_A_AMENDMENT_01.txt")[0]
    match = match_amendment_to_obligations(candidate, list_obligations(db))
    candidate_id = add_amendment_candidate(amendment_doc, candidate, match, db)

    with sqlite3.connect(db) as con:
        con.execute(
            """CREATE TRIGGER force_amendment_insert_failure
               BEFORE INSERT ON obligations WHEN NEW.document_role='amendment'
               BEGIN SELECT RAISE(ABORT, 'forced insertion failure'); END;"""
        )
        con.commit()

    import pytest
    with pytest.raises(sqlite3.IntegrityError, match="forced insertion failure"):
        review_amendment_candidate(candidate_id, "approved", "reviewer-4", "Testing rollback", db)

    old = next(item for item in list_obligations(db) if item["id"] == old_id)
    amendment = next(item for item in list_amendment_candidates(db) if item["id"] == candidate_id)
    assert old["status"] == "approved"
    assert old["deadline_value"] == 45
    assert amendment["status"] == "pending_review"
    actions = [event["action"] for event in list_audit_events(db)]
    assert "obligation_superseded" not in actions
    assert "obligation_version_created" not in actions
    assert "amendment_approved" not in actions
