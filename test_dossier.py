"""Tests for the SYN-2026-001 dossier resolution (dossier.py).

A standalone clone of this repository must find the dossier inside the
repo itself; the original development workspace layout stays a fallback.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dossier
from dossier import resolve_dossier_dir


class TestResolveDossierDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gab-dossier-test-"))
        # A fake repository root in the legacy nesting (<tmp>/build/repo),
        # so REPO_ROOT.parent.parent is <tmp> and the legacy dossier sits
        # at <tmp>/files/<dossier>.
        self.repo_root = self.tmp / "build" / "repo"
        self.repo_root.mkdir(parents=True)
        self.legacy_dir = (
            self.tmp / "files" / "dossier-synthetique-SYN-2026-001"
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_env_var_wins(self):
        with mock.patch.object(dossier, "REPO_ROOT", self.repo_root), \
                mock.patch.dict(os.environ,
                                {"GAB_DOSSIER_DIR": "/explicit/dossier"}):
            self.assertEqual(resolve_dossier_dir(), Path("/explicit/dossier"))

    def test_in_repo_copy_preferred_over_legacy(self):
        in_repo = self.repo_root / "runs" / "SYN-2026-001" / "sandbox"
        in_repo.mkdir(parents=True)
        self.legacy_dir.mkdir(parents=True)
        with mock.patch.object(dossier, "REPO_ROOT", self.repo_root), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_dossier_dir(), in_repo)

    def test_legacy_layout_is_the_fallback(self):
        with mock.patch.object(dossier, "REPO_ROOT", self.repo_root), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_dossier_dir(), self.legacy_dir)

    def test_real_repo_resolves_to_an_existing_dossier(self):
        resolved = resolve_dossier_dir()
        self.assertTrue(resolved.is_dir())
        for name in ("01-courriel-attribution.md", "02-offre-bail-fictive.md",
                     "03-liste-exigences.md", "04-inventaire-pieces.md"):
            self.assertTrue((resolved / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
