"""Tests for the dashboard: rendering, decision wiring, chain verification.

The server is started on an ephemeral loopback port; requests go through
urllib against 127.0.0.1 only.
"""

import shutil
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

from approvals.engine import ApprovalQueue
from auditlog.chain import AuditLog
from dashboard.server import DashboardState, make_server


class DashboardTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gab-dash-test-"))
        self.audit_path = self.tmp / "audit-log.jsonl"
        self.queue = ApprovalQueue()
        self.state = DashboardState(queue=self.queue,
                                    audit_log_path=self.audit_path)
        self.server = make_server(self.state, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def get(self, path: str = "/") -> str:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}{path}", timeout=5
        ) as response:
            return response.read().decode("utf-8")

    def post_decide(self, proposal_id: str, decision: str) -> str:
        data = urllib.parse.urlencode(
            {"proposal_id": proposal_id, "decision": decision}
        ).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/decide", data=data, method="POST"
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.read().decode("utf-8")


class TestDashboard(DashboardTestBase):
    def test_page_has_all_sections(self):
        body = self.get("/")
        for marker in ("Governed Agent Bridge", "Pending approvals",
                       "Decision history", "Audit log", "Verify the chain",
                       "Your agent. Your PC. Your rules."):
            self.assertIn(marker, body)

    def test_pending_proposal_card_and_approval_flow(self):
        proposal = self.queue.submit(
            action="draft_case_summary_letter", action_class="write",
            params={"output": "lettre-resume-cas.md"},
            justification="Contains personal (fictional) case data.",
        )
        body = self.get("/")
        self.assertIn("draft_case_summary_letter", body)
        self.assertIn("Contains personal (fictional) case data.", body)
        self.assertIn("Approve", body)
        self.assertIn("Refuse", body)

        self.post_decide(proposal.proposal_id, "approve")
        decided = self.queue.get(proposal.proposal_id)
        self.assertEqual(decided.status.value, "approved")
        self.assertEqual(decided.decided_by, "you (dashboard)")
        body = self.get("/")
        self.assertIn("No action is waiting for you.", body)

    def test_refusal_flow(self):
        proposal = self.queue.submit(
            action="assemble_export_package", action_class="write",
            params={"output": "paquet.pdf"}, justification="Export package.",
        )
        self.post_decide(proposal.proposal_id, "deny")
        self.assertEqual(self.queue.get(proposal.proposal_id).status.value,
                         "denied")

    def test_verify_chain_ok(self):
        log = AuditLog(self.audit_path)
        log.append("executor", "run_started", {"task": "t"}, result="ok")
        log.append("executor", "step_executed", {"step": 1}, result="ok")
        body = self.get("/?verify=1")
        self.assertIn("Chain verified", body)
        self.assertIn("step_executed", body)

    def test_verify_chain_names_broken_entry(self):
        log = AuditLog(self.audit_path)
        log.append("executor", "run_started", {"task": "t"}, result="ok")
        log.append("executor", "step_executed", {"step": 1}, result="ok")
        log.append("executor", "run_finished", {}, result="completed")
        # Tamper with a COPY the dashboard points at: edit entry #1's result.
        lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        lines[1] = lines[1].replace('"ok"', '"tampered"')
        tampered = self.tmp / "tampered.jsonl"
        tampered.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.state.audit_log_path = tampered
        body = self.get("/?verify=1")
        self.assertIn("Chain BROKEN at entry #1", body)
        self.assertIn("hash_mismatch", body)
        self.assertIn("class='broken'", body)


if __name__ == "__main__":
    unittest.main()
