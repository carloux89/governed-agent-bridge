"""End-to-end demo run of the SYN-2026-001 scenario (plan §2).

Runs the selected planner's plan through classifier + approvals +
executor. The planner is chosen by ``PLANNER=spark|mock`` (default
``mock`` — deterministic, offline; ``spark`` asks Muse Spark live
through the PC bridge and falls back to the mock, reason audit-logged,
on any failure). The two sensitive steps pause; here the "human"
decisions are simulated programmatically (approve both) — in the live
demo they are clicked in the dashboard instead.

Artifacts land in ``runs/SYN-2026-001/``: the sandbox (input dossier +
generated files) and the run's hash-chained audit log. Synthetic data
only. Usage::

    python3 demo_run.py
"""

from __future__ import annotations

import shutil
from pathlib import Path

from approvals.engine import ApprovalQueue
from auditlog.chain import AuditLog, verify_chain
from executor.executor import Executor
from planner.planner import planner_from_env

BUILD_ROOT = Path(__file__).resolve().parent
DOSSIER_DIR = BUILD_ROOT.parent.parent / "files" / "dossier-synthetique-SYN-2026-001"
RUN_DIR = BUILD_ROOT / "runs" / "SYN-2026-001"
DOSSIER_FILES = [
    "01-courriel-attribution.md",
    "02-offre-bail-fictive.md",
    "03-liste-exigences.md",
    "04-inventaire-pieces.md",
]
TASK = (
    "Prépare le dossier SYN-2026-001 : échéances, liste de documents, "
    "résumé du cas, paquet d'impression."
)


def main() -> None:
    if RUN_DIR.exists():
        shutil.rmtree(RUN_DIR)
    sandbox_root = RUN_DIR / "sandbox"
    sandbox_root.mkdir(parents=True)
    for name in DOSSIER_FILES:
        shutil.copyfile(DOSSIER_DIR / name, sandbox_root / name)

    audit_path = RUN_DIR / "audit-log.jsonl"
    audit_log = AuditLog(audit_path)
    queue = ApprovalQueue(journal_path=RUN_DIR / "approvals-journal.jsonl")

    def simulated_human(proposal):
        print(f"  ⏸ PAUSE — approval requested: {proposal.action} "
              f"({proposal.action_class.value})")
        print(f"      justification: {proposal.justification}")
        queue.approve(proposal.proposal_id, decided_by="simulated human (demo_run)")
        print("      → approved (simulated)")

    executor = Executor(sandbox_root, audit_log, queue=queue,
                        pause_hook=simulated_human)
    planner = planner_from_env(audit_log=audit_log)
    plan = planner.plan(TASK, {"dossier_id": "SYN-2026-001"})
    print(f"Running plan from planner={plan['planner']!r}: {TASK}")
    if plan.get("planner_fallback_reason"):
        print(f"  ⚠ planner fallback: {plan['planner_fallback_reason']}")
    result = executor.run(plan)
    for outcome in result.outcomes:
        print(f"  step {outcome.step}: {outcome.action} "
              f"[{outcome.verdict}] → {outcome.outcome}")
    verification = verify_chain(audit_path)
    print(f"Audit log: {audit_path}")
    print(f"Chain: {verification}")
    print(f"Counts: {result.counts()}")


if __name__ == "__main__":
    main()
