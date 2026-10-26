"""Governance class table (formalized policy, as data).

Ruling implemented here (build plan §1.2, tightened for the demo):

- An action whose effects all stay INSIDE the governed sandbox workspace
  is an **internal** action: auto-approved, but fully audit-logged.
- An action with ANY effect OUTSIDE the sandbox — sending a message,
  executing an arbitrary command, deleting, printing, writing outside the
  workspace, or producing a deliverable addressed to a recipient outside
  the sandbox — is a **sensitive** action: it requires explicit human
  approval before it executes.
- An unknown action is sensitive (fail-closed / default-deny).

Each known action declares its effects. An effect has a ``kind`` and a
``scope``:

- ``sandbox``  — the effect lands inside the sandbox. When the effect
  carries a ``target_param``, the classifier also resolves that parameter
  dynamically: a path that escapes the sandbox turns the effect external.
- ``external`` — the effect intrinsically leaves the sandbox.

The verdict for a step is ``internal`` only if every effect resolves to
scope ``sandbox``; otherwise it is ``sensitive``.

``action_class`` is the approvals-engine class used when a proposal is
created for the step (READ is the only class the engine auto-approves, so
internal steps map to READ by design; the classifier's own verdict is the
authoritative governance label and is what gets audit-logged).
"""

from __future__ import annotations

# kind: read | write | execute | send | delete | print | inspect
# scope: sandbox | external | dynamic (resolved from target_param)

ACTION_RULES: dict[str, dict] = {
    "read_document": {
        "action_class": "read",
        "effects": [
            {"kind": "read", "scope": "sandbox", "target_param": "document"},
        ],
        "rationale": "Reads a document inside the sandbox; nothing leaves the workspace.",
    },
    "inspect_workspace": {
        "action_class": "read",
        "effects": [
            {"kind": "inspect", "scope": "sandbox"},
        ],
        "rationale": "Lists the sandbox contents; read-only.",
    },
    "extract_deadlines": {
        "action_class": "read",
        "effects": [
            {"kind": "write", "scope": "sandbox", "target_param": "output"},
        ],
        "rationale": "Generates echeances.md, a working file inside the sandbox.",
    },
    "build_checklist": {
        "action_class": "read",
        "effects": [
            {"kind": "read", "scope": "sandbox", "target_param": "requirements"},
            {"kind": "read", "scope": "sandbox", "target_param": "inventory"},
            {"kind": "write", "scope": "sandbox"},
        ],
        "rationale": "Assembles the document checklist as a working file inside the sandbox.",
    },
    "draft_case_summary_letter": {
        "action_class": "write",
        "effects": [
            {"kind": "write", "scope": "sandbox", "target_param": "output"},
            {"kind": "write", "scope": "external"},
        ],
        "rationale": (
            "The summary letter is an outgoing deliverable: it is addressed "
            "to the housing office and exists to leave the sandbox as part "
            "of the case package. Producing it is an external-facing write, "
            "so a human approves it first (demo pause no. 1)."
        ),
    },
    "assemble_export_package": {
        "action_class": "write",
        "effects": [
            {"kind": "write", "scope": "external", "target_param": "output"},
        ],
        "rationale": (
            "The export/print package leaves the sandbox (export per "
            "decision D4, print per plan section 2 step 6): sensitive step "
            "no. 2, human approval required."
        ),
    },
    "write_file": {
        "action_class": "write",
        "effects": [
            {"kind": "write", "scope": "dynamic", "target_param": "path"},
        ],
        "rationale": "Generic write: internal only if the target path stays inside the sandbox.",
    },
    "send_message": {
        "action_class": "send",
        "effects": [{"kind": "send", "scope": "external"}],
        "rationale": "Sending reaches a recipient outside the sandbox.",
    },
    "send_email": {
        "action_class": "send",
        "effects": [{"kind": "send", "scope": "external"}],
        "rationale": "Email leaves the machine entirely.",
    },
    "run_command": {
        "action_class": "execute",
        "effects": [{"kind": "execute", "scope": "external"}],
        "rationale": "Executing an arbitrary command affects the whole machine, not just the sandbox.",
    },
    "delete_file": {
        "action_class": "delete",
        "effects": [{"kind": "delete", "scope": "dynamic", "target_param": "path"}],
        "rationale": "Deletion is irreversible; sensitive even inside the sandbox unless policy says otherwise — it never does here.",
    },
    "print_document": {
        "action_class": "print",
        "effects": [{"kind": "print", "scope": "external"}],
        "rationale": "Printing produces a physical artifact outside the sandbox.",
    },
}

# Deletion is destructive wherever it lands: a dynamic delete that resolves
# inside the sandbox is STILL sensitive. The classifier enforces this via
# ALWAYS_SENSITIVE_KINDS.
ALWAYS_SENSITIVE_KINDS = frozenset({"execute", "send", "delete", "print"})

# Fallback governance class for an action missing from the table.
UNKNOWN_ACTION_CLASS = "execute"
