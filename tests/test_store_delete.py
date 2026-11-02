import pytest

from src.store import (
    add_document,
    add_obligation,
    add_fulfilment_event,
    delete_obligation,
    initialize,
    list_obligations,
)


CANDIDATE = {
    "investor": "LP-TEST",
    "fund": "Test Fund",
    "obligation_type": "reporting",
    "deliverables": ["quarterly report"],
    "frequency": "quarterly",
    "trigger": "quarter end",
    "deadline_value": 30,
    "deadline_unit": "calendar_days",
    "conditions": [],
    "source_quote": "The GP shall provide the quarterly report within 30 days.",
    "requires_human_review": True,
}


def _record(tmp_path, status="pending_review"):
    db = tmp_path / "records.db"
    initialize(db)
    doc_id = add_document("test.txt", CANDIDATE["source_quote"], db_path=db)
    obligation_id = add_obligation(doc_id, CANDIDATE, status=status, db_path=db)
    return db, obligation_id


def test_delete_pending_review_record(tmp_path):
    db, obligation_id = _record(tmp_path)
    delete_obligation(obligation_id, db)
    assert list_obligations(db) == []


def test_cannot_delete_approved_record(tmp_path):
    db, obligation_id = _record(tmp_path, status="approved")
    with pytest.raises(ValueError, match="Only pending-review"):
        delete_obligation(obligation_id, db)
    assert len(list_obligations(db)) == 1


def test_cannot_delete_record_with_fulfilment_history(tmp_path):
    db, obligation_id = _record(tmp_path)
    add_fulfilment_event(
        obligation_id,
        reporting_period_end="2027-03-31",
        deadline_date="2027-04-30",
        state="not_started",
        db_path=db,
    )
    with pytest.raises(ValueError, match="fulfilment history"):
        delete_obligation(obligation_id, db)
    assert len(list_obligations(db)) == 1


def test_delete_unknown_record_fails_clearly(tmp_path):
    db, _ = _record(tmp_path)
    with pytest.raises(ValueError, match="no longer exists"):
        delete_obligation("does-not-exist", db)
