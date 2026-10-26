"""Append-only JSONL audit log with a SHA-256 hash chain.

Design (build plan §1.3): every entry records timestamp, actor, action,
parameters, decision and result, plus the SHA-256 hash of the previous
entry. Each entry's own hash is computed over the canonical JSON of the
entry (including ``prev_hash``), so editing any byte of any entry breaks
that entry's hash, and deleting an entry breaks the ``prev_hash`` link of
the following one. ``verify_chain`` pinpoints the exact entry where a
tampered copy breaks.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

GENESIS_HASH = "0" * 64


def _canonical_bytes(entry_without_hash: dict) -> bytes:
    """Deterministic serialization used for hashing."""
    return json.dumps(
        entry_without_hash, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def compute_entry_hash(entry: dict) -> str:
    """SHA-256 over the entry content, excluding the ``hash`` field itself."""
    payload = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class VerifyResult:
    ok: bool
    entries_checked: int
    break_index: int | None = None  # 0-based line index of the first bad entry
    break_seq: int | None = None  # seq value stored in that entry, if readable
    reason: str | None = None  # "hash_mismatch" | "prev_hash_mismatch" | "invalid_json"

    def __str__(self) -> str:
        if self.ok:
            return f"chain OK ({self.entries_checked} entries)"
        return (
            f"chain BROKEN at entry index {self.break_index} "
            f"(seq={self.break_seq}): {self.reason}"
        )


class AuditLog:
    """Append-only writer for one JSONL audit log file.

    The file is only ever opened in append mode. The next entry's ``seq``
    and ``prev_hash`` are derived from the current tail of the file.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _tail(self) -> tuple[int, str]:
        """Return (entry_count, last_hash) for the existing log."""
        if not self.path.exists():
            return 0, GENESIS_HASH
        count = 0
        last_hash = GENESIS_HASH
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                count += 1
                last_hash = json.loads(line)["hash"]
        return count, last_hash

    def append(
        self,
        actor: str,
        action: str,
        params: dict | None = None,
        decision: dict | None = None,
        result: str | None = None,
        ts: str | None = None,
    ) -> dict:
        """Append one entry and return it (including its ``hash``)."""
        seq, prev_hash = self._tail()
        entry = {
            "seq": seq,
            "ts": ts if ts is not None else _utc_now_iso(),
            "actor": actor,
            "action": action,
            "params": params if params is not None else {},
            "decision": decision,
            "result": result,
            "prev_hash": prev_hash,
        }
        entry["hash"] = compute_entry_hash(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def verify(self) -> VerifyResult:
        return verify_chain(self.path)


def verify_chain(path: str | Path) -> VerifyResult:
    """Recompute the whole chain; report the first entry where it breaks.

    - ``hash_mismatch``: the entry's stored hash does not match its content
      (the entry itself was edited).
    - ``prev_hash_mismatch``: the entry's stored ``prev_hash`` does not match
      the previous entry's hash (an entry was removed, inserted or reordered,
      or the previous entry's hash field was edited).
    """
    path = Path(path)
    if not path.exists():
        return VerifyResult(ok=True, entries_checked=0)
    prev_hash = GENESIS_HASH
    checked = 0
    with path.open("r", encoding="utf-8") as fh:
        for index, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                return VerifyResult(
                    ok=False, entries_checked=checked,
                    break_index=index, break_seq=None, reason="invalid_json",
                )
            if entry.get("prev_hash") != prev_hash:
                return VerifyResult(
                    ok=False, entries_checked=checked,
                    break_index=index, break_seq=entry.get("seq"),
                    reason="prev_hash_mismatch",
                )
            if compute_entry_hash(entry) != entry.get("hash"):
                return VerifyResult(
                    ok=False, entries_checked=checked,
                    break_index=index, break_seq=entry.get("seq"),
                    reason="hash_mismatch",
                )
            prev_hash = entry["hash"]
            checked += 1
    return VerifyResult(ok=True, entries_checked=checked)
