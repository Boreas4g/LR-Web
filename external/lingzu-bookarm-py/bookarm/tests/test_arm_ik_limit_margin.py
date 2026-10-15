"""Regression tests for IK at the edge of the joint-limit workspace.

2026-08-27 midreturn incident: the lower/shift/forward push steps target
x=-0.448 m, z=0.184 m, rpy=(90, 0, 90) deg, whose only solution sits at
J4=-59.4 deg — inside the conservative 0.05 rad solve margin around the
-60 deg J4 lower limit. The default-margin solve must keep failing (it
protects fresh solves), while the margin-0 retry used by ``_move_l`` must
find the within-limits solution.
"""

from __future__ import annotations

import math
from pathlib import Path
import unittest

from bookarm.devices.arm.kinematics import PinocchioKinematics
from bookarm.devices.arm.protocol import DEFAULT_JOINT_LIMITS
from bookarm.devices.arm.safety import validate_joint_target
from bookarm.domain.models.pose import BasePose, JointPose

_URDF = Path(__file__).resolve().parents[1] / "resources" / "urdf" / "el_a3.urdf"
_TCP = (0.0, 0.01, -0.14, 0.0, 0.0, 0.0)

# The live session's shift/forward target (identical poses; forward offset 0).
_TARGET = BasePose(
    -0.448,
    -0.1576297,
    0.1837878,
    math.radians(90.0),
    0.0,
    math.radians(90.0),
)
# A chained ik_step candidate near the boundary solution, like _move_l's seed.
_NEAR_SEED = JointPose.from_values(
    tuple(math.radians(value) for value in (33.0, 114.0, -55.0, -58.5, 33.0, 0.0))
)


class LimitMarginIkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.kinematics = PinocchioKinematics(_URDF, _TCP)

    def test_default_margin_rejects_the_boundary_target(self):
        with self.assertRaisesRegex(RuntimeError, "did not converge"):
            self.kinematics.solve_ik(_TARGET, _NEAR_SEED, max_iter=120, eps=3e-4)

    def test_margin_zero_retry_finds_the_within_limits_solution(self):
        solution = self.kinematics.solve_ik(
            _TARGET, _NEAR_SEED, max_iter=120, eps=3e-4, joint_limit_margin=0.0
        )

        # The hard limit check still passes: the retry widens convergence, not
        # the allowed joint space.
        validate_joint_target(solution, DEFAULT_JOINT_LIMITS)
        fk = self.kinematics.forward_kinematics(solution)
        self.assertAlmostEqual(fk.x_m, _TARGET.x_m, places=3)
        self.assertAlmostEqual(fk.y_m, _TARGET.y_m, places=3)
        self.assertAlmostEqual(fk.z_m, _TARGET.z_m, places=3)


if __name__ == "__main__":
    unittest.main()
