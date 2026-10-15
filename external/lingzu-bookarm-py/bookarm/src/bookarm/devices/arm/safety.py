"""Direct arm command validation and feedback arrival checks."""

from __future__ import annotations

from typing import Mapping, Sequence

from bookarm.devices.arm.protocol import DEFAULT_JOINT_LIMITS
from bookarm.domain.models.pose import JointPose


def validate_joint_target(target: JointPose, limits: Mapping[int, Sequence[float]] = DEFAULT_JOINT_LIMITS) -> None:
    for index, value in enumerate(target.values_rad, start=1):
        minimum, maximum = limits[index]
        if not float(minimum) <= float(value) <= float(maximum):
            raise ValueError(
                "arm joint %d target %.4f rad is outside [%.4f, %.4f]" % (index, value, minimum, maximum)
            )


def protect_joint_target(
    target: JointPose,
    limits: Mapping[int, Sequence[float]] = DEFAULT_JOINT_LIMITS,
    stop_margin_rad: float = 0.0,
) -> JointPose:
    """Keep commanded positions inside the SDK's hard-stop margin."""

    margin = max(0.0, float(stop_margin_rad))
    values = []
    for index, value in enumerate(target.values_rad, start=1):
        minimum, maximum = limits[index]
        lower = float(minimum) + margin
        upper = float(maximum) - margin
        if lower > upper:
            raise ValueError("arm joint %d has no usable range after stop margin" % index)
        values.append(min(max(float(value), lower), upper))
    return JointPose.from_values(values)


def ensure_joint_arrival(target: JointPose, measured: JointPose, tolerance_rad: float) -> None:
    error = max(abs(expected - actual) for expected, actual in zip(target.values_rad, measured.values_rad))
    if error > float(tolerance_rad):
        raise RuntimeError("arm final joint error %.4f rad exceeds %.4f rad" % (error, tolerance_rad))
