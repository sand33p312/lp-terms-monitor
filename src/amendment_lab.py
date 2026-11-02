"""Isolated SQLite store for the LP Terms Monitor amendment lifecycle lab.

This database is intentionally separate from the main LP Terms Monitor database.
It demonstrates controlled amendment review, versioning, audit history and rollback.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_DIR / "data" / "amendment_lifecycle_lab.sqlite3"


class AmendmentWorkflowError(RuntimeError):
    """Raised when an amendment transition is unsafe or invalid."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def initialize(db_path: str | Path = DEFAULT_DB, seed: bool = True) -> None:
    with connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS obligations (
                id TEXT PRIMARY KEY,
                fund TEXT NOT NULL,
                investor TEXT NOT NULL,
                governing_document TEXT NOT NULL,
                clause_reference TEXT NOT NULL,
                deliverables_json TEXT NOT NULL,
                frequency TEXT NOT NULL,
                deadline_value INTEGER NOT NULL CHECK(deadline_value > 0),
                deadline_unit TEXT NOT NULL CHECK(deadline_unit IN ('calendar_days','business_days')),
                effective_from TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('approved','superseded')),
                version INTEGER NOT NULL CHECK(version >= 1),
                previous_version_id TEXT REFERENCES obligations(id),
                source_amendment TEXT,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                UNIQUE(investor, clause_reference, version)
            );

            CREATE TABLE IF NOT EXISTS amendment_candidates (
                id TEXT PRIMARY KEY,
                amendment_document TEXT NOT NULL,
                investor TEXT NOT NULL,
                target_document TEXT NOT NULL,
                target_clause TEXT NOT NULL,
                field_name TEXT NOT NULL,
                old_value INTEGER NOT NULL,
                old_unit TEXT NOT NULL,
                new_value INTEGER NOT NULL,
                new_unit TEXT NOT NULL,
                effective_date TEXT NOT NULL,
                unchanged_terms_json TEXT NOT NULL,
                source_quote TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending_review','approved','rejected')),
                matched_obligation_id TEXT REFERENCES obligations(id),
                applied_obligation_id TEXT REFERENCES obligations(id),
                reviewer TEXT,
                review_note TEXT,
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                UNIQUE(amendment_document, investor, target_clause, field_name, effective_date)
            );

            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_at TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                details_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_obligations_investor_clause
                ON obligations(investor, clause_reference, status);
            CREATE INDEX IF NOT EXISTS idx_audit_entity
                ON audit_events(entity_type, entity_id, id);
            """
        )
        if seed:
            _seed_once(conn)


def _seed_once(conn: sqlite3.Connection) -> None:
    existing = conn.execute("SELECT COUNT(*) FROM obligations").fetchone()[0]
    if existing:
        return
    _insert_seed_scenario(conn)


def _insert_seed_scenario(conn: sqlite3.Connection) -> None:
    now = _now()
    conn.execute(
        """INSERT INTO obligations (
            id, fund, investor, governing_document, clause_reference,
            deliverables_json, frequency, deadline_value, deadline_unit,
            effective_from, status, version, previous_version_id,
            source_amendment, created_at, created_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "OBL-LPA-4.2-V1", "ABC Private Equity Fund IV", "LP-A",
            "LP-A Side Letter", "Clause 4.2",
            json.dumps([
                "unaudited quarterly financial statements",
                "portfolio exposure schedule",
            ]),
            "quarterly", 45, "calendar_days", "2025-01-01", "approved", 1,
            None, None, now, "lab-seed",
        ),
    )
    conn.execute(
        """INSERT INTO amendment_candidates (
            id, amendment_document, investor, target_document, target_clause,
            field_name, old_value, old_unit, new_value, new_unit,
            effective_date, unchanged_terms_json, source_quote, status,
            matched_obligation_id, applied_obligation_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "AMD-LPA-4.2-2026-01", "06_LP_A_AMENDMENT_01.txt", "LP-A",
            "LP-A Side Letter", "Clause 4.2", "deadline", 45,
            "calendar_days", 30, "calendar_days", "2026-01-01",
            json.dumps(["All other terms of Clause 4.2 remain unchanged."]),
            ('With effect from 1 January 2026, the reference to "forty-five '
             '(45) calendar days" in Clause 4.2 of the LP-A Side Letter is '
             'deleted and replaced with "thirty (30) calendar days". All '
             'other terms of Clause 4.2 remain unchanged.'),
            "pending_review", "OBL-LPA-4.2-V1", None, now,
        ),
    )
    _audit(
        conn, "scenario", "LP-A-45-TO-30", "scenario_seeded", "system",
        {"database": "isolated_amendment_lifecycle_lab", "baseline_deadline": 45,
         "proposed_deadline": 30, "effective_date": "2026-01-01"},
    )


def reset_scenario(db_path: str | Path = DEFAULT_DB) -> None:
    """Reset only this lab database and recreate the LP-A 45→30 scenario."""
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM audit_events")
            conn.execute("DELETE FROM amendment_candidates")
            conn.execute("DELETE FROM obligations")
            _insert_seed_scenario(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _decode_obligation(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["deliverables"] = json.loads(item.pop("deliverables_json"))
    return item


def _decode_candidate(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["unchanged_terms"] = json.loads(item.pop("unchanged_terms_json"))
    return item


def get_obligations(db_path: str | Path = DEFAULT_DB) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM obligations ORDER BY investor, version"
        ).fetchall()
    return [_decode_obligation(row) for row in rows]


def get_candidates(db_path: str | Path = DEFAULT_DB) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM amendment_candidates ORDER BY created_at, id"
        ).fetchall()
    return [_decode_candidate(row) for row in rows]


def get_audit_events(db_path: str | Path = DEFAULT_DB) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM audit_events ORDER BY id DESC").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["details"] = json.loads(item.pop("details_json"))
        result.append(item)
    return result


def _audit(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    action: str,
    actor: str,
    details: dict[str, Any],
) -> None:
    conn.execute(
        """INSERT INTO audit_events
           (event_at, entity_type, entity_id, action, actor, details_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (_now(), entity_type, entity_id, action, actor, json.dumps(details, sort_keys=True)),
    )


def review_amendment(
    candidate_id: str,
    decision: str,
    reviewer: str,
    review_note: str,
    db_path: str | Path = DEFAULT_DB,
) -> str | None:
    """Approve/reject a proposed amendment with atomic versioning and audit.

    Approval supersedes exactly one matching active obligation and creates a new
    approved version. Rejection changes only the candidate/audit history.
    Returns the new obligation ID on approval, otherwise None.
    """
    decision = decision.strip().lower()
    reviewer = reviewer.strip()
    review_note = review_note.strip()
    if decision not in {"approved", "rejected"}:
        raise AmendmentWorkflowError("Decision must be 'approved' or 'rejected'.")
    if not reviewer:
        raise AmendmentWorkflowError("Reviewer identifier is required.")
    if not review_note:
        raise AmendmentWorkflowError("A review note is required for the audit trail.")

    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            candidate_row = conn.execute(
                "SELECT * FROM amendment_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if candidate_row is None:
                raise AmendmentWorkflowError("Amendment candidate was not found.")
            candidate = dict(candidate_row)
            if candidate["status"] != "pending_review":
                raise AmendmentWorkflowError(
                    f"Candidate is already {candidate['status']}; repeat application is blocked."
                )

            if decision == "rejected":
                conn.execute(
                    """UPDATE amendment_candidates
                       SET status='rejected', reviewer=?, review_note=?, reviewed_at=?
                       WHERE id=? AND status='pending_review'""",
                    (reviewer, review_note, _now(), candidate_id),
                )
                _audit(
                    conn, "amendment_candidate", candidate_id, "amendment_rejected",
                    reviewer,
                    {"investor": candidate["investor"], "target_clause": candidate["target_clause"],
                     "old_value": candidate["old_value"], "new_value": candidate["new_value"],
                     "note": review_note, "original_obligation_unchanged": True},
                )
                conn.commit()
                return None

            # Validate the proposed change before mutating any row.
            if candidate["field_name"] != "deadline":
                raise AmendmentWorkflowError("This lab only applies deadline amendments.")
            if candidate["new_unit"] not in {"calendar_days", "business_days"}:
                raise AmendmentWorkflowError("Unsupported new deadline unit.")
            if int(candidate["new_value"]) <= 0 or int(candidate["old_value"]) <= 0:
                raise AmendmentWorkflowError("Deadline values must be positive integers.")
            if (candidate["old_value"] == candidate["new_value"]
                    and candidate["old_unit"] == candidate["new_unit"]):
                raise AmendmentWorkflowError("The amendment does not change the deadline.")
            try:
                datetime.strptime(candidate["effective_date"], "%Y-%m-%d")
            except ValueError as exc:
                raise AmendmentWorkflowError("Effective date must be YYYY-MM-DD.") from exc

            matches = conn.execute(
                """SELECT * FROM obligations
                   WHERE investor=? AND clause_reference=? AND status='approved'
                     AND deadline_value=? AND deadline_unit=?
                   ORDER BY version""",
                (candidate["investor"], candidate["target_clause"],
                 candidate["old_value"], candidate["old_unit"]),
            ).fetchall()
            if len(matches) != 1:
                raise AmendmentWorkflowError(
                    f"Expected exactly one active approved obligation matching the old value; found {len(matches)}. "
                    "No records were changed."
                )
            old = dict(matches[0])
            if candidate["matched_obligation_id"] and candidate["matched_obligation_id"] != old["id"]:
                raise AmendmentWorkflowError(
                    "The currently active obligation differs from the candidate's matched record. Reconcile manually."
                )

            new_id = f"OBL-{candidate['investor']}-{candidate['target_clause'].replace(' ', '').replace('.', '-')}-V{old['version'] + 1}-{uuid.uuid4().hex[:6].upper()}"
            now = _now()

            # Both record changes, candidate status and audit events share one transaction.
            updated = conn.execute(
                "UPDATE obligations SET status='superseded' WHERE id=? AND status='approved'",
                (old["id"],),
            ).rowcount
            if updated != 1:
                raise AmendmentWorkflowError("Could not supersede the original obligation safely.")

            conn.execute(
                """INSERT INTO obligations (
                    id, fund, investor, governing_document, clause_reference,
                    deliverables_json, frequency, deadline_value, deadline_unit,
                    effective_from, status, version, previous_version_id,
                    source_amendment, created_at, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'approved', ?, ?, ?, ?, ?)""",
                (
                    new_id, old["fund"], old["investor"], old["governing_document"],
                    old["clause_reference"], old["deliverables_json"], old["frequency"],
                    candidate["new_value"], candidate["new_unit"], candidate["effective_date"],
                    old["version"] + 1, old["id"], candidate["amendment_document"], now, reviewer,
                ),
            )
            conn.execute(
                """UPDATE amendment_candidates
                   SET status='approved', reviewer=?, review_note=?, reviewed_at=?,
                       matched_obligation_id=?, applied_obligation_id=?
                   WHERE id=? AND status='pending_review'""",
                (reviewer, review_note, now, old["id"], new_id, candidate_id),
            )
            _audit(
                conn, "obligation", old["id"], "obligation_superseded", reviewer,
                {"superseded_by": new_id, "old_deadline_value": old["deadline_value"],
                 "old_deadline_unit": old["deadline_unit"], "amendment_candidate_id": candidate_id},
            )
            _audit(
                conn, "obligation", new_id, "obligation_version_created", reviewer,
                {"previous_version_id": old["id"], "new_deadline_value": candidate["new_value"],
                 "new_deadline_unit": candidate["new_unit"], "effective_from": candidate["effective_date"],
                 "source_amendment": candidate["amendment_document"], "review_note": review_note},
            )
            _audit(
                conn, "amendment_candidate", candidate_id, "amendment_approved", reviewer,
                {"old_obligation_id": old["id"], "new_obligation_id": new_id,
                 "note": review_note},
            )
            conn.commit()
            return new_id
        except Exception:
            conn.rollback()
            raise


def get_scenario_summary(db_path: str | Path = DEFAULT_DB) -> dict[str, Any]:
    obligations = get_obligations(db_path)
    candidates = get_candidates(db_path)
    active = [o for o in obligations if o["status"] == "approved"]
    superseded = [o for o in obligations if o["status"] == "superseded"]
    pending = [c for c in candidates if c["status"] == "pending_review"]
    return {
        "obligation_count": len(obligations),
        "active_count": len(active),
        "superseded_count": len(superseded),
        "pending_count": len(pending),
        "active_obligations": active,
        "superseded_obligations": superseded,
        "candidates": candidates,
    }
