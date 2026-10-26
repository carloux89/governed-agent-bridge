"""Unit tests for the governance classifier."""

import unittest
from pathlib import Path

from classifier.classifier import Verdict, classify_plan, classify_step
from planner.planner import MockPlanner

SANDBOX = Path("/tmp/gab-test-sandbox")


def _demo_plan() -> dict:
    return MockPlanner().plan(
        "Prépare le dossier SYN-2026-001", {"dossier_id": "SYN-2026-001"}
    )


class TestDemoScenarioClassification(unittest.TestCase):
    """The 6 steps of plan §2 under the sandbox ruling."""

    def setUp(self):
        self.results = classify_plan(_demo_plan(), SANDBOX)

    def test_six_steps_classified(self):
        self.assertEqual(len(self.results), 6)

    def test_read_and_internal_generation_steps_are_internal(self):
        # Steps 1-2 read inside the sandbox; step 3 generates echeances.md
        # and step 4 the checklist — working files inside the sandbox.
        for result in self.results[:4]:
            self.assertEqual(result.verdict, Verdict.INTERNAL, result.action)
            self.assertFalse(result.requires_approval)

    def test_final_external_effect_steps_require_approval(self):
        # Step 5: the summary letter is an outgoing deliverable.
        # Step 6: the export package leaves the sandbox.
        for result in self.results[4:]:
            self.assertEqual(result.verdict, Verdict.SENSITIVE, result.action)
            self.assertTrue(result.requires_approval)

    def test_letter_step_records_external_effect(self):
        letter = self.results[4]
        self.assertEqual(letter.action, "draft_case_summary_letter")
        external = [e for e in letter.effects if e.scope == "external"]
        self.assertTrue(external)

    def test_internal_steps_map_to_read_for_the_engine(self):
        # READ is the only class the approvals engine auto-approves.
        for result in self.results[:4]:
            self.assertEqual(result.action_class, "read")


class TestClassifierRules(unittest.TestCase):
    def test_send_execute_delete_print_are_sensitive(self):
        for action in ("send_message", "run_command", "delete_file",
                       "print_document"):
            result = classify_step({"action": action, "params": {}}, SANDBOX)
            self.assertEqual(result.verdict, Verdict.SENSITIVE, action)

    def test_delete_inside_sandbox_still_sensitive(self):
        result = classify_step(
            {"action": "delete_file", "params": {"path": "notes.md"}}, SANDBOX
        )
        self.assertEqual(result.verdict, Verdict.SENSITIVE)

    def test_write_file_inside_sandbox_is_internal(self):
        result = classify_step(
            {"action": "write_file", "params": {"path": "draft.md"}}, SANDBOX
        )
        self.assertEqual(result.verdict, Verdict.INTERNAL)

    def test_path_escape_turns_step_sensitive(self):
        result = classify_step(
            {"action": "write_file", "params": {"path": "../outside.md"}},
            SANDBOX,
        )
        self.assertEqual(result.verdict, Verdict.SENSITIVE)
        self.assertTrue(any("escapes the sandbox" in r for r in result.reasons))

    def test_read_with_escaping_document_is_sensitive(self):
        result = classify_step(
            {"action": "read_document",
             "params": {"document": "/etc/passwd"}},
            SANDBOX,
        )
        self.assertEqual(result.verdict, Verdict.SENSITIVE)

    def test_unknown_action_fails_closed(self):
        result = classify_step(
            {"action": "teleport_files", "params": {}}, SANDBOX
        )
        self.assertEqual(result.verdict, Verdict.SENSITIVE)
        self.assertFalse(result.known_action)

    def test_classification_is_json_ready(self):
        import json

        for result in classify_plan(_demo_plan(), SANDBOX):
            json.dumps(result.to_dict())  # must not raise


if __name__ == "__main__":
    unittest.main()
