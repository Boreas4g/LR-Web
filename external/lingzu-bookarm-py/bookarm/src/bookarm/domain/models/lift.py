"""Hardware-neutral result of one verified lift positioning operation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LiftPositionResult:
    """A completed absolute lift move expressed in workflow millimetres.

    ``controller_*`` fields are retained for commissioning and fault analysis;
    callers normally use the workflow-coordinate target and measurement.
    """

    target_height_mm: float
    measured_height_mm: float
    tolerance_pulses: int
    controller_target_pulses: int
    controller_measured_pulses: int
