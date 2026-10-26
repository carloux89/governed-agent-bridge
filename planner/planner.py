"""Planner interface and deterministic mock planner (build plan §1.4, §2).

A planner turns a natural-language task plus dossier context into a JSON
plan: an ordered list of proposed steps, each with an action, a governance
risk class (consumed by the approvals engine) and a justification (shown
to the human in the approval dashboard, plan §1.2).

``MockPlanner`` is deterministic: same input -> identical plan, no network,
no randomness. It knows the synthetic demo scenario SYN-2026-001 (plan §2)
and returns a conservative single-step plan for anything else.

``SparkPlanner`` is the live planner: it asks Muse Spark (Meta Model API)
for a plan through the PC bridge — the API credential never leaves the PC
(see :mod:`planner.transport`). The reply is parsed defensively and every
step is validated against the known action vocabulary; on ANY failure
(transport, HTTP, malformed JSON, schema violation, unknown action) it
falls back to ``MockPlanner`` and records the fallback, with its reason,
in the audit log event stream. A run therefore always shows honestly
which planner produced its plan, and why.

Planner selection is by environment: ``PLANNER=spark|mock`` (default
``mock``) via :func:`planner_from_env`. Live mode stays off until the
hackathon API credits are confirmed applied — before that, a generation
call would bill the account's card.

Standard library only.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod

from classifier.rules import ACTION_RULES
from executor.actions import ACTION_HANDLERS

from .transport import BridgeSparkTransport, SparkTransportError

DEMO_DOSSIER_ID = "SYN-2026-001"
MODEL_ID = "muse-spark-1.3"

# Actions a plan may propose: executable (a handler exists) AND classified
# (a governance rule exists). Anything else is rejected, fail-closed.
KNOWN_ACTIONS = frozenset(
    action for action in ACTION_HANDLERS if action in ACTION_RULES
)
RISK_CLASSES = frozenset(
    {"read", "write", "execute", "send", "delete", "print"}
)
MAX_PLAN_STEPS = 12
_MAX_REASON_CHARS = 300


class Planner(ABC):
    """Interface every planner (mock or live) must implement."""

    name: str = "planner"

    @abstractmethod
    def plan(self, task: str, context: dict | None = None) -> dict:
        """Return a JSON-serializable plan for ``task``.

        Plan shape::

            {"task": str, "planner": str, "dossier_id": str | None,
             "steps": [{"step": int, "action": str, "risk_class": str,
                        "params": dict, "justification": str}, ...]}
        """
        raise NotImplementedError


def _demo_steps() -> list[dict]:
    """The 6-step demo scenario of plan §2, on 100% synthetic data only."""
    return [
        {
            "step": 1,
            "action": "read_document",
            "risk_class": "read",
            "params": {"document": "01-courriel-attribution.md",
                       "dossier_id": DEMO_DOSSIER_ID},
            "justification": "Read the fictional allocation email to extract its deadlines.",
        },
        {
            "step": 2,
            "action": "read_document",
            "risk_class": "read",
            "params": {"document": "02-offre-bail-fictive.md",
                       "dossier_id": DEMO_DOSSIER_ID},
            "justification": "Read the fictional lease offer for dates and amounts.",
        },
        {
            "step": 3,
            "action": "extract_deadlines",
            "risk_class": "read",
            "params": {"output": "echeances.md", "dossier_id": DEMO_DOSSIER_ID},
            "justification": "Collect deadlines from the dossier into a working summary (auto step, plan §2 step 3).",
        },
        {
            "step": 4,
            "action": "build_checklist",
            "risk_class": "read",
            "params": {"requirements": "03-liste-exigences.md",
                       "inventory": "04-inventaire-pieces.md",
                       "dossier_id": DEMO_DOSSIER_ID},
            "justification": "Check required documents against the inventory and flag the missing income proof (auto step, plan §2 step 4).",
        },
        {
            "step": 5,
            "action": "draft_case_summary_letter",
            "risk_class": "write",
            "params": {"output": "lettre-resume-cas.md",
                       "dossier_id": DEMO_DOSSIER_ID},
            "justification": "The summary letter contains personal (fictional) case data: sensitive step no. 1, human approval required (plan §2 step 5).",
        },
        {
            "step": 6,
            "action": "assemble_export_package",
            "risk_class": "write",
            "params": {"format": "pdf", "output": "paquet-SYN-2026-001.pdf",
                       "includes": ["01-courriel-attribution.md",
                                    "02-offre-bail-fictive.md",
                                    "03-liste-exigences.md",
                                    "lettre-resume-cas.md"],
                       "dossier_id": DEMO_DOSSIER_ID},
            "justification": "Assembling the export/print package is sensitive step no. 2, human approval required (plan §2 step 6; PDF export per decision D4).",
        },
    ]


class MockPlanner(Planner):
    """Deterministic offline planner for development and rehearsals."""

    name = "mock"

    def plan(self, task: str, context: dict | None = None) -> dict:
        context = context or {}
        dossier_id = context.get("dossier_id")
        normalized = task.lower()
        if dossier_id == DEMO_DOSSIER_ID or DEMO_DOSSIER_ID.lower() in normalized \
                or "prépare le dossier" in normalized or "prepare the dossier" in normalized:
            return {
                "task": task,
                "planner": self.name,
                "dossier_id": DEMO_DOSSIER_ID,
                "steps": _demo_steps(),
            }
        # Conservative fallback: the mock knows no other scenario, so it
        # proposes a single read-only inspection step and nothing else.
        return {
            "task": task,
            "planner": self.name,
            "dossier_id": dossier_id,
            "steps": [
                {
                    "step": 1,
                    "action": "inspect_workspace",
                    "risk_class": "read",
                    "params": {},
                    "justification": "Mock planner fallback: read-only inspection only; no sensitive action proposed.",
                }
            ],
        }


# ---------------------------------------------------------------------------
# Live Spark planner (plan §1.4)
# ---------------------------------------------------------------------------

# Per-action guidance embedded in the prompt: the params shape the model
# must emit and the risk class governance expects for that action.
_ACTION_GUIDE: dict[str, tuple[str, str]] = {
    "read_document": (
        '{"document": <one of the dossier files>, "dossier_id": <dossier id>}',
        "read",
    ),
    "inspect_workspace": ("{}", "read"),
    "extract_deadlines": (
        '{"output": "echeances.md", "dossier_id": <dossier id>}', "read",
    ),
    "build_checklist": (
        '{"requirements": "03-liste-exigences.md", '
        '"inventory": "04-inventaire-pieces.md", "dossier_id": <dossier id>}',
        "read",
    ),
    "draft_case_summary_letter": (
        '{"output": "lettre-resume-cas.md", "dossier_id": <dossier id>}',
        "write",
    ),
    "assemble_export_package": (
        '{"format": "pdf", "output": "paquet-SYN-2026-001.pdf", '
        '"includes": [<dossier file names>], "dossier_id": <dossier id>}',
        "write",
    ),
}

_DEFAULT_DOSSIER_FILES = [
    "01-courriel-attribution.md",
    "02-offre-bail-fictive.md",
    "03-liste-exigences.md",
    "04-inventaire-pieces.md",
]


def build_messages(task: str, context: dict | None = None) -> list[dict]:
    """Chat messages that demand a STRICT JSON plan from Muse Spark."""
    context = context or {}
    documents = context.get("documents") or _DEFAULT_DOSSIER_FILES
    action_lines = "\n".join(
        f'- "{action}": params {params_hint}; risk_class "{risk}"'
        for action, (params_hint, risk) in _ACTION_GUIDE.items()
        if action in KNOWN_ACTIONS
    )
    system = (
        "You are the planning component of Governed Agent Bridge, a "
        "governed agent that works on a user's PC under human approval. "
        "Turn the task into an ordered plan of steps.\n\n"
        "Reply with STRICT JSON ONLY: no prose, no explanation, no "
        "markdown code fences. The JSON object must have exactly this "
        "shape:\n"
        '{"steps": [{"step": 1, "action": "<action>", "risk_class": '
        '"<class>", "params": {...}, "justification": "<one sentence>"}]}\n\n'
        "Allowed actions — use ONLY these, with the params shape and "
        f"risk_class shown:\n{action_lines}\n\n"
        "Rules:\n"
        "- Steps are numbered 1, 2, 3, ... in execution order.\n"
        "- Never invent an action name or a file name. The dossier "
        f"documents are: {', '.join(documents)}.\n"
        "- justification is one sentence, shown to the human who must "
        "approve sensitive steps; say what the step does and why.\n"
        "- Read-only preparation steps come first; the summary letter "
        "and the export package (the two write steps) come last."
    )
    user = (
        f"Task: {task}\n"
        f"Dossier context (JSON): {json.dumps(context, ensure_ascii=False)}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_payload(task: str, context: dict | None = None,
                  model: str = MODEL_ID) -> dict:
    """The chat-completions payload sent (via the bridge) to the API."""
    return {
        "model": model,
        "messages": build_messages(task, context),
        "temperature": 0,
    }


class SparkPlanError(ValueError):
    """The model's reply is not a valid plan; the message is the reason."""


def _extract_json_object(text: str) -> dict:
    """Pull the JSON object out of the model's reply, or raise."""
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        lines = lines[1:]  # opening fence (maybe ```json)
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end <= start:
        raise SparkPlanError("no JSON object found in the model reply")
    try:
        data = json.loads(candidate[start:end + 1])
    except json.JSONDecodeError as exc:
        raise SparkPlanError(
            f"invalid JSON in the model reply: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})"
        ) from exc
    if not isinstance(data, dict):
        raise SparkPlanError("the model reply is not a JSON object")
    return data


def _validate_plan(text: str, task: str, context: dict) -> dict:
    """Validate the model's reply against the plan schema, fail-closed."""
    data = _extract_json_object(text)
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise SparkPlanError("the plan has no non-empty 'steps' list")
    if len(raw_steps) > MAX_PLAN_STEPS:
        raise SparkPlanError(
            f"the plan has {len(raw_steps)} steps (max {MAX_PLAN_STEPS})"
        )
    steps = []
    for position, raw in enumerate(raw_steps, start=1):
        if not isinstance(raw, dict):
            raise SparkPlanError(f"step at position {position} is not an object")
        if raw.get("step") != position:
            raise SparkPlanError(
                f"step numbering invalid at position {position}: "
                f"got {raw.get('step')!r}"
            )
        action = raw.get("action")
        if action not in KNOWN_ACTIONS:
            raise SparkPlanError(
                f"unknown action {action!r} at step {position}"
            )
        risk_class = raw.get("risk_class")
        if risk_class not in RISK_CLASSES:
            raise SparkPlanError(
                f"invalid risk_class {risk_class!r} at step {position}"
            )
        params = raw.get("params", {})
        if not isinstance(params, dict):
            raise SparkPlanError(f"params at step {position} is not an object")
        justification = raw.get("justification")
        if not isinstance(justification, str) or not justification.strip():
            raise SparkPlanError(f"missing justification at step {position}")
        steps.append({
            "step": position,
            "action": action,
            "risk_class": risk_class,
            "params": params,
            "justification": justification,
        })
    dossier_id = context.get("dossier_id") or data.get("dossier_id")
    return {
        "task": task,
        "planner": "spark",
        "dossier_id": dossier_id,
        "steps": steps,
    }


def _reason_of(exc: Exception) -> str:
    """A single-line, log-safe reason string for a planning failure."""
    if isinstance(exc, SparkPlanError):
        text = str(exc)
    else:
        text = f"{type(exc).__name__}: {exc}"
    return " ".join(text.split())[:_MAX_REASON_CHARS]


def _unconfigured_transport(error: SparkTransportError):
    """A transport stand-in that re-raises a stored configuration error."""
    def call(payload):
        raise error
    return call


class SparkPlanner(Planner):
    """Live planner backed by Muse Spark via the Meta Model API.

    The API call itself happens on the user's PC through the bridge
    transport (the credential never leaves the PC). ``transport`` is any
    callable ``payload -> reply text``; tests inject a fake, production
    uses :class:`~planner.transport.BridgeSparkTransport`.

    On ANY failure the planner falls back to ``fallback``
    (:class:`MockPlanner` by default) and records the fallback — with
    its reason — in the audit log event stream (when ``audit_log`` is
    given) and on the returned plan itself (``planner_requested`` /
    ``planner_fallback_reason``), so a run never hides which planner
    actually produced its plan, or why.
    """

    name = "spark"

    def __init__(self, transport=None, fallback: Planner | None = None,
                 audit_log=None, model: str = MODEL_ID):
        if transport is not None:
            self.transport = transport
        else:
            try:
                self.transport = BridgeSparkTransport()
            except SparkTransportError as exc:
                # Bridge not configured (no GAB_BRIDGE_HOST /
                # GAB_BRIDGE_PY and no explicit transport): defer the
                # error to call time so plan() records it as the
                # fallback reason — planner selection itself must not
                # crash, and the mock plan is still produced.
                self.transport = _unconfigured_transport(exc)
        self.fallback = fallback if fallback is not None else MockPlanner()
        self.audit_log = audit_log
        self.model = model
        self.events: list[dict] = []
        self.last_fallback_reason: str | None = None

    def _record(self, action: str, params: dict, result: str) -> None:
        self.events.append({"action": action, "params": params,
                            "result": result})
        if self.audit_log is not None:
            self.audit_log.append(
                actor="planner", action=action, params=params, result=result
            )

    def plan(self, task: str, context: dict | None = None) -> dict:
        context = context or {}
        try:
            reply = self.transport(build_payload(task, context, self.model))
            plan = _validate_plan(reply, task, context)
        except Exception as exc:  # fail-closed: any failure -> fallback
            return self._fallback(task, context, _reason_of(exc))
        plan["planner_requested"] = self.name
        plan["model"] = self.model
        self._record(
            "plan_produced",
            {"planner": self.name, "model": self.model,
             "task": task, "dossier_id": plan.get("dossier_id"),
             "steps": len(plan["steps"])},
            result="ok",
        )
        return plan

    def _fallback(self, task: str, context: dict, reason: str) -> dict:
        self.last_fallback_reason = reason
        self._record(
            "planner_fallback",
            {"requested_planner": self.name, "model": self.model,
             "reason": reason, "fell_back_to": self.fallback.name},
            result="fallback_to_mock",
        )
        plan = self.fallback.plan(task, context)
        plan["planner_requested"] = self.name
        plan["planner_fallback_reason"] = reason
        return plan


def planner_from_env(audit_log=None, environ: dict | None = None) -> Planner:
    """Select the planner from the ``PLANNER`` variable (default mock).

    ``PLANNER=spark`` enables the live Spark planner — deliberately, and
    only once the hackathon API credits are confirmed applied (before
    that, a generation call would bill the account's card). Any other
    value, including an unset variable, yields the deterministic mock.
    """
    env = os.environ if environ is None else environ
    choice = (env.get("PLANNER") or "mock").strip().lower()
    if choice == "spark":
        return SparkPlanner(audit_log=audit_log)
    return MockPlanner()
