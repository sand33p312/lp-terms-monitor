"""Small SQLite persistence layer for the local prototype."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB = Path("data/lp_terms_monitor.db")


def connect(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize(db_path: str | Path = DEFAULT_DB) -> None:
    with connect(db_path) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            document_type TEXT NOT NULL DEFAULT 'unknown',
            investor_hint TEXT,
            fund_hint TEXT,
            text_content TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            is_synthetic INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS obligations (
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            investor TEXT NOT NULL,
            fund TEXT,
            obligation_type TEXT NOT NULL,
            deliverables_json TEXT NOT NULL,
            frequency TEXT NOT NULL,
            trigger_text TEXT NOT NULL,
            deadline_value INTEGER,
            deadline_unit TEXT NOT NULL,
            responsible_party TEXT,
            conditions_json TEXT NOT NULL,
            effective_from TEXT,
            clause_reference TEXT,
            source_quote TEXT NOT NULL,
            document_role TEXT,
            potential_amendment_note TEXT,
            requires_human_review INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'pending_review',
            review_reason TEXT,
            validation_errors_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewed_by TEXT,
            review_note TEXT,
            supersedes_obligation_id TEXT,
            amendment_candidate_id TEXT,
            FOREIGN KEY(document_id) REFERENCES documents(id)
        );
        CREATE TABLE IF NOT EXISTS fulfilment_events (
            id TEXT PRIMARY KEY,
            obligation_id TEXT NOT NULL,
            reporting_period_end TEXT NOT NULL,
            deadline_date TEXT,
            state TEXT NOT NULL DEFAULT 'not_started',
            delivered_at TEXT,
            evidence_reference TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(obligation_id) REFERENCES obligations(id)
        );
        CREATE TABLE IF NOT EXISTS amendment_candidates (
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            investor TEXT,
            fund TEXT,
            target_document TEXT,
            target_clause TEXT,
            change_type TEXT NOT NULL,
            field_name TEXT NOT NULL,
            old_value_json TEXT,
            new_value_json TEXT,
            effective_date TEXT,
            unchanged_terms_json TEXT NOT NULL DEFAULT '[]',
            source_quote TEXT NOT NULL,
            extraction_method TEXT,
            matched_obligation_id TEXT,
            active_obligation_id TEXT,
            match_status TEXT NOT NULL,
            match_note TEXT,
            status TEXT NOT NULL DEFAULT 'pending_review',
            requires_human_review INTEGER NOT NULL DEFAULT 1,
            review_reason TEXT,
            validation_errors_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewed_by TEXT,
            review_note TEXT,
            resulting_obligation_id TEXT,
            FOREIGN KEY(document_id) REFERENCES documents(id)
        );
        CREATE TABLE IF NOT EXISTS audit_events (
            id TEXT PRIMARY KEY,
            event_at TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            action TEXT NOT NULL,
            actor TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_obligations_investor ON obligations(investor);
        CREATE INDEX IF NOT EXISTS idx_obligations_status ON obligations(status);
        CREATE INDEX IF NOT EXISTS idx_amendment_candidates_status ON amendment_candidates(status);
        CREATE INDEX IF NOT EXISTS idx_audit_events_time ON audit_events(event_at);
        CREATE INDEX IF NOT EXISTS idx_audit_events_entity ON audit_events(entity_type, entity_id);
        """)
        existing_columns = {row[1] for row in con.execute("PRAGMA table_info(obligations)").fetchall()}
        migrations = {
            "document_role": "ALTER TABLE obligations ADD COLUMN document_role TEXT",
            "potential_amendment_note": "ALTER TABLE obligations ADD COLUMN potential_amendment_note TEXT",
            "requires_human_review": "ALTER TABLE obligations ADD COLUMN requires_human_review INTEGER NOT NULL DEFAULT 1",
            "supersedes_obligation_id": "ALTER TABLE obligations ADD COLUMN supersedes_obligation_id TEXT",
            "amendment_candidate_id": "ALTER TABLE obligations ADD COLUMN amendment_candidate_id TEXT",
        }
        for column, statement in migrations.items():
            if column not in existing_columns:
                con.execute(statement)


def _write_audit_event(
    con: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    action: str,
    actor: str,
    details: dict[str, Any] | None = None,
) -> str:
    """Append an application audit event using the caller's transaction.

    This is an append-only application convention, not a tamper-proof ledger.
    Important state changes call this inside the same SQLite transaction so an
    audit event cannot be committed without its corresponding state change.
    """
    event_id = str(uuid.uuid4())
    con.execute(
        """INSERT INTO audit_events
           (id, event_at, entity_type, entity_id, action, actor, details_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            event_id,
            datetime.now(timezone.utc).isoformat(),
            entity_type,
            entity_id,
            action,
            actor or "system",
            json.dumps(details or {}, sort_keys=True),
        ),
    )
    return event_id


def add_document(filename: str, text_content: str, document_type: str = "unknown",
                 investor_hint: str | None = None, fund_hint: str | None = None,
                 is_synthetic: bool = False, db_path: str | Path = DEFAULT_DB) -> str:
    doc_id = str(uuid.uuid4())
    with connect(db_path) as con:
        con.execute(
            "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (doc_id, filename, document_type, investor_hint, fund_hint, text_content,
             datetime.now(timezone.utc).isoformat(), int(is_synthetic)),
        )
        _write_audit_event(
            con, "document", doc_id, "document_saved", "system",
            {"filename": filename, "document_type": document_type, "is_synthetic": bool(is_synthetic)},
        )
    return doc_id


def add_obligation(document_id: str, candidate: dict[str, Any], status: str = "pending_review",
                   db_path: str | Path = DEFAULT_DB) -> str:
    obligation_id = str(uuid.uuid4())
    with connect(db_path) as con:
        con.execute("""INSERT INTO obligations (
            id, document_id, investor, fund, obligation_type, deliverables_json, frequency,
            trigger_text, deadline_value, deadline_unit, responsible_party, conditions_json,
            effective_from, clause_reference, source_quote, document_role,
            potential_amendment_note, requires_human_review, status, review_reason,
            validation_errors_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            obligation_id, document_id, candidate.get("investor") or "Unknown investor",
            candidate.get("fund"), candidate.get("obligation_type", "reporting"),
            json.dumps(candidate.get("deliverables", [])), candidate.get("frequency", "unspecified"),
            candidate.get("trigger", "Unspecified"), candidate.get("deadline_value"),
            candidate.get("deadline_unit", "unspecified"), candidate.get("responsible_party"),
            json.dumps(candidate.get("conditions", [])), candidate.get("effective_from"),
            candidate.get("clause_reference"), candidate.get("source_quote", ""),
            candidate.get("document_role"), candidate.get("potential_amendment_note"),
            int(bool(candidate.get("requires_human_review", True))), status,
            candidate.get("review_reason"), json.dumps(candidate.get("validation_errors", [])),
            datetime.now(timezone.utc).isoformat(),
        ))
        _write_audit_event(
            con, "obligation", obligation_id, "obligation_candidate_created", "system",
            {"document_id": document_id, "investor": candidate.get("investor"), "status": status,
             "clause_reference": candidate.get("clause_reference")},
        )
    return obligation_id


def list_obligations(db_path: str | Path = DEFAULT_DB) -> list[dict[str, Any]]:
    with connect(db_path) as con:
        rows = con.execute("""SELECT o.*, d.filename FROM obligations o
            JOIN documents d ON d.id=o.document_id ORDER BY o.investor, o.created_at""").fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["deliverables"] = json.loads(item.pop("deliverables_json"))
        item["conditions"] = json.loads(item.pop("conditions_json"))
        item["validation_errors"] = json.loads(item.pop("validation_errors_json"))
        results.append(item)
    return results


def get_document(document_id: str, db_path: str | Path = DEFAULT_DB) -> dict[str, Any] | None:
    with connect(db_path) as con:
        row = con.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
    return dict(row) if row else None


def list_documents(db_path: str | Path = DEFAULT_DB) -> list[dict[str, Any]]:
    with connect(db_path) as con:
        rows = con.execute("SELECT id, filename, document_type, investor_hint, fund_hint, uploaded_at, is_synthetic FROM documents ORDER BY uploaded_at DESC").fetchall()
    return [dict(r) for r in rows]


def review_obligation(obligation_id: str, decision: str, reviewer: str, note: str = "",
                      db_path: str | Path = DEFAULT_DB) -> None:
    if decision not in {"approved", "rejected", "needs_review"}:
        raise ValueError("Invalid review decision")
    with connect(db_path) as con:
        row = con.execute("SELECT investor, status FROM obligations WHERE id=?", (obligation_id,)).fetchone()
        if row is None:
            raise ValueError("Obligation record not found")
        if row["status"] == "superseded":
            raise ValueError("Superseded obligations are historical records and cannot be reviewed again")
        now = datetime.now(timezone.utc).isoformat()
        con.execute("""UPDATE obligations SET status=?, reviewed_at=?, reviewed_by=?, review_note=?
            WHERE id=?""", (decision, now, reviewer, note, obligation_id))
        _write_audit_event(
            con, "obligation", obligation_id, f"obligation_{decision}", reviewer,
            {"investor": row["investor"], "previous_status": row["status"], "new_status": decision, "note": note},
        )



def delete_obligation(obligation_id: str, db_path: str | Path = DEFAULT_DB) -> None:
    """Permanently delete an unapproved obligation with no fulfilment history.

    Approved/superseded records and records referenced by fulfilment events are
    deliberately protected so the prototype does not erase operational history.
    """
    deletable_statuses = {"pending_review", "needs_review", "rejected"}
    with connect(db_path) as con:
        row = con.execute(
            "SELECT status FROM obligations WHERE id=?", (obligation_id,)
        ).fetchone()
        if row is None:
            raise ValueError("The selected obligation no longer exists.")
        if row["status"] not in deletable_statuses:
            raise ValueError(
                "Only pending-review, needs-review or rejected records can be deleted. "
                "Approved and superseded records are retained for audit history."
            )
        event_count = con.execute(
            "SELECT COUNT(*) FROM fulfilment_events WHERE obligation_id=?",
            (obligation_id,),
        ).fetchone()[0]
        if event_count:
            raise ValueError(
                "This obligation has fulfilment history and cannot be deleted. "
                "Keep the record so its audit trail remains intact."
            )
        _write_audit_event(
            con, "obligation", obligation_id, "obligation_deleted", "user",
            {"previous_status": row["status"], "reason": "User deleted unapproved candidate"},
        )
        con.execute("DELETE FROM obligations WHERE id=?", (obligation_id,))

def add_fulfilment_event(obligation_id: str, reporting_period_end: str, deadline_date: str | None,
                         state: str, delivered_at: str | None = None,
                         evidence_reference: str | None = None,
                         db_path: str | Path = DEFAULT_DB) -> str:
    if state not in {"not_started", "in_progress", "fulfilled", "overdue", "requires_review"}:
        raise ValueError("Invalid fulfilment state")
    event_id = str(uuid.uuid4())
    with connect(db_path) as con:
        con.execute("""INSERT INTO fulfilment_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (
            event_id, obligation_id, reporting_period_end, deadline_date, state,
            delivered_at, evidence_reference, datetime.now(timezone.utc).isoformat(),
        ))
        _write_audit_event(
            con, "fulfilment_event", event_id, "fulfilment_event_created", "user",
            {"obligation_id": obligation_id, "reporting_period_end": reporting_period_end,
             "deadline_date": deadline_date, "state": state, "delivered_at": delivered_at,
             "evidence_reference": evidence_reference},
        )
    return event_id


def clear_database(db_path: str | Path = DEFAULT_DB) -> None:
    with connect(db_path) as con:
        con.execute("DELETE FROM audit_events")
        con.execute("DELETE FROM amendment_candidates")
        con.execute("DELETE FROM fulfilment_events")
        con.execute("DELETE FROM obligations")
        con.execute("DELETE FROM documents")


def list_fulfilment_events(db_path: str | Path = DEFAULT_DB) -> list[dict[str, Any]]:
    with connect(db_path) as con:
        rows = con.execute("""SELECT e.*, o.investor, o.deliverables_json
            FROM fulfilment_events e JOIN obligations o ON o.id=e.obligation_id
            ORDER BY e.reporting_period_end DESC""").fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["deliverables"] = json.loads(item.pop("deliverables_json"))
        results.append(item)
    return results


def list_audit_events(db_path: str | Path = DEFAULT_DB, limit: int = 500) -> list[dict[str, Any]]:
    """Return recent append-only application audit events, newest first."""
    safe_limit = max(1, min(int(limit), 5000))
    with connect(db_path) as con:
        rows = con.execute(
            "SELECT * FROM audit_events ORDER BY event_at DESC, rowid DESC LIMIT ?",
            (safe_limit,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["details"] = json.loads(item.pop("details_json") or "{}")
        result.append(item)
    return result



def _decode_amendment_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for json_field, output_field in (
        ("old_value_json", "old_value"), ("new_value_json", "new_value"),
        ("unchanged_terms_json", "unchanged_terms"),
        ("validation_errors_json", "validation_errors"),
    ):
        raw = item.pop(json_field, None)
        item[output_field] = json.loads(raw) if raw else ([] if output_field in {"unchanged_terms", "validation_errors"} else None)
    return item


def add_amendment_candidate(
    document_id: str, candidate: dict[str, Any], match_result: dict[str, Any],
    db_path: str | Path = DEFAULT_DB,
) -> str:
    """Persist an amendment proposal without changing any obligation."""
    match_status = match_result.get("match_status", "no_match")
    status = "already_applied" if match_status == "already_applied" else (
        "pending_review" if match_status == "matched" else "needs_review"
    )
    # A candidate whose values are not literally supported by its quote, or whose
    # change type cannot be applied automatically, is kept for manual handling
    # rather than dropped. Approval re-checks validation errors and refuses it.
    if status == "pending_review" and candidate.get("validation_errors"):
        status = "needs_review"
    with connect(db_path) as con:
        existing = con.execute(
            "SELECT id FROM amendment_candidates WHERE document_id=? AND source_quote=?",
            (document_id, candidate.get("source_quote", "")),
        ).fetchone()
        if existing:
            return existing["id"]
        candidate_id = str(uuid.uuid4())
        con.execute("""INSERT INTO amendment_candidates (
            id, document_id, investor, fund, target_document, target_clause, change_type,
            field_name, old_value_json, new_value_json, effective_date, unchanged_terms_json,
            source_quote, extraction_method, matched_obligation_id, active_obligation_id,
            match_status, match_note, status, requires_human_review, review_reason,
            validation_errors_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            candidate_id, document_id, candidate.get("investor"), candidate.get("fund"),
            candidate.get("target_document"), candidate.get("target_clause"),
            candidate.get("change_type", "other"), candidate.get("field_name", "unknown"),
            json.dumps(candidate.get("old_value")), json.dumps(candidate.get("new_value")),
            candidate.get("effective_date"), json.dumps(candidate.get("unchanged_terms", [])),
            candidate.get("source_quote", ""), candidate.get("extraction_method"),
            match_result.get("matched_obligation_id"), match_result.get("active_obligation_id"),
            match_status, match_result.get("match_note"), status, 1,
            candidate.get("review_reason") or "Mandatory human confirmation before amendment application.",
            json.dumps(candidate.get("validation_errors", [])), datetime.now(timezone.utc).isoformat(),
        ))
        _write_audit_event(
            con, "amendment_candidate", candidate_id, "amendment_candidate_saved", "system",
            {"document_id": document_id, "investor": candidate.get("investor"),
             "target_clause": candidate.get("target_clause"), "match_status": match_status,
             "old_value": candidate.get("old_value"), "new_value": candidate.get("new_value")},
        )
    return candidate_id


def list_amendment_candidates(db_path: str | Path = DEFAULT_DB) -> list[dict[str, Any]]:
    with connect(db_path) as con:
        rows = con.execute("""SELECT a.*, d.filename FROM amendment_candidates a
            JOIN documents d ON d.id=a.document_id ORDER BY a.created_at DESC""").fetchall()
    return [_decode_amendment_row(row) for row in rows]


def get_amendment_candidate(candidate_id: str, db_path: str | Path = DEFAULT_DB) -> dict[str, Any] | None:
    with connect(db_path) as con:
        row = con.execute("""SELECT a.*, d.filename FROM amendment_candidates a
            JOIN documents d ON d.id=a.document_id WHERE a.id=?""", (candidate_id,)).fetchone()
    return _decode_amendment_row(row) if row else None


def review_amendment_candidate(
    candidate_id: str, decision: str, reviewer: str, note: str = "",
    db_path: str | Path = DEFAULT_DB,
) -> str | None:
    """Approve/reject an amendment. Approval atomically versions the matched obligation.

    Returns the new obligation ID on approval, otherwise None. An amendment is never
    applied unless it matched exactly one currently approved old obligation.
    """
    if decision not in {"approved", "rejected", "needs_review"}:
        raise ValueError("Invalid amendment review decision")
    reviewer = (reviewer or "").strip()
    note = (note or "").strip()
    if not reviewer:
        raise ValueError("Reviewer identifier is required")
    if not note:
        raise ValueError("A review note is required for the audit trail")
    now = datetime.now(timezone.utc).isoformat()
    with connect(db_path) as con:
        con.row_factory = sqlite3.Row
        con.execute("BEGIN IMMEDIATE")
        amendment = con.execute("SELECT * FROM amendment_candidates WHERE id=?", (candidate_id,)).fetchone()
        if amendment is None:
            raise ValueError("Amendment candidate not found")
        if amendment["status"] in {"approved", "rejected", "already_applied"}:
            raise ValueError(f"This amendment candidate is already {amendment['status']} and cannot be reviewed again")

        if decision != "approved":
            con.execute("""UPDATE amendment_candidates SET status=?, reviewed_at=?, reviewed_by=?, review_note=?
                WHERE id=?""", (decision, now, reviewer, note, candidate_id))
            _write_audit_event(
                con, "amendment_candidate", candidate_id, f"amendment_{decision}", reviewer,
                {"investor": amendment["investor"], "target_clause": amendment["target_clause"],
                 "previous_status": amendment["status"], "new_status": decision,
                 "note": note, "original_obligation_unchanged": True},
            )
            return None

        if amendment["match_status"] != "matched" or not amendment["matched_obligation_id"]:
            raise ValueError("Cannot apply this amendment: it is not matched to exactly one approved old obligation")
        if amendment["field_name"] != "deadline":
            raise ValueError("This prototype currently supports approval of deadline amendments only")
        validation_errors = json.loads(amendment["validation_errors_json"] or "[]")
        if validation_errors:
            raise ValueError("Cannot approve an amendment with validation errors: " + "; ".join(validation_errors))

        old_value = json.loads(amendment["old_value_json"] or "{}")
        new_value = json.loads(amendment["new_value_json"] or "{}")
        if not isinstance(old_value.get("value"), int) or old_value.get("unit") not in {"calendar_days", "business_days"}:
            raise ValueError("Old deadline value is incomplete or unsupported")
        if not isinstance(new_value.get("value"), int) or new_value.get("unit") not in {"calendar_days", "business_days"}:
            raise ValueError("New deadline value is incomplete or unsupported")
        if old_value == new_value:
            raise ValueError("The proposed amendment does not change the old deadline")
        try:
            datetime.strptime(amendment["effective_date"], "%Y-%m-%d")
        except (TypeError, ValueError):
            raise ValueError("A valid ISO effective date is required before applying an amendment")

        old = con.execute("SELECT * FROM obligations WHERE id=?", (amendment["matched_obligation_id"],)).fetchone()
        if old is None or old["status"] != "approved":
            raise ValueError("The matched original obligation is no longer approved. Refresh and rematch before applying.")
        if old["deadline_value"] != old_value["value"] or old["deadline_unit"] != old_value["unit"]:
            raise ValueError("The original obligation no longer has the expected old deadline. Amendment was not applied.")

        # Idempotency guard: never create another active version with the same target value/date.
        existing = con.execute("""SELECT id FROM obligations
            WHERE investor=? AND fund IS ? AND clause_reference=? AND deadline_value=? AND deadline_unit=?
            AND effective_from=? AND status='approved' AND document_role='amendment'""", (
                old["investor"], old["fund"], old["clause_reference"], new_value["value"],
                new_value["unit"], amendment["effective_date"],
            )).fetchone()
        if existing:
            raise ValueError(
                "An approved amended version already exists while the old version is still active. "
                "Reconcile the obligation register manually; no duplicate version was created."
            )

        new_id = str(uuid.uuid4())
        candidate_note = (
            f"Approved amendment: deadline changed from {old_value['value']} {old_value['unit']} "
            f"to {new_value['value']} {new_value['unit']} effective {amendment['effective_date'] or 'date unconfirmed'}. "
            "Legal effect and applicability approved by the named reviewer for this prototype workflow."
        )
        con.execute("""INSERT INTO obligations (
            id, document_id, investor, fund, obligation_type, deliverables_json, frequency,
            trigger_text, deadline_value, deadline_unit, responsible_party, conditions_json,
            effective_from, clause_reference, source_quote, document_role,
            potential_amendment_note, requires_human_review, status, review_reason,
            validation_errors_json, created_at, reviewed_at, reviewed_by, review_note,
            supersedes_obligation_id, amendment_candidate_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            new_id, amendment["document_id"], old["investor"], old["fund"], old["obligation_type"],
            old["deliverables_json"], old["frequency"], old["trigger_text"],
            new_value["value"], new_value["unit"], old["responsible_party"],
            old["conditions_json"], amendment["effective_date"], old["clause_reference"],
            amendment["source_quote"], "amendment", candidate_note, 0, "approved",
            "Human-approved amendment candidate", "[]", now, now, reviewer, note,
            old["id"], candidate_id,
        ))
        con.execute("UPDATE obligations SET status='superseded' WHERE id=?", (old["id"],))
        con.execute("""UPDATE amendment_candidates SET status='approved', reviewed_at=?, reviewed_by=?,
            review_note=?, resulting_obligation_id=? WHERE id=?""",
            (now, reviewer, note, new_id, candidate_id))
        _write_audit_event(
            con, "obligation", old["id"], "obligation_superseded", reviewer,
            {"superseded_by": new_id, "old_deadline_value": old["deadline_value"],
             "old_deadline_unit": old["deadline_unit"], "amendment_candidate_id": candidate_id},
        )
        _write_audit_event(
            con, "obligation", new_id, "obligation_version_created", reviewer,
            {"previous_version_id": old["id"], "new_deadline_value": new_value["value"],
             "new_deadline_unit": new_value["unit"], "effective_from": amendment["effective_date"],
             "source_amendment": amendment["document_id"], "review_note": note},
        )
        _write_audit_event(
            con, "amendment_candidate", candidate_id, "amendment_approved", reviewer,
            {"old_obligation_id": old["id"], "new_obligation_id": new_id,
             "old_value": old_value, "new_value": new_value,
             "effective_date": amendment["effective_date"], "note": note},
        )
        return new_id
