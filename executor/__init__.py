"""Governed executor (plan §1.2–§1.3).

Runs a plan step by step: internal steps execute under policy
auto-approval; sensitive steps pause on the approvals queue until a human
decides (default-deny on expiry). Everything is written to the hash-chained
audit log, and every filesystem effect is confined by the sandbox guard.
"""

from .executor import Executor, RunResult, StepOutcome
from .sandbox import Sandbox, SandboxEscape

__all__ = [
    "Executor",
    "RunResult",
    "Sandbox",
    "SandboxEscape",
    "StepOutcome",
]
