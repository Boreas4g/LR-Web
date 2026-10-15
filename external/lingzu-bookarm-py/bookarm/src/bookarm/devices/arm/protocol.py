"""Robstride CAN protocol values and direct transport contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import math
import time
from typing import Dict, Protocol


class MotorType(IntEnum):
    RS00 = 0
    EL05 = 1
    RS05 = 2


class CommType(IntEnum):
    MOTION_CONTROL = 1
    FEEDBACK = 2
    ENABLE = 3
    DISABLE = 4
    SET_ZERO = 6
    WRITE_PARAM = 18
    SET_AUTO_REPORT = 24


class RunMode(IntEnum):
    MOTION_CONTROL = 0
    POSITION_PP = 1
    VELOCITY = 2
    CURRENT = 3
    POSITION_CSP = 5


class ParamIndex(IntEnum):
    RUN_MODE = 0x7005
    LOC_REF = 0x7016
    LIMIT_SPD = 0x7017
    VEL_MAX = 0x7024
    ACC_SET = 0x7025
    CAN_TIMEOUT = 0x7028


class MotorModeState(IntEnum):
    """Mode bits carried by a RobStride Type 2 feedback CAN identifier."""

    RESET = 0
    CALIBRATION = 1
    MOTOR = 2


@dataclass(frozen=True)
class MotorLimits:
    position_min_rad: float
    position_max_rad: float
    velocity_min_rad_s: float
    velocity_max_rad_s: float
    torque_min_nm: float
    torque_max_nm: float
    kp_min: float = 0.0
    kp_max: float = 500.0
    kd_min: float = 0.0
    kd_max: float = 5.0


MOTOR_LIMITS: Dict[MotorType, MotorLimits] = {
    MotorType.RS00: MotorLimits(-12.57, 12.57, -33.0, 33.0, -14.0, 14.0),
    MotorType.EL05: MotorLimits(-12.57, 12.57, -50.0, 50.0, -6.0, 6.0),
    MotorType.RS05: MotorLimits(-12.57, 12.57, -50.0, 50.0, -5.5, 5.5),
}

DEFAULT_MOTOR_TYPES = {
    1: MotorType.RS00,
    2: MotorType.RS00,
    3: MotorType.RS00,
    4: MotorType.EL05,
    5: MotorType.EL05,
    6: MotorType.EL05,
    7: MotorType.EL05,
}

DEFAULT_DIRECTIONS = {1: -1.0, 2: 1.0, 3: -1.0, 4: 1.0, 5: -1.0, 6: 1.0, 7: 1.0}
DEFAULT_OFFSETS = {motor_id: 0.0 for motor_id in range(1, 8)}
DEFAULT_JOINT_LIMITS = {
    1: (-4.71239, 4.71239),
    2: (0.0, 3.66519),
    3: (-4.01426, 0.0),
    4: (-1.0472, 1.5708),
    5: (-1.5708, 1.5708),
    6: (-1.5708, 1.5708),
    7: (0.0, math.radians(108.5)),
}


@dataclass(frozen=True)
class MotorFeedback:
    motor_id: int
    position_rad: float
    velocity_rad_s: float
    torque_nm: float
    temperature_c: float
    fault_code: int
    timestamp_s: float
    mode_state: int = MotorModeState.RESET


class ArmTransport(Protocol):
    def connect(self) -> None:
        ...

    def close(self) -> None:
        ...

    def enable_motor(self, motor_id: int) -> bool:
        ...

    def disable_motor(self, motor_id: int, clear_fault: bool = False) -> bool:
        ...

    def enable_feedback(self, motor_id: int) -> bool:
        ...

    def set_run_mode(self, motor_id: int, mode: RunMode) -> bool:
        ...

    def set_can_timeout(self, motor_id: int, timeout_s: float) -> bool:
        ...

    def set_pp_velocity(self, motor_id: int, velocity_rad_s: float) -> bool:
        ...

    def set_pp_acceleration(self, motor_id: int, acceleration_rad_s2: float) -> bool:
        ...

    def send_motion(
        self, motor_id: int, position_rad: float, velocity_rad_s: float,
        kp: float, kd: float, torque_nm: float
    ) -> bool:
        ...

    def feedback(self) -> Dict[int, MotorFeedback]:
        ...

    def can_fps(self) -> float:
        ...

    def emergency_stop(self) -> bool:
        ...

    def set_zero_position(self, motor_id: int) -> bool:
        ...


def fresh_feedback(feedback: MotorFeedback, max_age_s: float) -> bool:
    return time.time() - float(feedback.timestamp_s) <= float(max_age_s)
