"""Governance classifier (plan §1.2, governance layer).

Formalizes the sandbox ruling as data + code: effects that stay inside
the governed sandbox workspace are internal (auto-approved, audit-logged);
any effect outside the sandbox is sensitive and requires explicit human
approval. Unknown actions fail closed.
"""

from .classifier import Classification, Verdict, classify_plan, classify_step
from .rules import ACTION_RULES

__all__ = [
    "ACTION_RULES",
    "Classification",
    "Verdict",
    "classify_plan",
    "classify_step",
]
