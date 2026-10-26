"""Sandbox directory guard.

Every filesystem effect of the executor goes through this guard: a path
is resolved against the sandbox root and refused if it escapes — by
``..`` segments, by being absolute outside the root, or by any other
means. This is the enforcement counterpart of the classifier's ruling:
internal steps may only ever touch the governed workspace.
"""

from __future__ import annotations

from pathlib import Path


class SandboxEscape(Exception):
    """Raised when a path would resolve outside the sandbox root."""


class Sandbox:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative: str | Path) -> Path:
        """Resolve ``relative`` inside the sandbox or raise SandboxEscape."""
        candidate = Path(relative)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise SandboxEscape(
                f"path {relative!r} escapes sandbox {self.root}"
            ) from None
        return resolved

    def read_text(self, relative: str | Path) -> str:
        return self.resolve(relative).read_text(encoding="utf-8")

    def write_text(self, relative: str | Path, content: str) -> Path:
        path = self.resolve(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def write_bytes(self, relative: str | Path, data: bytes) -> Path:
        path = self.resolve(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def exists(self, relative: str | Path) -> bool:
        try:
            return self.resolve(relative).exists()
        except SandboxEscape:
            return False

    def list_files(self) -> list[str]:
        return sorted(
            str(p.relative_to(self.root))
            for p in self.root.rglob("*")
            if p.is_file()
        )
