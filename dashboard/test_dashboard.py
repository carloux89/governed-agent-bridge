"""Tests for the dashboard: rendering, decision wiring, chain verification.

The server is started on an ephemeral loopback port; requests go through
urllib against 127.0.0.1 only.
"""

import errno
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


def _make_server_or_skip(state, server_factory=make_server):
    """Start the dashboard server for a test.

    Some sandboxed environments forbid binding even a loopback socket
    (``PermissionError: [Errno 1] Operation not permitted``). That is an
    environment limitation, not a product failure: declare the test
    skipped with an explicit reason instead of erroring. Any other
    ``OSError`` still propagates as a real error. When binding is
    permitted the real server is returned and the tests exercise real
    HTTP, unchanged.
    """
    try:
        return server_factory(state, port=0)
    except OSError as exc:
        if isinstance(exc, PermissionError) or exc.errno in (
            errno.EPERM,
            errno.EACCES,
        ):
            raise unittest.SkipTest(
                "socket binding not permitted in this environment "
                f"({exc})"
            ) from exc
        raise


class DashboardTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gab-dash-test-"))
        self.audit_path = self.tmp / "audit-log.jsonl"
        self.queue = ApprovalQueue()
        self.state = DashboardState(queue=self.queue,
                                    audit_log_path=self.audit_path)
        try:
            self.server = _make_server_or_skip(self.state)
        except unittest.SkipTest:
            shutil.rmtree(self.tmp, ignore_errors=True)
            raise
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


class TestServerBindGuard(unittest.TestCase):
    """The bind guard: permission failures become explicit skips,
    everything else still surfaces as an error."""

    def _state(self):
        return DashboardState(queue=ApprovalQueue(),
                              audit_log_path=Path("unused.jsonl"))

    @staticmethod
    def _raising(exc):
        def factory(state, port=0):
            raise exc
        return factory

    def test_permission_error_becomes_skip_with_reason(self):
        factory = self._raising(
            PermissionError(errno.EPERM, "Operation not permitted"))
        with self.assertRaises(unittest.SkipTest) as ctx:
            _make_server_or_skip(self._state(), server_factory=factory)
        self.assertIn("socket binding not permitted", str(ctx.exception))

    def test_oserror_with_eperm_errno_becomes_skip(self):
        exc = OSError("Operation not permitted")
        exc.errno = errno.EPERM  # bypasses the PermissionError subclassing
        with self.assertRaises(unittest.SkipTest):
            _make_server_or_skip(self._state(),
                                 server_factory=self._raising(exc))

    def test_other_oserrors_still_propagate(self):
        exc = OSError(errno.EADDRINUSE, "Address already in use")
        with self.assertRaises(OSError) as ctx:
            _make_server_or_skip(self._state(),
                                 server_factory=self._raising(exc))
        self.assertNotIsInstance(ctx.exception, unittest.SkipTest)

    def test_successful_bind_returns_the_server(self):
        sentinel = object()
        result = _make_server_or_skip(
            self._state(), server_factory=lambda state, port=0: sentinel)
        self.assertIs(result, sentinel)


if __name__ == "__main__":
    unittest.main()
