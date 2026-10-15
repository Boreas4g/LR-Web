"""Calibration-backed transformation from D435 to arm-base coordinates."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Sequence, Tuple

import numpy as np


class CameraToBaseTransform:
    """Apply ``p_base = T_base_camera @ p_camera`` with a validated 4x4 matrix."""

    def __init__(self, matrix: Sequence[Sequence[float]]) -> None:
        self._matrix = np.asarray(matrix, dtype=float).reshape(4, 4)
        if not np.all(np.isfinite(self._matrix)):
            raise ValueError("camera-to-base transform contains non-finite values")
        if not np.allclose(self._matrix[3], [0.0, 0.0, 0.0, 1.0], atol=1e-9):
            raise ValueError("camera-to-base transform must be homogeneous")

    @classmethod
    def from_json(cls, path: Path) -> "CameraToBaseTransform":
        path = Path(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise FileNotFoundError("camera calibration file not found: %s" % path) from error
        except json.JSONDecodeError as error:
            raise ValueError("camera calibration is not valid JSON: %s" % error) from error
        if not isinstance(payload, dict) or "matrix" not in payload:
            raise ValueError("camera calibration requires a 4x4 matrix")
        return cls(payload["matrix"])

    @property
    def matrix(self) -> np.ndarray:
        """Defensive copy of the validated 4x4 homogeneous matrix."""

        return self._matrix.copy()

    def transform_point(self, point_camera_m: Sequence[float]) -> tuple[float, float, float]:
        point = np.asarray(point_camera_m, dtype=float).reshape(3)
        if not np.all(np.isfinite(point)):
            raise ValueError("camera point contains non-finite values")
        mapped = self._matrix @ np.array([point[0], point[1], point[2], 1.0], dtype=float)
        return (float(mapped[0]), float(mapped[1]), float(mapped[2]))


def rotation_to_rpy(rotation: Sequence[Sequence[float]]) -> Tuple[float, float, float]:
    """Decompose a rotation matrix into roll-pitch-yaw angles (radians).

    Convention: ``R = Rz(rz) @ Ry(ry) @ Rx(rx)`` (fixed-axis X-Y-Z), the same
    roll-pitch-yaw convention pinocchio's ``rpyToMatrix``/``matrixToRpy`` use
    for arm base poses elsewhere in this project.
    """

    r = np.asarray(rotation, dtype=float).reshape(3, 3)
    sy = math.sqrt(float(r[0, 0] ** 2 + r[1, 0] ** 2))
    if sy > 1e-9:
        rx = math.atan2(float(r[2, 1]), float(r[2, 2]))
        ry = math.atan2(float(-r[2, 0]), sy)
        rz = math.atan2(float(r[1, 0]), float(r[0, 0]))
    else:
        # Gimbal lock (ry = ±90°): roll is unobservable, fold it into rx.
        rx = math.atan2(float(-r[1, 2]), float(r[1, 1]))
        ry = math.atan2(float(-r[2, 0]), sy)
        rz = 0.0
    return (rx, ry, rz)


def rpy_to_rotation(rx_rad: float, ry_rad: float, rz_rad: float) -> np.ndarray:
    """Compose roll-pitch-yaw angles into ``R = Rz(rz) @ Ry(ry) @ Rx(rx)``."""

    cx, sx = math.cos(rx_rad), math.sin(rx_rad)
    cy, sy = math.cos(ry_rad), math.sin(ry_rad)
    cz, sz = math.cos(rz_rad), math.sin(rz_rad)
    return np.array(
        [
            [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
            [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
            [-sy, cy * sx, cy * cx],
        ],
        dtype=float,
    )


def matrix_to_pose6(matrix: Sequence[Sequence[float]]) -> Tuple[float, float, float, float, float, float]:
    """Split a 4x4 homogeneous matrix into (x, y, z in m, rx, ry, rz in rad)."""

    m = np.asarray(matrix, dtype=float).reshape(4, 4)
    rx, ry, rz = rotation_to_rpy(m[:3, :3])
    return (
        float(m[0, 3]), float(m[1, 3]), float(m[2, 3]),
        rx, ry, rz,
    )


def pose6_to_matrix(
    x_m: float, y_m: float, z_m: float,
    rx_rad: float, ry_rad: float, rz_rad: float,
) -> list:
    """Compose a 4x4 homogeneous matrix (nested lists) from a 6-DOF pose."""

    rotation = rpy_to_rotation(rx_rad, ry_rad, rz_rad)
    matrix = np.eye(4)
    matrix[:3, :3] = rotation
    matrix[:3, 3] = [x_m, y_m, z_m]
    return matrix.tolist()
