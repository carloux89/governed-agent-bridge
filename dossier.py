"""Locating the synthetic SYN-2026-001 dossier.

The dossier's canonical copy ships inside this repository, under
``runs/SYN-2026-001/sandbox/`` (the seeded demo sandbox — the four source
documents there are the demo's inputs). Resolution order:

1. the ``GAB_DOSSIER_DIR`` environment variable, when set (an explicit
   override always wins, even if the path does not exist yet);
2. the in-repository copy, when present — this is what makes a
   standalone clone of this repository self-contained;
3. the legacy goal-workspace layout (``<repo>/../../files/
   dossier-synthetique-SYN-2026-001``), kept as a fallback for the
   original development workspace this project was built in.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_VAR = "GAB_DOSSIER_DIR"
DOSSIER_NAME = "dossier-synthetique-SYN-2026-001"
REPO_ROOT = Path(__file__).resolve().parent


def resolve_dossier_dir() -> Path:
    """Return the directory holding the SYN-2026-001 source documents."""
    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override).expanduser()
    in_repo = REPO_ROOT / "runs" / "SYN-2026-001" / "sandbox"
    if in_repo.is_dir():
        return in_repo
    return REPO_ROOT.parent.parent / "files" / DOSSIER_NAME
