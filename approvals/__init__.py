"""Approval engine: risk-classed action proposals with human approval.

Plan §1.2: READ actions are auto-approved; WRITE / EXECUTE / SEND / DELETE
(and PRINT, listed as sensitive in the plan) require an explicit human
approval, with default-deny on expiry.
"""

from .engine import (
    ActionClass,
    ApprovalQueue,
    Proposal,
    ProposalExpired,
    Status,
)

__all__ = [
    "ActionClass",
    "ApprovalQueue",
    "Proposal",
    "ProposalExpired",
    "Status",
]
