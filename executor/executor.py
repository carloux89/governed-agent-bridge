"""Governed executor: runs a plan step by step under the governance rules.

Flow per step (build plan §1.2–§1.3):

1. The step is classified (classifier) and the verdict is audit-logged.
2. A proposal is submitted to the approvals queue and audit-logged.
   - INTERNAL steps are auto-approved by policy and execute at once.
   - SENSITIVE steps PAUSE the run: the executor waits until the queue
     reports a decision (approve / deny) or the proposal expires
     (default-deny). A ``pause_hook`` is invoked once at the pause so a
     dashboard, a test, or a human driver can act.
3. Approved steps execute through the sandbox guard; denied or expired
   steps are skipped. Every outcome is audit-logged into the hash chain.

No step executes without either a policy auto-approval or a traced human
"yes", and no handler can touch a path outside the sandbox.

Standard library only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from approvals.engine import ApprovalQueue, Status
from auditlog.chain import AuditLog
from classifier.classifier import Verdict, classify_step

from .actions import ACTION_HANDLERS
from .sandbox import Sandbox, SandboxEscape

EXECUTOR_ACTOR = "executor"
GOVERNANCE_ACTOR = "governance"
PATH_PARAM_KEYS = ("document", "output", "path", "target", "file",
                   "requirements", "inventory")
DEFAULT_TTL_SECONDS = 300.0


@dataclass
class StepOutcome:
    step: int
    action: str
    verdict: str
    outcome: str  # executed | skipped_denied | skipped_expired | blocked_escape | failed
    proposal_id: str | None = None
    result: dict | None = None
    detail: str = ""


@dataclass
class RunResult:
    plan_task: str
    outcomes: list[StepOutcome] = field(default_factory=list)

    def counts(self) -> dict:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.outcome] = counts.get(outcome.outcome, 0) + 1
        return counts


class Executor:
    def __init__(
        self,
        sandbox_root: str | Path,
        audit_log: AuditLog,
        queue: ApprovalQueue | None = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        pause_hook: Callable | None = None,
        poll_interval: float = 0.02,
    ):
        self.sandbox = Sandbox(sandbox_root)
        self.audit = audit_log
        self.queue = queue if queue is not None else ApprovalQueue()
        self.ttl_seconds = ttl_seconds
        self.pause_hook = pause_hook
        self.poll_interval = poll_interval

    # -- audit helpers ---------------------------------------------------
    def _log(self, actor: str, action: str, params: dict,
             decision: dict | None = None, result: str | None = None) -> dict:
        return self.audit.append(
            actor=actor, action=action, params=params,
            decision=decision, result=result,
        )

    # -- path pre-check (defence in depth over the handlers' own guard) --
    def _check_paths(self, step: dict) -> str | None:
        """Return an escape description if any path param leaves the sandbox."""
        params = step.get("params") or {}
        candidates: list[tuple[str, str]] = []
        for key in PATH_PARAM_KEYS:
            value = params.get(key)
            if isinstance(value, str) and value:
                candidates.append((key, value))
        includes = params.get("includes")
        if isinstance(includes, list):
            for item in includes:
                if isinstance(item, str) and item:
                    candidates.append(("includes", item))
        for key, value in candidates:
            try:
                self.sandbox.resolve(value)
            except SandboxEscape as exc:
                return f"{key}={value!r}: {exc}"
        return None

    # -- decision wait ---------------------------------------------------
    def _wait_for_decision(self, proposal) -> Status:
        if self.pause_hook is not None:
            self.pause_hook(proposal)
        while True:
            status = self.queue.effective_status(proposal.proposal_id)
            if status is not Status.PENDING:
                return status
            time.sleep(self.poll_interval)

    # -- main loop -------------------------------------------------------
    def run(self, plan: dict) -> RunResult:
        result = RunResult(plan_task=plan.get("task", ""))
        self._log(
            EXECUTOR_ACTOR, "run_started",
            {"task": result.plan_task, "planner": plan.get("planner"),
             "dossier_id": plan.get("dossier_id"),
             "steps": len(plan.get("steps", []))},
        )
        for step in plan.get("steps", []):
            result.outcomes.append(self._run_step(step))
        self._log(
            EXECUTOR_ACTOR, "run_finished",
            {"task": result.plan_task, "counts": result.counts()},
            result="completed",
        )
        return result

    def _run_step(self, step: dict) -> StepOutcome:
        step_no = step.get("step", 0)
        action = step.get("action", "")
        params = step.get("params") or {}
        base_params = {"step": step_no, "action": action, "params": params}

        classification = classify_step(step, self.sandbox.root)
        self._log(
            GOVERNANCE_ACTOR, "step_classified",
            {**base_params, "classification": classification.to_dict()},
            result=classification.verdict.value,
        )

        escape = self._check_paths(step)
        if escape is not None:
            self._log(
                GOVERNANCE_ACTOR, "step_blocked",
                {**base_params, "blocked_reason": escape},
                result="sandbox_escape_refused",
            )
            return StepOutcome(
                step=step_no, action=action,
                verdict=classification.verdict.value,
                outcome="blocked_escape", detail=escape,
            )

        proposal = self.queue.submit(
            action=action,
            action_class=classification.action_class,
            params=params,
            justification=step.get("justification", ""),
            ttl_seconds=self.ttl_seconds,
        )
        self._log(
            GOVERNANCE_ACTOR, "proposal_submitted",
            {**base_params, "proposal_id": proposal.proposal_id,
             "action_class": classification.action_class,
             "status": proposal.status.value},
            decision={"by": proposal.decided_by, "status": proposal.status.value}
            if proposal.status is not Status.PENDING else None,
            result=proposal.status.value,
        )

        if classification.verdict is Verdict.SENSITIVE:
            status = self._wait_for_decision(proposal)
            decided = self.queue.get(proposal.proposal_id)
            self._log(
                GOVERNANCE_ACTOR, "proposal_decided",
                {**base_params, "proposal_id": proposal.proposal_id,
                 "status": status.value},
                decision={"by": decided.decided_by or "policy:default-deny",
                          "status": status.value},
                result=status.value,
            )
            if status is Status.DENIED:
                self._log(EXECUTOR_ACTOR, "step_skipped",
                          {**base_params, "proposal_id": proposal.proposal_id},
                          result="denied_by_human")
                return StepOutcome(step_no, action, "sensitive",
                                   "skipped_denied", proposal.proposal_id)
            if status is not Status.APPROVED:
                self._log(EXECUTOR_ACTOR, "step_skipped",
                          {**base_params, "proposal_id": proposal.proposal_id},
                          result="expired_default_deny")
                return StepOutcome(step_no, action, "sensitive",
                                   "skipped_expired", proposal.proposal_id)

        return self._execute(step, proposal.proposal_id,
                             classification.verdict.value)

    def _execute(self, step: dict, proposal_id: str, verdict: str) -> StepOutcome:
        step_no = step.get("step", 0)
        action = step.get("action", "")
        params = step.get("params") or {}
        handler = ACTION_HANDLERS.get(action)
        if handler is None:
            self._log(
                EXECUTOR_ACTOR, "step_failed",
                {"step": step_no, "action": action, "params": params,
                 "proposal_id": proposal_id, "error": "no handler registered"},
                result="failed",
            )
            return StepOutcome(step_no, action, verdict, "failed",
                               proposal_id, detail="no handler registered")
        try:
            outcome = handler(params, self.sandbox)
        except SandboxEscape as exc:
            self._log(
                GOVERNANCE_ACTOR, "step_blocked",
                {"step": step_no, "action": action, "params": params,
                 "proposal_id": proposal_id, "blocked_reason": str(exc)},
                result="sandbox_escape_refused",
            )
            return StepOutcome(step_no, action, verdict, "blocked_escape",
                               proposal_id, detail=str(exc))
        except Exception as exc:  # fail-closed: log and mark, never hide
            self._log(
                EXECUTOR_ACTOR, "step_failed",
                {"step": step_no, "action": action, "params": params,
                 "proposal_id": proposal_id, "error": f"{type(exc).__name__}: {exc}"},
                result="failed",
            )
            return StepOutcome(step_no, action, verdict, "failed",
                               proposal_id, detail=str(exc))
        self._log(
            EXECUTOR_ACTOR, "step_executed",
            {"step": step_no, "action": action, "params": params,
             "proposal_id": proposal_id, "output": outcome},
            result="ok",
        )
        return StepOutcome(step_no, action, verdict, "executed",
                           proposal_id, result=outcome)
