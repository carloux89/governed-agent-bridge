"""Approval queue engine (build plan §1.2).

Every action an agent proposes is an object: action, parameters, risk
class, planner justification, and an expiry deadline. Rules:

- ``READ`` proposals are auto-approved at submission time.
- ``WRITE``, ``EXECUTE``, ``SEND``, ``DELETE`` and ``PRINT`` proposals stay
  PENDING until a human explicitly approves or denies them.
- A pending proposal whose deadline passes is EXPIRED: default-deny. It can
  no longer be approved, and ``is_authorized`` is False for it.

State lives in memory and, when a journal path is given, every event is
also appended to a JSONL journal so the queue can be rebuilt by replay.

Standard library only.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path


class ActionClass(str, Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    SEND = "send"
    DELETE = "delete"
    PRINT = "print"  # plan §1.2 lists printing among sensitive actions


# Classes that never execute without an explicit human "yes".
SENSITIVE_CLASSES = frozenset(
    {ActionClass.WRITE, ActionClass.EXECUTE, ActionClass.SEND,
     ActionClass.DELETE, ActionClass.PRINT}
)


class Status(str, Enum):
    PENDING = "pending"
    AUTO_APPROVED = "auto_approved"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


class ProposalExpired(Exception):
    """Raised when deciding a proposal whose deadline has passed."""


@dataclass
class Proposal:
    proposal_id: str
    action: str
    action_class: ActionClass
    params: dict = field(default_factory=dict)
    justification: str = ""
    created_at: float = 0.0
    expires_at: float = 0.0
    status: Status = Status.PENDING
    decided_by: str | None = None
    decided_at: float | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["action_class"] = self.action_class.value
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Proposal":
        d = dict(d)
        d["action_class"] = ActionClass(d["action_class"])
        d["status"] = Status(d["status"])
        return cls(**d)


class ApprovalQueue:
    """In-memory approval queue with an optional JSONL journal."""

    def __init__(self, journal_path: str | Path | None = None, clock=time.time):
        self._clock = clock
        self._proposals: dict[str, Proposal] = {}
        self.journal_path = Path(journal_path) if journal_path else None
        if self.journal_path and self.journal_path.exists():
            self._replay()

    # -- journal ---------------------------------------------------------
    def _record(self, event: str, proposal: Proposal) -> None:
        if not self.journal_path:
            return
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        row = {"event": event, "proposal": proposal.to_dict()}
        with self.journal_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _replay(self) -> None:
        with self.journal_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                proposal = Proposal.from_dict(row["proposal"])
                self._proposals[proposal.proposal_id] = proposal

    # -- submission ------------------------------------------------------
    def submit(
        self,
        action: str,
        action_class: ActionClass | str,
        params: dict | None = None,
        justification: str = "",
        ttl_seconds: float = 300.0,
    ) -> Proposal:
        action_class = ActionClass(action_class)
        now = self._clock()
        proposal = Proposal(
            proposal_id=uuid.uuid4().hex,
            action=action,
            action_class=action_class,
            params=params if params is not None else {},
            justification=justification,
            created_at=now,
            expires_at=now + ttl_seconds,
        )
        if action_class not in SENSITIVE_CLASSES:
            proposal.status = Status.AUTO_APPROVED
            proposal.decided_by = "policy:auto-read"
            proposal.decided_at = now
            self._proposals[proposal.proposal_id] = proposal
            self._record("auto_approved", proposal)
        else:
            self._proposals[proposal.proposal_id] = proposal
            self._record("submitted", proposal)
        return proposal

    # -- decisions -------------------------------------------------------
    def _get(self, proposal_id: str) -> Proposal:
        try:
            return self._proposals[proposal_id]
        except KeyError:
            raise KeyError(f"unknown proposal_id: {proposal_id}") from None

    def effective_status(self, proposal_id: str) -> Status:
        """Status with lazy expiry applied (default-deny on timeout)."""
        proposal = self._get(proposal_id)
        if proposal.status is Status.PENDING and self._clock() > proposal.expires_at:
            proposal.status = Status.EXPIRED
            self._record("expired", proposal)
        return proposal.status

    def _decide(self, proposal_id: str, decided_by: str, approve: bool) -> Proposal:
        proposal = self._get(proposal_id)
        status = self.effective_status(proposal_id)
        if status is Status.EXPIRED:
            raise ProposalExpired(
                f"proposal {proposal_id} expired at {proposal.expires_at}"
            )
        if status is not Status.PENDING:
            raise ValueError(
                f"proposal {proposal_id} already decided: {status.value}"
            )
        proposal.status = Status.APPROVED if approve else Status.DENIED
        proposal.decided_by = decided_by
        proposal.decided_at = self._clock()
        self._record("approved" if approve else "denied", proposal)
        return proposal

    def approve(self, proposal_id: str, decided_by: str) -> Proposal:
        return self._decide(proposal_id, decided_by, approve=True)

    def deny(self, proposal_id: str, decided_by: str) -> Proposal:
        return self._decide(proposal_id, decided_by, approve=False)

    # -- queries ---------------------------------------------------------
    def is_authorized(self, proposal_id: str) -> bool:
        """True only for auto-approved (READ) or human-approved proposals."""
        return self.effective_status(proposal_id) in (
            Status.AUTO_APPROVED,
            Status.APPROVED,
        )

    def pending(self) -> list[Proposal]:
        return [
            p for p in self._proposals.values()
            if self.effective_status(p.proposal_id) is Status.PENDING
        ]

    def all_proposals(self) -> list[Proposal]:
        """Every proposal ever submitted, oldest first (dashboard view)."""
        proposals = sorted(
            self._proposals.values(), key=lambda p: p.created_at
        )
        for proposal in proposals:
            self.effective_status(proposal.proposal_id)
        return proposals

    def get(self, proposal_id: str) -> Proposal:
        proposal = self._get(proposal_id)
        self.effective_status(proposal_id)
        return proposal
