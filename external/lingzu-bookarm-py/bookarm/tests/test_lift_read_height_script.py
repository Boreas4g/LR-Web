"""Offline tests for the read-only lift-height utility."""

from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "lift" / "lift_read_height.py"
_SPEC = importlib.util.spec_from_file_location("bookarm_lift_read_height_script", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
lift_read_height = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lift_read_height)


class FakeLiftService:
    instances: list["FakeLiftService"] = []

    def __init__(self, config: object) -> None:
        self.config = config
        self.started = False
        self.stopped = False
        FakeLiftService.instances.append(self)

    def start(self) -> None:
        self.started = True

    def read_lift_height_mm(self) -> float:
        return 812.345

    def stop(self) -> None:
        self.stopped = True


class LiftReadHeightScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeLiftService.instances.clear()

    def test_reads_height_without_confirmation_or_motion(self) -> None:
        output = io.StringIO()
        with (
            patch.object(lift_read_height, "AimLiftService", FakeLiftService),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(lift_read_height.main(["--port", "/dev/test-lift"]), 0)

        lift = FakeLiftService.instances[0]
        self.assertTrue(lift.started)
        self.assertTrue(lift.stopped)
        self.assertEqual(lift.config.port, "/dev/test-lift")
        self.assertIn("812.345 mm", output.getvalue())
