"""Offline tests for the arm forward-kinematics utility."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from bookarm.domain.models.pose import BasePose


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "arm" / "arm_forward_kinematics.py"
_SPEC = importlib.util.spec_from_file_location("bookarm_arm_forward_kinematics_script", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
arm_forward_kinematics = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(arm_forward_kinematics)


class FakeKinematics:
    created_with: tuple[Path, tuple[float, ...] | None] | None = None
    received_joints: tuple[float, ...] | None = None

    def __init__(self, urdf_path: Path, tcp_offset=None) -> None:
        FakeKinematics.created_with = (urdf_path, tcp_offset)

    def forward_kinematics(self, joints):
        FakeKinematics.received_joints = joints.values_rad
        return BasePose(0.1, -0.2, 0.3, 0.0, 0.5, -1.0)


class ArmForwardKinematicsScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeKinematics.created_with = None
        FakeKinematics.received_joints = None

    def test_calculates_pose_without_connecting_arm(self) -> None:
        output = io.StringIO()
        with (
            patch.object(arm_forward_kinematics, "PinocchioKinematics", FakeKinematics),
            contextlib.redirect_stdout(output),
        ):
            code = arm_forward_kinematics.main([
                "--joints", "0", "0.1", "-0.2", "0.3", "-0.4", "0.5",
                "--tcp-offset", "0", "0", "0.1", "0", "0", "0",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(FakeKinematics.received_joints, (0.0, 0.1, -0.2, 0.3, -0.4, 0.5))
        self.assertEqual(FakeKinematics.created_with[1], (0.0, 0.0, 0.1, 0.0, 0.0, 0.0))
        result = json.loads(output.getvalue())
        self.assertEqual(result["end_effector_base_pose_m_rad"], [0.1, -0.2, 0.3, 0.0, 0.5, -1.0])
        self.assertAlmostEqual(result["orientation_rpy_deg"]["pitch"], 28.6478897565)

    def test_returns_failure_when_kinematics_is_unavailable(self) -> None:
        output = io.StringIO()
        with (
            patch.object(arm_forward_kinematics, "PinocchioKinematics", side_effect=RuntimeError("missing pinocchio")),
            contextlib.redirect_stderr(output),
        ):
            code = arm_forward_kinematics.main(["--joints", "0", "0", "0", "0", "0", "0"])

        self.assertEqual(code, 1)
        self.assertIn("missing pinocchio", output.getvalue())

    def test_uses_the_bookarm_default_tcp_when_none_is_given(self) -> None:
        with (
            patch.object(arm_forward_kinematics, "PinocchioKinematics", FakeKinematics),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = arm_forward_kinematics.main(["--joints", "0", "0", "0", "0", "0", "0"])

        self.assertEqual(code, 0)
        self.assertEqual(FakeKinematics.created_with[1], (0.0, 0.01, -0.14, 0.0, 0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
