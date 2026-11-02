import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.amendment_lab import (
    AmendmentWorkflowError, get_audit_events, get_candidates, get_obligations,
    initialize, reset_scenario, review_amendment,
)


class AmendmentLifecycleLabTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Path(self.temp_dir.name) / "isolated_lab.sqlite3"
        initialize(self.db, seed=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _candidate(self):
        return get_candidates(self.db)[0]

    def _obligations(self):
        return get_obligations(self.db)

    def test_initial_state_has_one_active_45_day_obligation(self):
        obligations = self._obligations()
        self.assertEqual(len(obligations), 1)
        self.assertEqual(obligations[0]["deadline_value"], 45)
        self.assertEqual(obligations[0]["deadline_unit"], "calendar_days")
        self.assertEqual(obligations[0]["status"], "approved")
        self.assertEqual(self._candidate()["status"], "pending_review")

    def test_approval_supersedes_old_and_creates_new_active_version(self):
        new_id = review_amendment(
            self._candidate()["id"], "approved", "reviewer-1",
            "Verified source amendment and target clause.", self.db,
        )
        obligations = self._obligations()
        self.assertEqual(len(obligations), 2)
        old = next(o for o in obligations if o["version"] == 1)
        new = next(o for o in obligations if o["version"] == 2)
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(old["deadline_value"], 45)
        self.assertEqual(new["id"], new_id)
        self.assertEqual(new["status"], "approved")
        self.assertEqual(new["deadline_value"], 30)
        self.assertEqual(new["effective_from"], "2026-01-01")
        self.assertEqual(new["previous_version_id"], old["id"])
        self.assertEqual(new["deliverables"], old["deliverables"])
        self.assertEqual(self._candidate()["applied_obligation_id"], new_id)

    def test_rejection_leaves_original_active_and_creates_no_version(self):
        result = review_amendment(
            self._candidate()["id"], "rejected", "reviewer-2",
            "Rejected for test; do not apply.", self.db,
        )
        self.assertIsNone(result)
        obligations = self._obligations()
        self.assertEqual(len(obligations), 1)
        self.assertEqual(obligations[0]["status"], "approved")
        self.assertEqual(obligations[0]["deadline_value"], 45)
        self.assertEqual(self._candidate()["status"], "rejected")

    def test_duplicate_application_is_blocked(self):
        candidate_id = self._candidate()["id"]
        review_amendment(candidate_id, "approved", "reviewer-1", "Approved once.", self.db)
        with self.assertRaisesRegex(AmendmentWorkflowError, "repeat application is blocked"):
            review_amendment(candidate_id, "approved", "reviewer-1", "Try twice.", self.db)
        self.assertEqual(len(self._obligations()), 2)

    def test_wrong_old_value_blocks_approval_without_mutation(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE amendment_candidates SET old_value=99 WHERE id=?", (self._candidate()["id"],))
        with self.assertRaisesRegex(AmendmentWorkflowError, "found 0"):
            review_amendment(self._candidate()["id"], "approved", "reviewer", "Check match.", self.db)
        self.assertEqual(len(self._obligations()), 1)
        self.assertEqual(self._obligations()[0]["status"], "approved")
        self.assertEqual(self._candidate()["status"], "pending_review")

    def test_ambiguous_multiple_active_matches_block_approval(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                """INSERT INTO obligations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("OBL-DUPLICATE", "ABC Private Equity Fund IV", "LP-A", "LP-A Side Letter",
                 "Clause 4.2", '["duplicate deliverable"]', "quarterly", 45, "calendar_days",
                 "2025-01-01", "approved", 2, None, None, "2026-01-01T00:00:00+00:00", "test"),
            )
        with self.assertRaisesRegex(AmendmentWorkflowError, "found 2"):
            review_amendment(self._candidate()["id"], "approved", "reviewer", "Resolve ambiguity.", self.db)
        self.assertEqual(sum(o["status"] == "approved" for o in self._obligations()), 2)
        self.assertEqual(self._candidate()["status"], "pending_review")

    def test_atomic_rollback_when_new_version_insert_fails(self):
        candidate_id = self._candidate()["id"]
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                """CREATE TRIGGER force_version_insert_failure
                   BEFORE INSERT ON obligations WHEN NEW.version = 2
                   BEGIN SELECT RAISE(ABORT, 'forced insert failure for atomicity test'); END;"""
            )
        with self.assertRaises(sqlite3.IntegrityError):
            review_amendment(candidate_id, "approved", "reviewer", "Atomicity test.", self.db)
        obligations = self._obligations()
        self.assertEqual(len(obligations), 1)
        self.assertEqual(obligations[0]["status"], "approved")
        self.assertEqual(obligations[0]["deadline_value"], 45)
        self.assertEqual(self._candidate()["status"], "pending_review")
        actions = [e["action"] for e in get_audit_events(self.db)]
        self.assertNotIn("obligation_superseded", actions)
        self.assertNotIn("obligation_version_created", actions)

    def test_reviewer_and_review_note_are_required(self):
        with self.assertRaisesRegex(AmendmentWorkflowError, "Reviewer identifier is required"):
            review_amendment(self._candidate()["id"], "approved", "", "Note", self.db)
        with self.assertRaisesRegex(AmendmentWorkflowError, "review note is required"):
            review_amendment(self._candidate()["id"], "approved", "reviewer", " ", self.db)
        self.assertEqual(self._candidate()["status"], "pending_review")

    def test_approval_writes_audit_events(self):
        review_amendment(self._candidate()["id"], "approved", "reviewer-3", "Audit test.", self.db)
        actions = [e["action"] for e in get_audit_events(self.db)]
        self.assertIn("obligation_superseded", actions)
        self.assertIn("obligation_version_created", actions)
        self.assertIn("amendment_approved", actions)

    def test_rejection_writes_audit_event(self):
        review_amendment(self._candidate()["id"], "rejected", "reviewer-4", "Reject test.", self.db)
        actions = [e["action"] for e in get_audit_events(self.db)]
        self.assertIn("amendment_rejected", actions)

    def test_reset_restores_initial_state_only_in_lab_database(self):
        review_amendment(self._candidate()["id"], "approved", "reviewer", "Reset test.", self.db)
        reset_scenario(self.db)
        obligations = self._obligations()
        self.assertEqual(len(obligations), 1)
        self.assertEqual(obligations[0]["deadline_value"], 45)
        self.assertEqual(obligations[0]["status"], "approved")
        self.assertEqual(self._candidate()["status"], "pending_review")

    def test_invalid_decision_is_rejected(self):
        with self.assertRaisesRegex(AmendmentWorkflowError, "Decision must"):
            review_amendment(self._candidate()["id"], "maybe", "reviewer", "Note", self.db)


if __name__ == "__main__":
    unittest.main(verbosity=2)
