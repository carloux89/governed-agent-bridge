"""Step classifier: plan step -> governance verdict.

Consumes the class table in :mod:`classifier.rules` and resolves dynamic
effects against the governed sandbox directory. The verdict is the
authoritative governance label for a step:

- ``INTERNAL``  — every effect stays inside the sandbox: the step is
  auto-approved (by policy) and fully audit-logged.
- ``SENSITIVE`` — at least one effect leaves the sandbox, or the action is
  destructive/external by kind, or the action is unknown (fail-closed):
  the step needs an explicit human approval before it may execute.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .rules import ACTION_RULES, ALWAYS_SENSITIVE_KINDS, UNKNOWN_ACTION_CLASS

# Parameter names that carry filesystem paths in a step's params.
PATH_PARAM_KEYS = ("document", "output", "path", "target", "file",
                   "requirements", "inventory")


class Verdict(str, Enum):
    INTERNAL = "internal"
    SENSITIVE = "sensitive"


@dataclass
class ResolvedEffect:
    kind: str
    scope: str  # "sandbox" | "external" (after dynamic resolution)
    detail: str = ""


@dataclass
class Classification:
    action: str
    verdict: Verdict
    action_class: str  # approvals-engine class for the proposal
    effects: list[ResolvedEffect] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    known_action: bool = True

    @property
    def requires_approval(self) -> bool:
        return self.verdict is Verdict.SENSITIVE

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "verdict": self.verdict.value,
            "action_class": self.action_class,
            "known_action": self.known_action,
            "effects": [
                {"kind": e.kind, "scope": e.scope, "detail": e.detail}
                for e in self.effects
            ],
            "reasons": list(self.reasons),
        }


def _escapes_sandbox(value: str, sandbox_root: Path) -> bool:
    """True if a path-like parameter value resolves outside the sandbox."""
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = sandbox_root / candidate
    try:
        candidate.resolve().relative_to(sandbox_root.resolve())
    except ValueError:
        return True
    return False


def classify_step(step: dict, sandbox_root: str | Path | None = None) -> Classification:
    """Classify one plan step (dict with ``action`` and ``params``)."""
    action = step.get("action", "")
    params = step.get("params") or {}
    root = Path(sandbox_root) if sandbox_root is not None else None

    rule = ACTION_RULES.get(action)
    if rule is None:
        return Classification(
            action=action,
            verdict=Verdict.SENSITIVE,
            action_class=step.get("risk_class") or UNKNOWN_ACTION_CLASS,
            effects=[],
            reasons=[
                f"unknown action {action!r}: fail-closed, treated as sensitive"
            ],
            known_action=False,
        )

    effects: list[ResolvedEffect] = []
    reasons: list[str] = []
    sensitive = False

    for effect in rule["effects"]:
        kind = effect["kind"]
        scope = effect["scope"]
        detail = ""
        if scope in ("sandbox", "dynamic"):
            target_param = effect.get("target_param")
            value = params.get(target_param) if target_param else None
            if root is not None and isinstance(value, str) and value:
                if _escapes_sandbox(value, root):
                    scope = "external"
                    detail = f"{target_param}={value!r} escapes the sandbox"
                else:
                    scope = "sandbox"
                    detail = f"{target_param}={value!r} inside the sandbox"
            else:
                scope = "sandbox"
        if scope == "external":
            sensitive = True
            reasons.append(
                f"effect {kind!r} leaves the sandbox"
                + (f" ({detail})" if detail else "")
            )
        if kind in ALWAYS_SENSITIVE_KINDS:
            sensitive = True
            reasons.append(f"effect kind {kind!r} always requires approval")
        effects.append(ResolvedEffect(kind=kind, scope=scope, detail=detail))

    # Defence in depth: ANY path parameter escaping the sandbox makes the
    # step sensitive, even if its declared effects look internal.
    if root is not None:
        for key in PATH_PARAM_KEYS:
            value = params.get(key)
            if isinstance(value, str) and value and _escapes_sandbox(value, root):
                if not sensitive:
                    reasons.append(
                        f"parameter {key}={value!r} escapes the sandbox"
                    )
                sensitive = True
        includes = params.get("includes")
        if isinstance(includes, list):
            for item in includes:
                if isinstance(item, str) and item and _escapes_sandbox(item, root):
                    sensitive = True
                    reasons.append(
                        f"includes entry {item!r} escapes the sandbox"
                    )

    if not sensitive:
        reasons.append("all effects stay inside the sandbox: auto-approved, audit-logged")

    return Classification(
        action=action,
        verdict=Verdict.SENSITIVE if sensitive else Verdict.INTERNAL,
        action_class=rule["action_class"],
        effects=effects,
        reasons=reasons,
        known_action=True,
    )


def classify_plan(plan: dict, sandbox_root: str | Path | None = None) -> list[Classification]:
    """Classify every step of a plan, in order."""
    return [classify_step(step, sandbox_root) for step in plan.get("steps", [])]
