"""Coordinate-frame-safe robot pose data."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Optional, Sequence, Tuple


@dataclass(frozen=True)
class BasePose:
    """Pose in robot base frame, using metres and radians."""

    x_m: float
    y_m: float
    z_m: float
    rx_rad: float
    ry_rad: float
    rz_rad: float

    def __post_init__(self) -> None:
        if not all(math.isfinite(float(value)) for value in self.values()):
            raise ValueError("base pose values must be finite")

    def values(self) -> Tuple[float, float, float, float, float, float]:
        return (
            float(self.x_m), float(self.y_m), float(self.z_m),
            float(self.rx_rad), float(self.ry_rad), float(self.rz_rad),
        )

    def offset_cm(
        self,
        xyz_cm: Sequence[float],
        *,
        rpy_deg: Optional[Sequence[float]] = None,
        rpy_offset_deg: Optional[Sequence[float]] = None,
    ) -> "BasePose":
        """Create a base-frame offset pose without depending on GUI helpers."""

        if len(xyz_cm) != 3:
            raise ValueError("xyz_cm must contain three values")
        if rpy_deg is not None and rpy_offset_deg is not None:
            raise ValueError("use either rpy_deg or rpy_offset_deg")
        rx, ry, rz = self.rx_rad, self.ry_rad, self.rz_rad
        if rpy_deg is not None:
            if len(rpy_deg) != 3:
                raise ValueError("rpy_deg must contain three values")
            rx, ry, rz = (math.radians(float(value)) for value in rpy_deg)
        elif rpy_offset_deg is not None:
            if len(rpy_offset_deg) != 3:
                raise ValueError("rpy_offset_deg must contain three values")
            rx += math.radians(float(rpy_offset_deg[0]))
            ry += math.radians(float(rpy_offset_deg[1]))
            rz += math.radians(float(rpy_offset_deg[2]))
        return BasePose(
            self.x_m + float(xyz_cm[0]) / 100.0,
            self.y_m + float(xyz_cm[1]) / 100.0,
            self.z_m + float(xyz_cm[2]) / 100.0,
            rx,
            ry,
            rz,
        )

    @classmethod
    def from_values(cls, values: Sequence[float]) -> "BasePose":
        if len(values) != 6:
            raise ValueError("base pose must contain six values")
        return cls(*(float(value) for value in values))


@dataclass(frozen=True)
class JointPose:
    """Six arm joints in radians."""

    values_rad: Tuple[float, float, float, float, float, float]

    def __post_init__(self) -> None:
        if len(self.values_rad) != 6 or not all(math.isfinite(float(value)) for value in self.values_rad):
            raise ValueError("joint pose must contain six finite values")

    @classmethod
    def from_values(cls, values: Sequence[float]) -> "JointPose":
        if len(values) != 6:
            raise ValueError("joint pose must contain six values")
        return cls(tuple(float(value) for value in values))
