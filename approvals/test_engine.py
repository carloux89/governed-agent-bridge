"""Unit tests for the approval engine (plan §1.2 rules)."""

import tempfile
import unittest
from pathlib import Path

from approvals.engine import (
    ActionClass,
    ApprovalQueue,
    ProposalExpired,
    Status,
)


class FakeClock:
    def __init__(self, start=1_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestApprovalQueue(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.queue = ApprovalQueue(clock=self.clock)

    def test_read_is_auto_approved(self):
        p = self.queue.submit("read_document", ActionClass.READ,
                              {"document": "01.md"}, "read the dossier")
        self.assertEqual(p.status, Status.AUTO_APPROVED)
        self.assertTrue(self.queue.is_authorized(p.proposal_id))
        self.assertEqual(self.queue.pending(), [])

    def test_sensitive_classes_stay_pending(self):
        for cls in (ActionClass.WRITE, ActionClass.EXECUTE, ActionClass.SEND,
                    ActionClass.DELETE, ActionClass.PRINT):
            p = self.queue.submit("some_action", cls, {}, "justification")
            self.assertEqual(p.status, Status.PENDING, cls)
            self.assertFalse(self.queue.is_authorized(p.proposal_id), cls)
        self.assertEqual(len(self.queue.pending()), 5)

    def test_approve_authorizes(self):
        p = self.queue.submit("draft_letter", ActionClass.WRITE, {}, "j")
        decided = self.queue.approve(p.proposal_id, decided_by="user")
        self.assertEqual(decided.status, Status.APPROVED)
        self.assertEqual(decided.decided_by, "user")
        self.assertTrue(self.queue.is_authorized(p.proposal_id))

    def test_deny_blocks(self):
        p = self.queue.submit("send_email", ActionClass.SEND, {}, "j")
        self.queue.deny(p.proposal_id, decided_by="user")
        self.assertFalse(self.queue.is_authorized(p.proposal_id))
        self.assertEqual(self.queue.effective_status(p.proposal_id), Status.DENIED)

    def test_expiry_is_default_deny(self):
        p = self.queue.submit("draft_letter", ActionClass.WRITE, {}, "j",
                              ttl_seconds=60)
        self.clock.advance(61)
        self.assertEqual(self.queue.effective_status(p.proposal_id), Status.EXPIRED)
        self.assertFalse(self.queue.is_authorized(p.proposal_id))
        with self.assertRaises(ProposalExpired):
            self.queue.approve(p.proposal_id, decided_by="user")

    def test_double_decision_rejected(self):
        p = self.queue.submit("draft_letter", ActionClass.WRITE, {}, "j")
        self.queue.approve(p.proposal_id, decided_by="user")
        with self.assertRaises(ValueError):
            self.queue.deny(p.proposal_id, decided_by="user")

    def test_unknown_proposal_raises(self):
        with self.assertRaises(KeyError):
            self.queue.is_authorized("no-such-id")

    def test_journal_replay_rebuilds_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "approvals.jsonl"
            clock = FakeClock()
            q1 = ApprovalQueue(journal_path=journal, clock=clock)
            read = q1.submit("read_document", ActionClass.READ, {}, "j")
            write = q1.submit("draft_letter", ActionClass.WRITE, {}, "j")
            q1.approve(write.proposal_id, decided_by="user")

            q2 = ApprovalQueue(journal_path=journal, clock=clock)
            self.assertTrue(q2.is_authorized(read.proposal_id))
            self.assertTrue(q2.is_authorized(write.proposal_id))
            self.assertEqual(q2.get(write.proposal_id).decided_by, "user")
            self.assertEqual(q2.pending(), [])


if __name__ == "__main__":
    unittest.main()
