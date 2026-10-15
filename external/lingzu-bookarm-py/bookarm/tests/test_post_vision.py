"""post_vision（书脊倾斜判定 + 车辆居中脉冲对齐）单元测试。"""

from __future__ import annotations

import math

import pytest

from bookarm.application.post_vision import (
    align_vehicle_to_center,
    center_x_ratio,
    check_spine_tilt,
    compute_spine_tilt_deg,
)
from bookarm.domain.models.book import BookTarget
from bookarm.domain.models.pose import BasePose
from bookarm.domain.plans.take_book import TakeBookMotionPlan


def _quad(cx: float, tilt_deg: float = 0.0, w: float = 100.0, h: float = 300.0,
          cy: float = 360.0):
    """生成中心在 (cx, cy)、宽 w 高 h、顺时针旋转 tilt_deg 度的四边形角点。"""
    rad = math.radians(tilt_deg)
    cos_r, sin_r = math.cos(rad), math.sin(rad)
    points = []
    for dx, dy in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
        points.append((cx + dx * cos_r - dy * sin_r, cy + dx * sin_r + dy * cos_r))
    return tuple(points)


def _target(cx: float, tilt_deg: float = 0.0, frame_w: int = 1280) -> BookTarget:
    pose = BasePose(-0.3, 0.0, 0.5, 0.0, 0.0, 0.0)
    return BookTarget(
        raw_pose=pose,
        grasp_pose=pose,
        polygon_uv=_quad(cx, tilt_deg),
        frame_width_px=frame_w,
        frame_height_px=720,
    )


class _FakeVehicle:
    def __init__(self):
        self.pulses = []

    def pulse(self, direction: str, duration_ms: int):
        self.pulses.append((direction, duration_ms))
        return {"ok": True}


class _FakeVision:
    """每次 locate_book 依队列返回识别结果。"""

    def __init__(self, targets):
        self._targets = list(targets)
        self.calls = 0

    def locate_book(self, template):
        self.calls += 1
        if not self._targets:
            raise RuntimeError("no more targets")
        return self._targets.pop(0)


class TestTilt:
    def test_vertical_is_zero(self):
        assert compute_spine_tilt_deg(_quad(640.0)) == pytest.approx(0.0, abs=0.5)

    def test_tilt_30(self):
        assert compute_spine_tilt_deg(_quad(640.0, tilt_deg=30.0)) == pytest.approx(30.0, abs=0.5)

    def test_horizontal_is_90(self):
        assert compute_spine_tilt_deg(_quad(640.0, w=300.0, h=100.0)) == pytest.approx(90.0, abs=0.5)

    def test_none_and_malformed(self):
        assert compute_spine_tilt_deg(None) is None
        assert compute_spine_tilt_deg(((0, 0), (1, 1), (2, 2))) is None

    def test_boundary_60(self):
        plan = TakeBookMotionPlan()
        # 恰好 60° 不超过阈值 → 放行
        assert check_spine_tilt(_target(640.0, tilt_deg=60.0), plan) == pytest.approx(60.0, abs=0.5)
        # 61° 超过 → 中止
        with pytest.raises(RuntimeError, match="书脊倾斜"):
            check_spine_tilt(_target(640.0, tilt_deg=61.0), plan)

    def test_unknown_tilt_policy(self):
        target = BookTarget(
            raw_pose=BasePose(-0.3, 0.0, 0.5, 0.0, 0.0, 0.0),
            grasp_pose=BasePose(-0.3, 0.0, 0.5, 0.0, 0.0, 0.0),
        )
        assert check_spine_tilt(target, TakeBookMotionPlan()) is None
        strict = TakeBookMotionPlan(spine_tilt_fail_on_unknown=True)
        with pytest.raises(RuntimeError, match="无法计算"):
            check_spine_tilt(target, strict)


class TestCenterAlign:
    def test_center_ratio(self):
        assert center_x_ratio(_target(640.0)) == pytest.approx(0.5, abs=0.01)
        assert center_x_ratio(_target(960.0)) == pytest.approx(0.75, abs=0.01)

    def test_already_centered_no_pulse(self):
        vehicle = _FakeVehicle()
        target = _target(640.0)
        final, details = align_vehicle_to_center(
            _FakeVision([]), None, target, vehicle, TakeBookMotionPlan()
        )
        assert final is target
        assert details["aligned"] is False
        assert vehicle.pulses == []

    def test_right_offset_pulses_forward_then_stops(self):
        vehicle = _FakeVehicle()
        # 初始偏右（0.86），动一次后居中
        vision = _FakeVision([_target(640.0)])
        final, details = align_vehicle_to_center(
            vision, None, _target(1100.0), vehicle, TakeBookMotionPlan()
        )
        assert vehicle.pulses, "应发出脉冲"
        assert vehicle.pulses[0][0] == "forward"
        assert 100 <= vehicle.pulses[0][1] <= 400
        assert details["aligned"] is True
        assert details["align_moves"] == 1
        assert vision.calls == 1

    def test_sign_minus_one_reverses_direction(self):
        vehicle = _FakeVehicle()
        vision = _FakeVision([_target(640.0)])
        plan = TakeBookMotionPlan(align_direction_sign=-1)
        align_vehicle_to_center(vision, None, _target(1100.0), vehicle, plan)
        assert vehicle.pulses[0][0] == "backward"

    def test_max_moves_exceeded(self):
        vehicle = _FakeVehicle()
        # 每次都还是偏右 → 永远对不齐
        vision = _FakeVision([_target(1100.0) for _ in range(20)])
        with pytest.raises(RuntimeError, match="车辆对齐失败"):
            align_vehicle_to_center(vision, None, _target(1100.0), vehicle, TakeBookMotionPlan())

    def test_travel_cap(self):
        vehicle = _FakeVehicle()
        vision = _FakeVision([_target(1100.0) for _ in range(20)])
        plan = TakeBookMotionPlan(align_max_travel_m=0.05, align_max_moves=20)
        with pytest.raises(RuntimeError, match="行程"):
            align_vehicle_to_center(vision, None, _target(1100.0), vehicle, plan)
