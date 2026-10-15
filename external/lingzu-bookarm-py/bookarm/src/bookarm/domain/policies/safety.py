"""Small, deterministic completion checks shared by all use cases."""

from __future__ import annotations

from bookarm.domain.models.pose import JointPose


def ensure_lift_target(target_mm: float, measured_mm: float, tolerance_mm: float) -> None:
    if abs(float(target_mm) - float(measured_mm)) > float(tolerance_mm):
        raise RuntimeError(
            "lift position %.3f mm is outside %.3f mm tolerance of %.3f mm"
            % (float(measured_mm), float(tolerance_mm), float(target_mm))
        )


def ensure_joint_target(target: JointPose, measured: JointPose, tolerance_rad: float) -> None:
    max_error = max(
        abs(float(expected) - float(actual))
        for expected, actual in zip(target.values_rad, measured.values_rad)
    )
    if max_error > float(tolerance_rad):
        raise RuntimeError(
            "arm joint error %.4f rad exceeds tolerance %.4f rad"
            % (max_error, float(tolerance_rad))
        )
