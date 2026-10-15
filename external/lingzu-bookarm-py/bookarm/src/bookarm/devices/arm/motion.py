"""Deterministic joint and Cartesian interpolation for the direct arm service."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterator

import numpy as np

from bookarm.domain.models.pose import BasePose, JointPose


def minimum_jerk(tau: float) -> float:
    """Return a zero-velocity, zero-acceleration endpoint time scale."""

    value = min(max(float(tau), 0.0), 1.0)
    return value * value * value * (10.0 + value * (-15.0 + 6.0 * value))


@dataclass(frozen=True)
class JointTrajectoryPoint:
    time_s: float
    position: JointPose
    velocities_rad_s: tuple[float, ...]
    accelerations_rad_s2: tuple[float, ...]


@dataclass(frozen=True)
class _SCurveProfile:
    p0: float
    direction: float = 1.0
    total_time_s: float = 0.0
    jerk_rad_s3: float = 50.0
    segment_times_s: tuple[float, ...] = (0.0,) * 7


def _plan_scurve(start: float, end: float, velocity: float, acceleration: float, jerk: float) -> _SCurveProfile:
    distance = float(end) - float(start)
    direction = 1.0 if distance >= 0.0 else -1.0
    magnitude = abs(distance)
    if magnitude < 1e-9:
        return _SCurveProfile(p0=float(start))

    jerk_time = acceleration / jerk
    boundary = acceleration * acceleration / jerk
    discriminant = boundary * boundary + 4.0 * acceleration * magnitude
    natural_peak = 0.5 * (-boundary + math.sqrt(discriminant))
    if natural_peak <= acceleration * jerk_time:
        triangular_time = (magnitude / (2.0 * jerk)) ** (1.0 / 3.0)
        natural_peak = jerk * triangular_time * triangular_time
    peak_velocity = min(natural_peak, velocity)

    if peak_velocity >= acceleration * jerk_time - 1e-9:
        t1 = jerk_time
        t2 = max(0.0, peak_velocity / acceleration - jerk_time)
        t3 = jerk_time
        acceleration_distance = peak_velocity * (acceleration / jerk + peak_velocity / acceleration)
    else:
        t1 = math.sqrt(peak_velocity / jerk)
        t2 = 0.0
        t3 = t1
        acceleration_distance = jerk * t1 * t1 * t1
    cruise_distance = magnitude - 2.0 * acceleration_distance
    t4 = max(0.0, cruise_distance / peak_velocity) if peak_velocity > 1e-9 else 0.0
    segments = (t1, t2, t3, t4, t3, t2, t1)
    return _SCurveProfile(
        p0=float(start),
        direction=direction,
        total_time_s=sum(segments),
        jerk_rad_s3=float(jerk),
        segment_times_s=segments,
    )


def _evaluate_scurve(profile: _SCurveProfile, time_s: float) -> tuple[float, float, float]:
    remaining = min(max(0.0, float(time_s)), profile.total_time_s)
    position = profile.p0
    velocity = 0.0
    acceleration = 0.0
    jerk_signs = (1.0, 0.0, -1.0, 0.0, -1.0, 0.0, 1.0)
    for duration, jerk_sign in zip(profile.segment_times_s, jerk_signs):
        if duration <= 1e-9:
            continue
        elapsed = min(remaining, duration)
        jerk = profile.direction * jerk_sign * profile.jerk_rad_s3
        position += velocity * elapsed + 0.5 * acceleration * elapsed**2 + jerk * elapsed**3 / 6.0
        velocity += acceleration * elapsed + 0.5 * jerk * elapsed**2
        acceleration += jerk * elapsed
        remaining -= elapsed
        if remaining <= 1e-9:
            break
    return position, velocity, acceleration


def joint_trajectory(
    start: JointPose,
    target: JointPose,
    duration_s: float,
    rate_hz: float,
    max_velocity_rad_s: float = 3.0,
    max_acceleration_rad_s2: float = 15.0,
    max_jerk_rad_s3: float = 50.0,
) -> Iterator[JointTrajectoryPoint]:
    """Yield a synchronized, boundary-limited seven-segment S-curve trajectory."""

    parameters = (
        float(duration_s), float(rate_hz), float(max_velocity_rad_s),
        float(max_acceleration_rad_s2), float(max_jerk_rad_s3),
    )
    if not all(math.isfinite(value) and value > 0.0 for value in parameters):
        raise ValueError("joint trajectory duration, rate, velocity, acceleration, and jerk must be finite and positive")

    profiles = tuple(
        _plan_scurve(begin, end, parameters[2], parameters[3], parameters[4])
        for begin, end in zip(start.values_rad, target.values_rad)
    )
    natural_duration = max((profile.total_time_s for profile in profiles), default=0.0)
    total_duration = max(parameters[0], natural_duration, 1.0 / parameters[1])
    steps = max(1, int(math.ceil(total_duration * parameters[1])))
    sample_period = total_duration / steps

    for index in range(1, steps + 1):
        current_time = index * sample_period
        positions = []
        velocities = []
        accelerations = []
        for profile, exact_target in zip(profiles, target.values_rad):
            if profile.total_time_s <= 1e-9:
                position, velocity, acceleration = profile.p0, 0.0, 0.0
            else:
                time_scale = profile.total_time_s / total_duration
                position, velocity, acceleration = _evaluate_scurve(profile, current_time * time_scale)
                velocity *= time_scale
                acceleration *= time_scale * time_scale
            if index == steps:
                position, velocity, acceleration = float(exact_target), 0.0, 0.0
            positions.append(position)
            velocities.append(velocity)
            accelerations.append(acceleration)
        yield JointTrajectoryPoint(
            current_time,
            JointPose.from_values(positions),
            tuple(velocities),
            tuple(accelerations),
        )


def joint_targets(
    start: JointPose, target: JointPose, duration_s: float, rate_hz: float
) -> Iterator[tuple[JointPose, float]]:
    """Compatibility wrapper for callers that only consume scalar velocity."""

    for point in joint_trajectory(start, target, duration_s, rate_hz):
        yield point.position, max(abs(value) for value in point.velocities_rad_s)


def joint_path_derivatives(
    positions: tuple[JointPose, ...], duration_s: float
) -> tuple[tuple[tuple[float, ...], ...], tuple[tuple[float, ...], ...]]:
    """Compute SDK-style central-difference velocity and acceleration feedforward."""

    if len(positions) < 2:
        zeros = (0.0,) * 6
        return (zeros,), (zeros,)
    dt = float(duration_s) / float(len(positions) - 1)
    values = [point.values_rad for point in positions]
    velocities = [(0.0,) * 6 for _ in values]
    accelerations = [(0.0,) * 6 for _ in values]
    velocity_rows = []
    acceleration_rows = []
    for index in range(len(values)):
        if index == 0 or index == len(values) - 1:
            velocity_rows.append((0.0,) * 6)
            acceleration_rows.append((0.0,) * 6)
            continue
        velocity_rows.append(tuple(
            (values[index + 1][joint] - values[index - 1][joint]) / (2.0 * dt)
            for joint in range(6)
        ))
        acceleration_rows.append(tuple(
            (values[index + 1][joint] - 2.0 * values[index][joint] + values[index - 1][joint]) / (dt * dt)
            for joint in range(6)
        ))
    return tuple(velocity_rows), tuple(acceleration_rows)


def linear_pose(start: BasePose, target: BasePose, tau: float) -> BasePose:
    """Interpolate position linearly and orientation with GUI-equivalent SLERP."""

    scale = minimum_jerk(tau)
    rx, ry, rz = _slerp_rpy(
        (start.rx_rad, start.ry_rad, start.rz_rad),
        (target.rx_rad, target.ry_rad, target.rz_rad),
        scale,
    )
    return BasePose(
        start.x_m + (target.x_m - start.x_m) * scale,
        start.y_m + (target.y_m - start.y_m) * scale,
        start.z_m + (target.z_m - start.z_m) * scale,
        rx,
        ry,
        rz,
    )


def _slerp_rpy(start: tuple[float, float, float], target: tuple[float, float, float], tau: float) -> tuple[float, float, float]:
    """Spherically interpolate two Pinocchio-compatible RPY rotations."""

    try:
        import pinocchio as pin
    except ImportError:
        pin = None

    if pin is not None:
        q0 = _matrix_to_quaternion(pin.rpy.rpyToMatrix(*start))
        q1 = _matrix_to_quaternion(pin.rpy.rpyToMatrix(*target))
    else:
        q0 = _rpy_to_quaternion(*start)
        q1 = _rpy_to_quaternion(*target)
    dot = float(np.dot(q0, q1))
    if dot < 0.0:
        q1 = -q1
        dot = -dot
    dot = min(max(dot, -1.0), 1.0)
    if dot > 0.9995:
        quaternion = q0 + tau * (q1 - q0)
        quaternion /= np.linalg.norm(quaternion)
    else:
        theta = math.acos(dot)
        sin_theta = math.sin(theta)
        quaternion = (
            math.sin((1.0 - tau) * theta) / sin_theta * q0
            + math.sin(tau * theta) / sin_theta * q1
        )
    if pin is not None:
        rpy = pin.rpy.matrixToRpy(_quaternion_to_matrix(quaternion))
        return float(rpy[0]), float(rpy[1]), float(rpy[2])
    return _quaternion_to_rpy(quaternion)


def _rpy_to_quaternion(rx: float, ry: float, rz: float) -> np.ndarray:
    """Convert Pinocchio RPY to a normalized (w, x, y, z) quaternion."""

    cx, sx = math.cos(rx / 2.0), math.sin(rx / 2.0)
    cy, sy = math.cos(ry / 2.0), math.sin(ry / 2.0)
    cz, sz = math.cos(rz / 2.0), math.sin(rz / 2.0)
    return np.asarray((
        cx * cy * cz - sx * sy * sz,
        sx * cy * cz + cx * sy * sz,
        cx * sy * cz - sx * cy * sz,
        cx * cy * sz + sx * sy * cz,
    ), dtype=float)


def _quaternion_to_rpy(quaternion: np.ndarray) -> tuple[float, float, float]:
    """Convert a normalized (w, x, y, z) quaternion to Pinocchio RPY."""

    w, x, y, z = quaternion / np.linalg.norm(quaternion)
    rx = math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
    ry = math.asin(min(max(2.0 * (w * y - z * x), -1.0), 1.0))
    rz = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    return float(rx), float(ry), float(rz)


def _matrix_to_quaternion(matrix: np.ndarray) -> np.ndarray:
    """Convert a rotation matrix to the GUI SDK's (w, x, y, z) quaternion."""

    matrix = np.asarray(matrix, dtype=float)
    trace = float(np.trace(matrix))
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        quaternion = (0.25 * scale, (matrix[2, 1] - matrix[1, 2]) / scale,
                      (matrix[0, 2] - matrix[2, 0]) / scale, (matrix[1, 0] - matrix[0, 1]) / scale)
    elif matrix[0, 0] > matrix[1, 1] and matrix[0, 0] > matrix[2, 2]:
        scale = math.sqrt(1.0 + matrix[0, 0] - matrix[1, 1] - matrix[2, 2]) * 2.0
        quaternion = ((matrix[2, 1] - matrix[1, 2]) / scale, 0.25 * scale,
                      (matrix[0, 1] + matrix[1, 0]) / scale, (matrix[0, 2] + matrix[2, 0]) / scale)
    elif matrix[1, 1] > matrix[2, 2]:
        scale = math.sqrt(1.0 + matrix[1, 1] - matrix[0, 0] - matrix[2, 2]) * 2.0
        quaternion = ((matrix[0, 2] - matrix[2, 0]) / scale, (matrix[0, 1] + matrix[1, 0]) / scale,
                      0.25 * scale, (matrix[1, 2] + matrix[2, 1]) / scale)
    else:
        scale = math.sqrt(1.0 + matrix[2, 2] - matrix[0, 0] - matrix[1, 1]) * 2.0
        quaternion = ((matrix[1, 0] - matrix[0, 1]) / scale, (matrix[0, 2] + matrix[2, 0]) / scale,
                      (matrix[1, 2] + matrix[2, 1]) / scale, 0.25 * scale)
    result = np.asarray(quaternion, dtype=float)
    return result / np.linalg.norm(result)


def _quaternion_to_matrix(quaternion: np.ndarray) -> np.ndarray:
    """Convert a normalized (w, x, y, z) quaternion to a rotation matrix."""

    w, x, y, z = quaternion / np.linalg.norm(quaternion)
    return np.asarray((
        (1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w)),
        (2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w)),
        (2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y)),
    ), dtype=float)
