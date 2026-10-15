"""Prevent hardware and GUI imports from leaking into core workflow layers."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1] / "src" / "bookarm"
FORBIDDEN_PREFIXES = ("MotorStudio", "webui", "el_a3_sdk", "PyQt")


class ArchitectureTests(unittest.TestCase):
    def test_domain_and_application_do_not_import_hardware_or_gui_modules(self):
        offenders = []
        for layer in (ROOT / "domain", ROOT / "application", ROOT / "runtime"):
            for path in layer.rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    imported = []
                    if isinstance(node, ast.Import):
                        imported = [name.name for name in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported = [node.module]
                    for name in imported:
                        if name.startswith(FORBIDDEN_PREFIXES):
                            offenders.append("%s imports %s" % (path, name))
        self.assertEqual(offenders, [])

    def test_bookarm_does_not_import_the_original_gui_or_webui(self):
        offenders = []
        for path in ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [item.name for item in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if name.startswith(("MotorStudio", "webui")):
                        offenders.append("%s imports %s" % (path, name))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
