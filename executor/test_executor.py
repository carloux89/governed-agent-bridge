"""Integration tests for the governed executor.

Covers: the full SYN-2026-001 scenario end-to-end (classifier + approvals
+ executor + audit chain), a refused sensitive step, an expired proposal
(default-deny), and a sandbox-escape attempt.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from approvals.engine import ApprovalQueue
from auditlog.chain import AuditLog, verify_chain
from dossier import resolve_dossier_dir
from executor.executor import Executor
from executor.sandbox import Sandbox, SandboxEscape
from planner.planner import MockPlanner

DOSSIER_DIR = resolve_dossier_dir()
DOSSIER_FILES = [
    "01-courriel-attribution.md",
    "02-offre-bail-fictive.md",
    "03-liste-exigences.md",
    "04-inventaire-pieces.md",
]
TASK = (
    "Prépare le dossier SYN-2026-001 : échéances, liste de documents, "
    "résumé du cas, paquet d'impression."
)


def _seed_sandbox(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name in DOSSIER_FILES:
        shutil.copyfile(DOSSIER_DIR / name, root / name)


def _demo_plan() -> dict:
    return MockPlanner().plan(TASK, {"dossier_id": "SYN-2026-001"})


class ExecutorTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gab-exec-test-"))
        self.sandbox_root = self.tmp / "sandbox"
        _seed_sandbox(self.sandbox_root)
        self.audit_path = self.tmp / "audit-log.jsonl"
        self.queue = ApprovalQueue()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_executor(self, **kwargs) -> Executor:
        kwargs.setdefault("queue", self.queue)
        return Executor(self.sandbox_root, AuditLog(self.audit_path), **kwargs)

    def audit_actions(self) -> list[str]:
        return [
            json.loads(line)["action"]
            for line in self.audit_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class TestSandboxGuard(unittest.TestCase):
    def test_resolve_inside_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Sandbox(tmp)
            self.assertEqual(sandbox.resolve("a/b.md"),
                             (Path(tmp) / "a" / "b.md").resolve())

    def test_resolve_dotdot_escape_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Sandbox(tmp)
            with self.assertRaises(SandboxEscape):
                sandbox.resolve("../outside.md")

    def test_resolve_absolute_escape_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Sandbox(tmp)
            with self.assertRaises(SandboxEscape):
                sandbox.resolve("/etc/passwd")


class TestFullDemoRun(ExecutorTestBase):
    def test_full_run_with_both_approvals(self):
        decisions = []

        def approve_both(proposal):
            decisions.append(proposal.action)
            self.queue.approve(proposal.proposal_id, decided_by="test-human")

        result = self.make_executor(pause_hook=approve_both).run(_demo_plan())

        # All 6 steps executed; both pauses were approved by the "human".
        self.assertEqual([o.outcome for o in result.outcomes], ["executed"] * 6)
        self.assertEqual(
            decisions,
            ["draft_case_summary_letter", "assemble_export_package"],
        )

        # Internal outputs were generated inside the sandbox.
        echeances = (self.sandbox_root / "echeances.md").read_text("utf-8")
        for date in ("20 octobre 2026", "28 octobre 2026",
                     "15 novembre 2026", "30 juin 2027"):
            self.assertIn(date, echeances)
        checklist = (self.sandbox_root / "checklist.md").read_text("utf-8")
        self.assertIn("Preuve de revenu", checklist)
        self.assertIn("⚠", checklist)  # missing piece flagged
        self.assertTrue((self.sandbox_root / "lettre-resume-cas.md").exists())
        pdf = (self.sandbox_root / "paquet-SYN-2026-001.pdf").read_bytes()
        self.assertTrue(pdf.startswith(b"%PDF-1.4"))

        # Every stage is in the audit chain, and the chain verifies.
        actions = self.audit_actions()
        self.assertEqual(actions[0], "run_started")
        self.assertEqual(actions[-1], "run_finished")
        self.assertEqual(actions.count("step_classified"), 6)
        self.assertEqual(actions.count("proposal_submitted"), 6)
        self.assertEqual(actions.count("proposal_decided"), 2)
        self.assertEqual(actions.count("step_executed"), 6)
        verification = verify_chain(self.audit_path)
        self.assertTrue(verification.ok, str(verification))


class TestRefusedStep(ExecutorTestBase):
    def test_refused_step_is_skipped_and_logged(self):
        def deny_all(proposal):
            self.queue.deny(proposal.proposal_id, decided_by="test-human")

        plan = {
            "task": "refusal case", "planner": "test",
            "steps": [{
                "step": 1, "action": "draft_case_summary_letter",
                "risk_class": "write",
                "params": {"output": "lettre-resume-cas.md",
                           "dossier_id": "SYN-2026-001"},
                "justification": "sensitive content step",
            }],
        }
        result = self.make_executor(pause_hook=deny_all).run(plan)
        self.assertEqual(result.outcomes[0].outcome, "skipped_denied")
        self.assertFalse((self.sandbox_root / "lettre-resume-cas.md").exists())
        actions = self.audit_actions()
        self.assertIn("proposal_decided", actions)
        self.assertIn("step_skipped", actions)
        self.assertTrue(verify_chain(self.audit_path).ok)


class TestExpiredProposal(ExecutorTestBase):
    def test_expired_proposal_is_default_denied(self):
        # Nobody decides: the short TTL lapses and the step is skipped.
        plan = {
            "task": "expiry case", "planner": "test",
            "steps": [{
                "step": 1, "action": "assemble_export_package",
                "risk_class": "write",
                "params": {"format": "pdf", "output": "paquet.pdf",
                           "includes": ["01-courriel-attribution.md"],
                           "dossier_id": "SYN-2026-001"},
                "justification": "export package",
            }],
        }
        executor = self.make_executor(ttl_seconds=0.2)
        result = executor.run(plan)
        self.assertEqual(result.outcomes[0].outcome, "skipped_expired")
        self.assertFalse((self.sandbox_root / "paquet.pdf").exists())
        self.assertIn("step_skipped", self.audit_actions())
        self.assertTrue(verify_chain(self.audit_path).ok)


class TestSandboxEscapeAttempt(ExecutorTestBase):
    def test_escape_step_is_blocked_and_nothing_written_outside(self):
        plan = {
            "task": "escape case", "planner": "test",
            "steps": [{
                "step": 1, "action": "extract_deadlines",
                "risk_class": "read",
                "params": {"output": "../evil-echeances.md",
                           "dossier_id": "SYN-2026-001"},
                "justification": "output path escapes the sandbox",
            }],
        }
        result = self.make_executor().run(plan)
        self.assertEqual(result.outcomes[0].outcome, "blocked_escape")
        self.assertFalse((self.tmp / "evil-echeances.md").exists())
        self.assertIn("step_blocked", self.audit_actions())
        self.assertTrue(verify_chain(self.audit_path).ok)


if __name__ == "__main__":
    unittest.main()
