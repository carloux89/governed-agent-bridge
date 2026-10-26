"""Unit tests for the hash-chained audit log, including the tamper case."""

import json
import tempfile
import unittest
from pathlib import Path

from auditlog.chain import GENESIS_HASH, AuditLog, verify_chain


class TestAuditLog(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "audit.jsonl"
        self.log = AuditLog(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def _append_three(self):
        self.log.append("agent", "read_document", {"document": "01.md"},
                        decision={"kind": "auto"}, result="ok",
                        ts="2026-10-26T12:00:00+00:00")
        self.log.append("agent", "draft_case_summary_letter", {},
                        decision={"kind": "approved", "by": "user"},
                        result="ok", ts="2026-10-26T12:01:00+00:00")
        self.log.append("agent", "assemble_export_package", {"format": "pdf"},
                        decision={"kind": "approved", "by": "user"},
                        result="ok", ts="2026-10-26T12:02:00+00:00")

    def test_append_links_chain(self):
        self._append_three()
        rows = [json.loads(l) for l in self.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["seq"] for r in rows], [0, 1, 2])
        self.assertEqual(rows[0]["prev_hash"], GENESIS_HASH)
        self.assertEqual(rows[1]["prev_hash"], rows[0]["hash"])
        self.assertEqual(rows[2]["prev_hash"], rows[1]["hash"])
        for row in rows:
            self.assertEqual(len(row["hash"]), 64)

    def test_verify_clean_chain(self):
        self._append_three()
        result = verify_chain(self.path)
        self.assertTrue(result.ok)
        self.assertEqual(result.entries_checked, 3)

    def test_verify_empty_log_is_ok(self):
        result = verify_chain(self.path)
        self.assertTrue(result.ok)
        self.assertEqual(result.entries_checked, 0)

    def test_tampered_entry_is_pinpointed(self):
        """The demo's signature beat: edit one byte in a copy, verification
        names the exact entry where the chain breaks."""
        self._append_three()
        lines = self.path.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[1])
        entry["result"] = "tampered"
        lines[1] = json.dumps(entry, ensure_ascii=False)
        tampered = Path(self._tmp.name) / "audit-tampered.jsonl"
        tampered.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = verify_chain(tampered)
        self.assertFalse(result.ok)
        self.assertEqual(result.break_index, 1)
        self.assertEqual(result.break_seq, 1)
        self.assertEqual(result.reason, "hash_mismatch")

    def test_deleted_entry_breaks_prev_hash_link(self):
        self._append_three()
        lines = self.path.read_text(encoding="utf-8").splitlines()
        del lines[1]  # silently remove the middle entry
        broken = Path(self._tmp.name) / "audit-deleted.jsonl"
        broken.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = verify_chain(broken)
        self.assertFalse(result.ok)
        self.assertEqual(result.break_index, 1)
        self.assertEqual(result.reason, "prev_hash_mismatch")

    def test_original_log_untouched_by_tamper_tests(self):
        self._append_three()
        self.assertTrue(self.log.verify().ok)


if __name__ == "__main__":
    unittest.main()
