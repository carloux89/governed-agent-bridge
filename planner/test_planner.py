"""Unit tests for the planner interface, the mock, and the live Spark
planner (with fake transports — no network, no bridge, no PC)."""

import base64
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from auditlog.chain import AuditLog
from planner.planner import (
    KNOWN_ACTIONS,
    MockPlanner,
    SparkPlanner,
    planner_from_env,
)
from planner.transport import BridgeSparkTransport, SparkTransportError

DEMO_TASK = (
    "Prépare le dossier SYN-2026-001 : échéances, liste de documents, "
    "résumé du cas, paquet d'impression."
)
DEMO_CONTEXT = {"dossier_id": "SYN-2026-001"}

VALID_SPARK_REPLY = json.dumps({
    "steps": [
        {"step": 1, "action": "read_document", "risk_class": "read",
         "params": {"document": "01-courriel-attribution.md",
                    "dossier_id": "SYN-2026-001"},
         "justification": "Read the allocation email for its deadlines."},
        {"step": 2, "action": "extract_deadlines", "risk_class": "read",
         "params": {"output": "echeances.md", "dossier_id": "SYN-2026-001"},
         "justification": "Collect the dossier deadlines into a summary."},
        {"step": 3, "action": "draft_case_summary_letter",
         "risk_class": "write",
         "params": {"output": "lettre-resume-cas.md",
                    "dossier_id": "SYN-2026-001"},
         "justification": "Draft the case summary letter for approval."},
    ]
})


def _audit_entries(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestMockPlanner(unittest.TestCase):
    def setUp(self):
        self.planner = MockPlanner()

    def test_demo_task_returns_six_step_plan(self):
        plan = self.planner.plan(DEMO_TASK, DEMO_CONTEXT)
        steps = plan["steps"]
        self.assertEqual(len(steps), 6)
        self.assertEqual([s["step"] for s in steps], [1, 2, 3, 4, 5, 6])
        self.assertEqual(plan["dossier_id"], "SYN-2026-001")

    def test_demo_plan_governance_shape(self):
        """Plan §2: steps 1-4 auto (read), steps 5-6 sensitive (write)."""
        plan = self.planner.plan("prepare the dossier",
                                 {"dossier_id": "SYN-2026-001"})
        classes = [s["risk_class"] for s in plan["steps"]]
        self.assertEqual(classes[:4], ["read"] * 4)
        self.assertEqual(classes[4:], ["write", "write"])
        for step in plan["steps"]:
            self.assertTrue(step["justification"])

    def test_plan_is_deterministic_and_json_serializable(self):
        task = "Prépare le dossier SYN-2026-001"
        first = self.planner.plan(task, {"dossier_id": "SYN-2026-001"})
        second = self.planner.plan(task, {"dossier_id": "SYN-2026-001"})
        self.assertEqual(first, second)
        json.dumps(first)  # must not raise

    def test_unknown_task_gets_read_only_fallback(self):
        plan = self.planner.plan("Do something entirely different")
        self.assertEqual(len(plan["steps"]), 1)
        self.assertEqual(plan["steps"][0]["risk_class"], "read")


class TestSparkPlanner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audit_path = Path(self.tmp.name) / "audit-log.jsonl"
        self.audit_log = AuditLog(self.audit_path)

    def _planner(self, transport) -> SparkPlanner:
        return SparkPlanner(transport=transport, audit_log=self.audit_log)

    def test_valid_spark_plan_is_used_and_attributed(self):
        seen_payloads = []

        def fake_transport(payload):
            seen_payloads.append(payload)
            return VALID_SPARK_REPLY

        plan = self._planner(fake_transport).plan(DEMO_TASK, DEMO_CONTEXT)

        self.assertEqual(plan["planner"], "spark")
        self.assertEqual(plan["planner_requested"], "spark")
        self.assertEqual(plan["model"], "muse-spark-1.3")
        self.assertNotIn("planner_fallback_reason", plan)
        self.assertEqual(
            [s["action"] for s in plan["steps"]],
            ["read_document", "extract_deadlines",
             "draft_case_summary_letter"],
        )
        # The payload sent to the model demands the strict plan schema.
        self.assertEqual(seen_payloads[0]["model"], "muse-spark-1.3")
        system = seen_payloads[0]["messages"][0]["content"]
        self.assertIn("STRICT JSON ONLY", system)
        self.assertIn("read_document", system)

        entries = _audit_entries(self.audit_path)
        produced = [e for e in entries if e["action"] == "plan_produced"]
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0]["actor"], "planner")
        self.assertEqual(produced[0]["params"]["planner"], "spark")
        self.assertEqual(produced[0]["params"]["steps"], 3)
        self.assertFalse(
            any(e["action"] == "planner_fallback" for e in entries))
        self.assertTrue(self.audit_log.verify().ok)

    def test_malformed_json_falls_back_to_mock_with_reason(self):
        planner = self._planner(lambda payload: "Sorry, no JSON here.")
        plan = planner.plan(DEMO_TASK, DEMO_CONTEXT)

        mock_plan = MockPlanner().plan(DEMO_TASK, DEMO_CONTEXT)
        self.assertEqual(plan["planner"], "mock")
        self.assertEqual(plan["steps"], mock_plan["steps"])
        self.assertEqual(plan["planner_requested"], "spark")
        self.assertIn("no JSON object", plan["planner_fallback_reason"])
        self.assertEqual(planner.last_fallback_reason,
                         plan["planner_fallback_reason"])

        entries = _audit_entries(self.audit_path)
        fallbacks = [e for e in entries if e["action"] == "planner_fallback"]
        self.assertEqual(len(fallbacks), 1)
        self.assertEqual(fallbacks[0]["result"], "fallback_to_mock")
        self.assertIn("no JSON object", fallbacks[0]["params"]["reason"])
        self.assertTrue(self.audit_log.verify().ok)

    def test_unknown_action_falls_back_to_mock(self):
        reply = json.dumps({"steps": [
            {"step": 1, "action": "teleport_files", "risk_class": "read",
             "params": {}, "justification": "Beam the files over."},
        ]})
        plan = self._planner(lambda payload: reply).plan(
            DEMO_TASK, DEMO_CONTEXT)

        self.assertEqual(plan["planner"], "mock")
        self.assertIn("unknown action 'teleport_files'",
                      plan["planner_fallback_reason"])
        entries = _audit_entries(self.audit_path)
        fallbacks = [e for e in entries if e["action"] == "planner_fallback"]
        self.assertEqual(len(fallbacks), 1)
        self.assertIn("teleport_files", fallbacks[0]["params"]["reason"])

    def test_transport_exception_falls_back_to_mock(self):
        def broken_transport(payload):
            raise RuntimeError("bridge down")

        plan = self._planner(broken_transport).plan(DEMO_TASK, DEMO_CONTEXT)

        self.assertEqual(plan["planner"], "mock")
        self.assertIn("bridge down", plan["planner_fallback_reason"])
        entries = _audit_entries(self.audit_path)
        fallbacks = [e for e in entries if e["action"] == "planner_fallback"]
        self.assertEqual(len(fallbacks), 1)
        self.assertEqual(fallbacks[0]["params"]["requested_planner"], "spark")
        self.assertEqual(fallbacks[0]["params"]["fell_back_to"], "mock")
        self.assertTrue(self.audit_log.verify().ok)

    def test_unconfigured_bridge_falls_back_with_config_reason(self):
        """SparkPlanner without bridge config: no crash at construction,
        the plan falls back to the mock and records why."""
        with mock.patch.dict(os.environ, {}, clear=True):
            planner = SparkPlanner(audit_log=self.audit_log)
        plan = planner.plan(DEMO_TASK, DEMO_CONTEXT)

        self.assertEqual(plan["planner"], "mock")
        self.assertEqual(plan["planner_requested"], "spark")
        self.assertIn("not configured", plan["planner_fallback_reason"])
        entries = _audit_entries(self.audit_path)
        fallbacks = [e for e in entries if e["action"] == "planner_fallback"]
        self.assertEqual(len(fallbacks), 1)
        self.assertIn("not configured", fallbacks[0]["params"]["reason"])

    def test_vocabulary_is_executable_and_classified(self):
        self.assertEqual(
            KNOWN_ACTIONS,
            frozenset({
                "read_document", "inspect_workspace", "extract_deadlines",
                "build_checklist", "draft_case_summary_letter",
                "assemble_export_package",
            }),
        )


class TestPlannerSelection(unittest.TestCase):
    def test_default_is_mock(self):
        self.assertIsInstance(planner_from_env(environ={}), MockPlanner)

    def test_spark_selected_case_insensitively(self):
        planner = planner_from_env(environ={"PLANNER": " Spark "})
        self.assertIsInstance(planner, SparkPlanner)

    def test_unknown_value_stays_on_mock(self):
        planner = planner_from_env(environ={"PLANNER": "whatever"})
        self.assertIsInstance(planner, MockPlanner)


class _FakePC:
    """Simulates the PC side of the chunked deposit protocol.

    Decodes each -EncodedCommand, applies WriteAllText/AppendAllText
    deposits to an in-memory file store, answers the runner call with a
    canned reply, and records every script for assertions.
    """

    _DEPOSIT_RE = re.compile(
        r"(WriteAllText|AppendAllText)\(\(Join-Path \$env:TEMP "
        r"'([^']+)'\), '([^']*)'\)")

    def __init__(self, reply='{"steps": []}', fail_on=None):
        self.reply = reply
        self.fail_on = fail_on  # callable(script, call_number) -> bool
        self.argvs: list[list[str]] = []
        self.scripts: list[str] = []
        self.files: dict[str, str] = {}

    def __call__(self, argv, timeout):
        self.argvs.append(argv)
        script = base64.b64decode(argv[-1]).decode("utf-16-le")
        self.scripts.append(script)
        if self.fail_on is not None and self.fail_on(script, len(self.scripts)):
            return SimpleNamespace(
                returncode=1, stdout=b"",
                stderr="SPARK_BRIDGE_ERROR: boom".encode("cp1252"))
        deposit = self._DEPOSIT_RE.search(script)
        if deposit:
            kind, fname, chunk = deposit.groups()
            if kind == "WriteAllText":
                self.files[fname] = chunk
            else:
                self.files[fname] = self.files.get(fname, "") + chunk
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if "/chat/completions" in script:
            return SimpleNamespace(
                returncode=0, stdout=self.reply.encode("utf-8"), stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


class TestBridgeTransport(unittest.TestCase):
    def _transport(self, runner) -> BridgeSparkTransport:
        return BridgeSparkTransport(
            bridge_path="/fake/bridge.py", target="bridge-host",
            runner=runner,
        )

    @staticmethod
    def _remote_command(argv) -> str:
        """The command line as the PC receives it (post-ssh)."""
        return " ".join(argv[5:])

    def test_chunked_deposit_round_trips_and_runner_is_payload_free(self):
        pc = _FakePC()
        transport = self._transport(pc)
        # A payload large enough to force several deposit chunks.
        marker = "payload-marker-" + "x" * 9000
        payload = {"model": "muse-spark-1.3",
                   "messages": [{"role": "user", "content": marker}]}
        reply = transport(payload)

        self.assertEqual(reply, '{"steps": []}')
        payload_b64 = base64.b64encode(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        expected_chunks = -(-len(payload_b64) // 2000)
        self.assertGreaterEqual(expected_chunks, 3)

        # N deposit calls + exactly 1 runner call, in that order.
        self.assertEqual(len(pc.scripts), expected_chunks + 1)
        deposits, runner_script = pc.scripts[:-1], pc.scripts[-1]
        chunks = []
        fnames = set()
        for index, script in enumerate(deposits):
            match = _FakePC._DEPOSIT_RE.search(script)
            self.assertIsNotNone(match)
            kind, fname, chunk = match.groups()
            self.assertEqual(
                kind, "WriteAllText" if index == 0 else "AppendAllText")
            fnames.add(fname)
            chunks.append(chunk)
            self.assertLessEqual(len(chunk), 2000)
        self.assertEqual(len(fnames), 1)
        fname = fnames.pop()
        self.assertRegex(fname, r"^gab_payload_[0-9a-f]+\.b64$")

        # Every single command stays far below the Windows limit.
        for argv in pc.argvs:
            self.assertEqual(argv[1], "/fake/bridge.py")
            self.assertEqual(argv[2:5], ["ssh", "bridge-host", "--"])
            self.assertEqual(argv[5], "powershell")
            self.assertEqual(argv[-2], "-EncodedCommand")
            self.assertLess(len(argv[-1]), 8000)
            self.assertLess(len(self._remote_command(argv)), 8191)

        # The deposited bytes round-trip byte-identically.
        self.assertEqual("".join(chunks), payload_b64)
        self.assertEqual(
            base64.b64decode(pc.files[fname]),
            json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        # The runner call embeds NO payload: it reads the temp file and
        # the PC-side credential, posts, and deletes the temp file.
        self.assertIn(fname, runner_script)
        self.assertIn("providers.meta.api_key", runner_script)
        self.assertIn("/chat/completions", runner_script)
        self.assertIn("ReadAllText", runner_script)
        self.assertIn("Remove-Item", runner_script)
        self.assertNotIn(marker, runner_script)
        for chunk in chunks:
            self.assertNotIn(chunk[:64], runner_script)

    def test_small_payload_uses_one_deposit_and_one_runner(self):
        pc = _FakePC()
        reply = self._transport(pc)({"model": "muse-spark-1.3",
                                     "messages": []})
        self.assertEqual(reply, '{"steps": []}')
        self.assertEqual(len(pc.scripts), 2)
        self.assertIn("WriteAllText", pc.scripts[0])
        self.assertIn("/chat/completions", pc.scripts[1])

    def test_deposit_failure_raises_and_cleans_up(self):
        pc = _FakePC(fail_on=lambda script, n: n == 2)
        payload = {"model": "muse-spark-1.3",
                   "messages": [{"role": "user", "content": "y" * 9000}]}
        with self.assertRaises(SparkTransportError) as ctx:
            self._transport(pc)(payload)
        self.assertIn("boom", str(ctx.exception))
        # Deposit #1 ok, deposit #2 failed, then a cleanup call for the
        # same temp file (Remove-Item, and NOT the API runner).
        self.assertEqual(len(pc.scripts), 3)
        cleanup = pc.scripts[-1]
        self.assertIn("Remove-Item", cleanup)
        self.assertNotIn("/chat/completions", cleanup)
        fname = _FakePC._DEPOSIT_RE.search(pc.scripts[0]).group(2)
        self.assertIn(fname, cleanup)

    def test_runner_nonzero_exit_raises_transport_error(self):
        pc = _FakePC(
            fail_on=lambda script, n: "/chat/completions" in script)

        with self.assertRaises(SparkTransportError) as ctx:
            self._transport(pc)({"model": "muse-spark-1.3"})
        self.assertIn("boom", str(ctx.exception))

    def test_runner_failure_raises_transport_error(self):
        pc = _FakePC()

        def runner(argv, timeout):
            script = base64.b64decode(argv[-1]).decode("utf-16-le")
            if "/chat/completions" in script:
                raise subprocess.TimeoutExpired(cmd="ssh", timeout=timeout)
            return pc(argv, timeout)

        with self.assertRaises(SparkTransportError):
            self._transport(runner)({"model": "muse-spark-1.3"})

    def test_missing_config_raises_clear_error(self):
        """No argument, no env: construction fails, naming the fix."""
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SparkTransportError) as ctx:
                BridgeSparkTransport(runner=lambda argv, timeout: None)
        self.assertIn("not configured", str(ctx.exception))
        self.assertIn("GAB_BRIDGE_PY", str(ctx.exception))

    def test_partial_config_still_raises(self):
        """Only the bridge path set: the missing host is the error."""
        env = {"GAB_BRIDGE_PY": "/env/bridge.py"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SparkTransportError) as ctx:
                BridgeSparkTransport(runner=lambda argv, timeout: None)
        self.assertIn("GAB_BRIDGE_HOST", str(ctx.exception))

    def test_env_fallbacks_are_used(self):
        env = {"GAB_BRIDGE_PY": "/env/bridge.py",
               "GAB_BRIDGE_HOST": "env-host"}
        with mock.patch.dict(os.environ, env, clear=True):
            transport = BridgeSparkTransport(
                runner=lambda argv, timeout: None)
        self.assertEqual(transport.bridge_path, Path("/env/bridge.py"))
        self.assertEqual(transport.target, "env-host")

    def test_params_beat_env(self):
        env = {"GAB_BRIDGE_PY": "/env/bridge.py",
               "GAB_BRIDGE_HOST": "env-host"}
        with mock.patch.dict(os.environ, env, clear=True):
            transport = BridgeSparkTransport(
                bridge_path="/arg/bridge.py", target="arg-host",
                runner=lambda argv, timeout: None)
        self.assertEqual(transport.bridge_path, Path("/arg/bridge.py"))
        self.assertEqual(transport.target, "arg-host")


if __name__ == "__main__":
    unittest.main()
