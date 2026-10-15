"""Convert transport feedback to arm-base joint feedback and validate freshness."""

from __future__ import annotations

import time
from typing import Dict, Mapping

from bookarm.devices.arm.protocol import MotorFeedback
from bookarm.domain.models.pose import JointPose


def joint_pose_from_feedback(
    feedback: Mapping[int, MotorFeedback],
    directions: Mapping[int, float],
    max_age_s: float,
    offsets: Mapping[int, float] | None = None,
) -> JointPose:
    values = []
    now = time.time()
    for motor_id in range(1, 7):
        item = feedback.get(motor_id)
        if item is None:
            raise RuntimeError("arm feedback is missing joint %d" % motor_id)
        if now - float(item.timestamp_s) > float(max_age_s):
            raise RuntimeError("arm feedback for joint %d is stale" % motor_id)
        # 电机故障码（锁存型，如夹取过流）不阻断读取：恢复流程依赖读数，
        # 故障码在硬件状态页展示并由「故障恢复」按钮清除。
        offset = float(offsets.get(motor_id, 0.0)) if offsets is not None else 0.0
        values.append((float(item.position_rad) - offset) * float(directions[motor_id]))
    return JointPose.from_values(values)


def gripper_angle_from_feedback(
    feedback: Mapping[int, MotorFeedback],
    directions: Mapping[int, float],
    max_age_s: float,
    offsets: Mapping[int, float] | None = None,
) -> float:
    item = feedback.get(7)
    if item is None:
        raise RuntimeError("arm feedback is missing gripper joint")
    if time.time() - float(item.timestamp_s) > float(max_age_s):
        raise RuntimeError("arm feedback for gripper joint is stale")
    # 与关节一致：夹爪故障码不阻断读取，由「故障恢复」按钮清除。
    offset = float(offsets.get(7, 0.0)) if offsets is not None else 0.0
    return (float(item.position_rad) - offset) * float(directions[7])
