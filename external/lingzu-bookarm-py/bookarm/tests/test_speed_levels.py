"""Speed-level presets: uniform scaling of durations, rod and gripper speeds."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from bookarm.domain.plans.mid_return import MidReturnMotionPlan
from bookarm.domain.plans.return_book import ReturnBookMotionPlan
from bookarm.domain.plans.speed import (
    DEFAULT_SPEED_LEVEL,
    SPEED_LEVELS,
    SPEED_PROFILES,
    apply_speed_level,
    remove_speed_scaling,
    validate_speed_level,
)
from bookarm.domain.plans.take_book import TakeBookMotionPlan
from bookarm.infrastructure.config.profiles import (
    load_mid_return_motion_plan,
    load_return_book_motion_plan,
    load_take_book_motion_plan,
)


class ValidateSpeedLevelTests(unittest.TestCase):
    def test_accepts_the_three_levels(self):
        for level in ("slow", "standard", "fast"):
            self.assertEqual(validate_speed_level(level), level)
        self.assertEqual(SPEED_LEVELS, ("slow", "standard", "fast"))
        self.assertEqual(DEFAULT_SPEED_LEVEL, "standard")
        self.assertEqual(tuple(SPEED_PROFILES), SPEED_LEVELS)

    def test_rejects_unknown_levels_and_non_strings(self):
        for bad in ("turbo", "", "Standard", None, 1, 1.0, ["fast"]):
            with self.assertRaises(ValueError):
                validate_speed_level(bad)


class ApplySpeedLevelTests(unittest.TestCase):
    def test_plans_default_to_standard(self):
        self.assertEqual(TakeBookMotionPlan().speed_level, "standard")
        self.assertEqual(ReturnBookMotionPlan().speed_level, "standard")
        self.assertEqual(MidReturnMotionPlan().speed_level, "standard")

    def test_standard_is_an_exact_identity(self):
        for plan in (TakeBookMotionPlan(), ReturnBookMotionPlan(), MidReturnMotionPlan()):
            self.assertEqual(apply_speed_level(plan), plan)
            self.assertEqual(remove_speed_scaling(plan), plan)

    def test_fast_scales_take_book_uniformly(self):
        plan = apply_speed_level(replace(TakeBookMotionPlan(), speed_level="fast"))
        self.assertEqual(plan.speed_level, "fast")
        # 3-second tier -> 1.8s; 2-second tier -> 1.2s.
        self.assertEqual(plan.zero_duration_s, 1.8)
        self.assertEqual(plan.debug_duration_s, 1.8)
        self.assertEqual(plan.pregrasp_duration_s, 1.8)
        self.assertEqual(plan.approach_duration_s, 1.2)
        self.assertEqual(plan.fine_grasp_duration_s, 1.2)
        self.assertEqual(plan.post_grasp_duration_s, 1.2)
        self.assertEqual(plan.rod_speed, 1800)
        gripper = plan.gripper
        self.assertEqual(gripper.open_speed_deg_s, 60.12)
        self.assertEqual(gripper.close_speed_deg_s, 60.12)
        self.assertEqual(gripper.command_lead_s, 0.15)
        self.assertEqual(gripper.stall_time_s, 0.51)
        self.assertEqual(gripper.contact_hold_duration_s, 0.6)
        # Contact-judgement and torque constants never scale with the level.
        self.assertEqual(gripper.min_monitor_s, 0.4)
        self.assertEqual(gripper.start_boost_s, 0.4)
        self.assertEqual(gripper.monitor_period_s, 0.05)
        self.assertEqual(gripper.timeout_s, 10.0)
        self.assertEqual(gripper.effort, 0.5)
        self.assertEqual(gripper.carry_effort, 0.2)
        self.assertEqual(gripper.stall_lead_threshold_deg, 3.0)
        self.assertEqual(gripper.stall_tolerance_deg, 0.4)
        self.assertEqual(gripper.close_deg, 108.5)
        # Non-speed plan fields are untouched.
        self.assertEqual(plan.rod_timeout_s, 12.0)
        self.assertEqual(plan.rod_acceleration, 50)
        self.assertEqual(plan.joint_tolerance_rad, 0.08)
        self.assertEqual(plan.pregrasp_offset_cm, (10.0, 0.0, 0.0))

    def test_slow_scales_take_book_uniformly(self):
        plan = apply_speed_level(replace(TakeBookMotionPlan(), speed_level="slow"))
        self.assertEqual(plan.zero_duration_s, 4.5)
        self.assertEqual(plan.approach_duration_s, 3.0)
        self.assertEqual(plan.rod_speed, 700)
        self.assertEqual(plan.gripper.open_speed_deg_s, 11.69)
        self.assertEqual(plan.gripper.close_speed_deg_s, 11.69)
        self.assertEqual(plan.gripper.command_lead_s, 0.4)
        self.assertEqual(plan.gripper.stall_time_s, 0.78)
        self.assertEqual(plan.gripper.contact_hold_duration_s, 1.2)

    def test_fast_scales_every_mid_return_duration(self):
        plan = apply_speed_level(replace(MidReturnMotionPlan(), speed_level="fast"))
        for name in (
            "zero_duration_s",
            "debug_duration_s",
            "pregrasp_duration_s",
            "pre_insert_duration_s",
            "insert_duration_s",
        ):
            self.assertEqual(getattr(plan, name), 1.8, name)
        for name in (
            "approach_duration_s",
            "fine_grasp_duration_s",
            "push_in_duration_s",
            "re_push_duration_s",
            "lower_duration_s",
            "shift_duration_s",
            "carry_forward_duration_s",
            "forward_duration_s",
            "push_duration_s",
            "retract_duration_s",
        ):
            self.assertEqual(getattr(plan, name), 1.2, name)
        self.assertEqual(plan.rod_speed, 1800)
        # Insert geometry is not speed-coupled.
        self.assertEqual(plan.target_y_offset_cm, 4.0)
        self.assertEqual(plan.pre_insert_x_m, -0.33)

    def test_fast_scales_return_book(self):
        plan = apply_speed_level(replace(ReturnBookMotionPlan(), speed_level="fast"))
        self.assertEqual(plan.zero_duration_s, 1.8)
        self.assertEqual(plan.debug_duration_s, 1.8)
        self.assertEqual(plan.pregrasp_duration_s, 1.8)
        self.assertEqual(plan.pre_insert_duration_s, 1.8)
        self.assertEqual(plan.insert_duration_s, 1.8)
        self.assertEqual(plan.push_duration_s, 1.2)
        self.assertEqual(plan.retract_duration_s, 1.2)
        self.assertEqual(plan.gripper.close_speed_deg_s, 60.12)
        # The rod-less plan has no rod word; insert geometry never scales.
        self.assertFalse(hasattr(plan, "rod_speed"))
        self.assertEqual(plan.target_y_offset_cm, 4.0)
        self.assertEqual(plan.pre_insert_x_max_m, -0.20)

    def test_scaling_respects_tuned_base_values(self):
        base = replace(
            TakeBookMotionPlan(),
            speed_level="fast",
            zero_duration_s=4.0,
            rod_speed=1200,
        )
        plan = apply_speed_level(base)
        self.assertEqual(plan.zero_duration_s, 2.4)
        self.assertEqual(plan.rod_speed, 2160)

    def test_apply_then_remove_returns_the_exact_base(self):
        for factory in (TakeBookMotionPlan, ReturnBookMotionPlan, MidReturnMotionPlan):
            for level in ("slow", "fast"):
                base = replace(factory(), speed_level=level)
                self.assertEqual(remove_speed_scaling(apply_speed_level(base)), base)


class ProfileLoaderSpeedLevelTests(unittest.TestCase):
    def setUp(self):
        self._dirs = tempfile.TemporaryDirectory()
        self.addCleanup(self._dirs.cleanup)
        self.root = Path(self._dirs.name)

    def _write(self, name: str, document: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_missing_speed_level_loads_as_standard_unchanged(self):
        plan = load_take_book_motion_plan(self._write("take.json", {}))
        self.assertEqual(plan.speed_level, "standard")
        self.assertEqual(plan, TakeBookMotionPlan())

        mid = load_mid_return_motion_plan(self._write("mid.json", {}))
        self.assertEqual(mid.speed_level, "standard")
        self.assertEqual(mid, MidReturnMotionPlan())

        ret = load_return_book_motion_plan(self._write("return.json", {}))
        self.assertEqual(ret.speed_level, "standard")
        self.assertEqual(ret, ReturnBookMotionPlan())

    def test_loader_applies_fast_level(self):
        plan = load_take_book_motion_plan(
            self._write("take.json", {"speed_level": "fast", "zero_duration_s": 4.0})
        )
        self.assertEqual(plan.speed_level, "fast")
        self.assertEqual(plan.zero_duration_s, 2.4)
        self.assertEqual(plan.pregrasp_duration_s, 1.8)
        self.assertEqual(plan.rod_speed, 1800)
        self.assertEqual(plan.gripper.close_speed_deg_s, 60.12)

    def test_loader_applies_slow_level_to_mid_return_and_return(self):
        mid = load_mid_return_motion_plan(self._write("mid.json", {"speed_level": "slow"}))
        self.assertEqual(mid.insert_duration_s, 4.5)
        self.assertEqual(mid.push_duration_s, 3.0)
        ret = load_return_book_motion_plan(self._write("return.json", {"speed_level": "slow"}))
        self.assertEqual(ret.pre_insert_duration_s, 4.5)
        self.assertEqual(ret.insert_duration_s, 4.5)
        self.assertEqual(ret.push_duration_s, 3.0)

    def test_loader_rejects_an_unknown_speed_level(self):
        with self.assertRaises(ValueError):
            load_take_book_motion_plan(self._write("take.json", {"speed_level": "turbo"}))
        with self.assertRaises(ValueError):
            load_mid_return_motion_plan(self._write("mid.json", {"speed_level": 3}))
        with self.assertRaises(ValueError):
            load_return_book_motion_plan(self._write("return.json", {"speed_level": ""}))


if __name__ == "__main__":
    unittest.main()
