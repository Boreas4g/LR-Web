"""Configuration readers reject incomplete calibration before hardware use."""

from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path
import unittest

from bookarm.infrastructure.config.calibration import (
    load_book_stand_pickup_slots,
    load_book_stand_slots,
)
from bookarm.infrastructure.config.profiles import (
    load_return_book_motion_plan,
    load_take_book_motion_plan,
)


class ConfigurationTests(unittest.TestCase):
    def test_example_slot_calibration_is_not_accepted_without_a_calibrated_slot(self):
        example = Path(__file__).resolve().parents[1] / "config" / "calibration" / "book_stand_slots.example.json"

        with self.assertRaisesRegex(ValueError, "uncalibrated"):
            load_book_stand_slots(example)

    def test_single_calibrated_slot_is_loaded(self):
        pose = [0.4, 0.0, 0.2, 0.0, 0.0, 0.0]
        data = {
            "slots": {
                "1": {"pre_place": pose, "place": pose, "lower_place": pose, "retreat": pose}
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "slots.json"
            path.write_text(json.dumps(data), encoding="utf-8")

            slots = load_book_stand_slots(path)

        self.assertEqual(sorted(slots), [1])

    def test_all_four_calibrated_slots_are_loaded(self):
        pose = [0.4, 0.0, 0.2, 0.0, 0.0, 0.0]
        data = {
            "slots": {
                str(index): {"pre_place": pose, "place": pose, "lower_place": pose, "retreat": pose}
                for index in (1, 2, 3, 4)
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "slots.json"
            path.write_text(json.dumps(data), encoding="utf-8")

            slots = load_book_stand_slots(path)

        self.assertEqual(sorted(slots), [1, 2, 3, 4])
        self.assertAlmostEqual(slots[2].place.x_m, 0.4)

    def test_example_motion_profile_loads(self):
        example = Path(__file__).resolve().parents[1] / "config" / "profiles" / "take_book.example.json"

        plan = load_take_book_motion_plan(example)

        self.assertEqual(plan.rod_grasp_deg, -90.0)
        self.assertEqual(plan.gripper.close_deg, 108.5)
        self.assertEqual(plan.gripper.kp, 40.0)
        self.assertEqual(plan.gripper.kd, 3.0)
        for actual, expected in zip(
            (math.degrees(value) for value in plan.debug_joint.values_rad),
            (0.0, 10.0, -65.0, 50.0, 0.0, 0.0),
        ):
            self.assertAlmostEqual(actual, expected, places=4)

    def test_three_pickup_slots_are_loaded(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "config"
            / "calibration"
            / "book_stand_pickup_slots.json"
        )

        slots = load_book_stand_pickup_slots(path)

        # 标定文件支持 1-5 槽位；加载结果应与文件内的槽位集合一致
        import json

        with open(path, encoding="utf-8") as fh:
            expected = sorted(int(key) for key in json.load(fh)["slots"].keys())
        self.assertEqual(sorted(slots), expected)
        self.assertEqual(len(slots[1].grasp.values()), 6)

    def test_return_book_profile_loads_offsets_and_release_angle(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "config"
            / "profiles"
            / "return_book.example.json"
        )

        plan = load_return_book_motion_plan(path)

        self.assertEqual(plan.target_y_offset_cm, 4.0)
        self.assertEqual(plan.insert_offset_cm, (0.0, 0.0, 0.0))
        self.assertEqual(plan.insert_rpy_deg, (95.0, 0.0, 90.0))
        self.assertEqual(plan.min_insert_z_m, 0.25)
        self.assertEqual(plan.pre_insert_x_max_m, -0.2)
        self.assertEqual(plan.pickup_open_deg, 50.0)
        self.assertEqual(plan.release_open_deg, 50.0)
        self.assertEqual(plan.push_x_offset_cm, -5.0)
        self.assertEqual(plan.push_rpy_deg, (0.0, 0.0, 0.0))
        self.assertEqual(plan.retract_x_offset_cm, 10.0)
        self.assertEqual(plan.retract_rpy_deg, (90.0, 0.0, 90.0))
        self.assertEqual(plan.insert_duration_s, 3.0)
        self.assertEqual(plan.push_duration_s, 2.0)
        self.assertEqual(plan.retract_duration_s, 2.0)
        self.assertEqual(plan.gripper.close_deg, 108.5)

    def test_return_book_profile_ignores_the_legacy_pre_insert_x(self):
        # Profiles written while the pre-insert x was a pinned field still
        # carry the key; the loader drops it instead of rejecting the file.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "return.json"
            path.write_text(
                json.dumps({"pre_insert_x_m": -0.31, "target_y_offset_cm": 5.0}),
                encoding="utf-8",
            )

            plan = load_return_book_motion_plan(path)

        self.assertFalse(hasattr(plan, "pre_insert_x_m"))
        self.assertEqual(plan.target_y_offset_cm, 5.0)

    def test_return_book_profile_validates_pre_insert_x_max(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, payload in (
                ("zero.json", {"pre_insert_x_max_m": 0.0}),
                ("positive.json", {"pre_insert_x_max_m": 0.1}),
                ("below_floor.json", {"pre_insert_x_max_m": -0.9}),
            ):
                path = Path(directory) / name
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "pre_insert_x_max_m"):
                    load_return_book_motion_plan(path)

            # Absence loads the dataclass default; an in-range value wins.
            absent = Path(directory) / "absent.json"
            absent.write_text(json.dumps({}), encoding="utf-8")
            self.assertEqual(load_return_book_motion_plan(absent).pre_insert_x_max_m, -0.20)
            ok = Path(directory) / "ok.json"
            ok.write_text(json.dumps({"pre_insert_x_max_m": -0.25}), encoding="utf-8")
            self.assertEqual(load_return_book_motion_plan(ok).pre_insert_x_max_m, -0.25)

    def test_return_book_profile_accepts_and_validates_push_rpy_deg(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, payload in (
                ("two.json", {"push_rpy_deg": [1.0, 2.0]}),
                ("nan.json", {"push_rpy_deg": [0.0, float("nan"), 0.0]}),
            ):
                path = Path(directory) / name
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "push_rpy_deg"):
                    load_return_book_motion_plan(path)

            # Absence loads the dataclass default; a 3-element value wins.
            absent = Path(directory) / "absent.json"
            absent.write_text(json.dumps({}), encoding="utf-8")
            self.assertEqual(load_return_book_motion_plan(absent).push_rpy_deg, (0.0, 0.0, 0.0))
            ok = Path(directory) / "ok.json"
            ok.write_text(json.dumps({"push_rpy_deg": [5.0, -10.0, 2.0]}), encoding="utf-8")
            self.assertEqual(load_return_book_motion_plan(ok).push_rpy_deg, (5.0, -10.0, 2.0))

    def test_return_book_profile_accepts_and_validates_retract_rpy_deg(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, payload in (
                ("two.json", {"retract_rpy_deg": [90.0, 0.0]}),
                ("nan.json", {"retract_rpy_deg": [90.0, float("nan"), 90.0]}),
            ):
                path = Path(directory) / name
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "retract_rpy_deg"):
                    load_return_book_motion_plan(path)

            # Absence loads the dataclass default; a 3-element value wins.
            absent = Path(directory) / "absent.json"
            absent.write_text(json.dumps({}), encoding="utf-8")
            self.assertEqual(
                load_return_book_motion_plan(absent).retract_rpy_deg, (90.0, 0.0, 90.0)
            )
            ok = Path(directory) / "ok.json"
            ok.write_text(
                json.dumps({"retract_rpy_deg": [80.0, 5.0, 85.0]}), encoding="utf-8"
            )
            self.assertEqual(load_return_book_motion_plan(ok).retract_rpy_deg, (80.0, 5.0, 85.0))


if __name__ == "__main__":
    unittest.main()
