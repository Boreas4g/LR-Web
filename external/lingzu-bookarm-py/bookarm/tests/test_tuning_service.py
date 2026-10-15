"""HTTP motion-plan tuning service tests with the hardware-free backends."""

from __future__ import annotations

import math
from pathlib import Path
import tempfile
import threading
import time
import unittest

import numpy as np
from fastapi.testclient import TestClient

from bookarm.api.requests import TakeBookRequest, TakeBookStage
from bookarm.domain.models.book import BookIdentity, SpineTemplate
from bookarm.domain.models.shelf import BookStandSlot
from bookarm.domain.models.task import TaskState
from bookarm.infrastructure.config.calibration import (
    load_book_stand_pickup_slots,
    load_book_stand_slots,
)
from bookarm.infrastructure.config.profiles import (
    load_return_book_motion_plan,
    load_take_book_motion_plan,
)
from bookarm.service.app import create_app
from bookarm.service.runner import FakeWorkflowRunner
from bookarm.service.studio import FakeStudioBackend, StudioStore
from bookarm.service.tuning import TuningDevices, create_fake_tuning


TUNING_URL = "/api/v1/tuning"


class TuningServiceTests(unittest.TestCase):
    def setUp(self):
        self._dirs = tempfile.TemporaryDirectory()
        self.addCleanup(self._dirs.cleanup)
        root = Path(self._dirs.name)
        self.upload_dir = root / "uploads"
        self.tuning_dir = root / "tuning"
        self.runner = FakeWorkflowRunner(stage_delay_s=0.01)
        self.addCleanup(self.runner.shutdown)
        self.studio = FakeStudioBackend(
            StudioStore(root / "studio"), busy_lock=self.runner.busy_lock
        )
        self.tuning = create_fake_tuning(self.runner, self.studio, store_dir=self.tuning_dir)
        self.client = TestClient(
            create_app(
                self.runner,
                upload_dir=self.upload_dir,
                studio=self.studio,
                tuning=self.tuning,
            )
        )

    def library_id(self) -> str:
        image_bgr = np.full((12, 8, 3), 180, np.uint8)
        return self.studio.import_library_image(image_bgr, source="test").id

    def prepare(self, kind: str, slot: int = 1) -> dict:
        response = self.client.post(
            "%s/%s/session/prepare" % (TUNING_URL, kind),
            json={"slot": slot, "template_id": self.library_id()},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_get_profile_returns_all_fields(self):
        response = self.client.get("%s/take/profile" % TUNING_URL)

        self.assertEqual(response.status_code, 200)
        profile = response.json()
        self.assertEqual(profile["grasp_rpy_deg"], [60.0, 0.0, 90.0])
        self.assertEqual(profile["fine_grasp_rpy_deg"], [95.0, 0.0, 90.0])
        self.assertEqual(len(profile["zero_joint"]), 6)
        self.assertEqual(len(profile["debug_joint"]), 6)
        self.assertEqual(profile["rod_grasp_deg"], -70.0)
        self.assertEqual(profile["rod_home_deg"], 0.0)
        self.assertEqual(profile["gripper"]["close_deg"], 108.5)

        return_profile = self.client.get("%s/return/profile" % TUNING_URL).json()
        self.assertEqual(return_profile["target_y_offset_cm"], 4.0)
        self.assertEqual(return_profile["insert_rpy_deg"], [95.0, 0.0, 90.0])
        self.assertEqual(return_profile["min_insert_z_m"], 0.25)
        self.assertIn("gripper", return_profile)
        # The retired shelf-placement and rod fields are gone.
        for removed in (
            "shelf_reference_point_from_top_ratio",
            "shelf_target_base_offset_cm",
            "pre_place_offset_cm",
            "place_offset_cm",
            "place_rpy_deg",
            "pre_place_duration_s",
            "place_duration_s",
            "retreat_duration_s",
            "rod_prepare_deg",
            "prepare_gripper_open_deg",
            "rod_speed",
            "pre_insert_x_m",
        ):
            self.assertNotIn(removed, return_profile)

        unknown = self.client.get("%s/bogus/profile" % TUNING_URL)
        self.assertEqual(unknown.status_code, 404)

    def test_put_partial_update_applies_and_persists(self):
        response = self.client.put(
            "%s/take/profile" % TUNING_URL,
            json={"fine_grasp_rpy_deg": [80.0, 0.0, 90.0], "gripper": {"effort": 0.7}},
        )

        self.assertEqual(response.status_code, 200)
        current = self.client.get("%s/take/profile" % TUNING_URL).json()
        self.assertEqual(current["fine_grasp_rpy_deg"], [80.0, 0.0, 90.0])
        self.assertEqual(current["grasp_rpy_deg"], [60.0, 0.0, 90.0])
        self.assertEqual(current["gripper"]["effort"], 0.7)
        self.assertEqual(current["gripper"]["open_deg"], 30.0)
        # The live plan the fake runner holds is hot-swapped, not copied.
        self.assertEqual(self.runner.take_plan.fine_grasp_rpy_deg, (80.0, 0.0, 90.0))

        persisted = self.tuning_dir / "take_profile.json"
        self.assertTrue(persisted.is_file())
        reloaded = load_take_book_motion_plan(persisted)
        self.assertEqual(reloaded.fine_grasp_rpy_deg, (80.0, 0.0, 90.0))
        self.assertEqual(reloaded.gripper.effort, 0.7)

    def test_put_unknown_field_is_rejected(self):
        response = self.client.put("%s/take/profile" % TUNING_URL, json={"bogus_field": 1})

        self.assertEqual(response.status_code, 422)
        self.assertIn("unknown fields", response.json()["detail"])
        self.assertEqual(self.runner.take_plan.fine_grasp_rpy_deg, (95, 0.0, 90.0))
        self.assertFalse((self.tuning_dir / "take_profile.json").exists())

        bad_gripper = self.client.put(
            "%s/return/profile" % TUNING_URL, json={"gripper": {"bogus": 1}}
        )
        self.assertEqual(bad_gripper.status_code, 422)
        self.assertFalse((self.tuning_dir / "return_profile.json").exists())

    def test_put_with_persist_false_applies_without_writing_the_file(self):
        response = self.client.put(
            "%s/take/profile?persist=false" % TUNING_URL,
            json={"fine_grasp_rpy_deg": [70.0, 0.0, 90.0]},
        )

        self.assertEqual(response.status_code, 200)
        # The live plan is hot-swapped immediately…
        self.assertEqual(self.runner.take_plan.fine_grasp_rpy_deg, (70.0, 0.0, 90.0))
        current = self.client.get("%s/take/profile" % TUNING_URL).json()
        self.assertEqual(current["fine_grasp_rpy_deg"], [70.0, 0.0, 90.0])
        # …but the profile file was neither written nor replaced.
        self.assertFalse((self.tuning_dir / "take_profile.json").exists())

    def test_put_with_persist_false_leaves_an_existing_file_untouched(self):
        persisted = self.client.put(
            "%s/take/profile" % TUNING_URL, json={"rod_grasp_deg": -60.0}
        )
        self.assertEqual(persisted.status_code, 200)
        profile_file = self.tuning_dir / "take_profile.json"
        self.assertTrue(profile_file.is_file())

        applied = self.client.put(
            "%s/take/profile?persist=false" % TUNING_URL, json={"rod_grasp_deg": -75.0}
        )

        self.assertEqual(applied.status_code, 200)
        self.assertEqual(self.runner.take_plan.rod_grasp_deg, -75.0)
        reloaded = load_take_book_motion_plan(profile_file)
        self.assertEqual(reloaded.rod_grasp_deg, -60.0)

    def test_save_profile_persists_the_hot_applied_live_plan(self):
        applied = self.client.put(
            "%s/take/profile?persist=false" % TUNING_URL,
            json={"gripper": {"effort": 0.9}},
        )
        self.assertEqual(applied.status_code, 200)
        self.assertFalse((self.tuning_dir / "take_profile.json").exists())

        saved = self.client.post("%s/take/profile/save" % TUNING_URL)

        self.assertEqual(saved.status_code, 200)
        profile_file = self.tuning_dir / "take_profile.json"
        self.assertTrue(profile_file.is_file())
        reloaded = load_take_book_motion_plan(profile_file)
        self.assertEqual(reloaded.gripper.effort, 0.9)

    def test_put_with_persist_false_rejects_invalid_and_changes_nothing(self):
        response = self.client.put(
            "%s/take/profile?persist=false" % TUNING_URL, json={"bogus_field": 1}
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.runner.take_plan.fine_grasp_rpy_deg, (95, 0.0, 90.0))
        self.assertFalse((self.tuning_dir / "take_profile.json").exists())

    def test_step_preview_reports_debug_joints_without_prepare(self):
        preview = self.client.get("%s/take/session/step/preview" % TUNING_URL).json()

        self.assertEqual(preview["step_index"], 0)
        self.assertEqual(preview["step_name"], "调试位")
        motion = preview["motion"]
        self.assertEqual(motion["type"], "joints")
        profile = self.client.get("%s/take/profile" % TUNING_URL).json()
        for actual, wanted_rad in zip(motion["joints_deg"], profile["debug_joint"]):
            self.assertAlmostEqual(actual, math.degrees(wanted_rad))

    def test_step_preview_hides_target_steps_until_prepared(self):
        # Enable first so the non-target steps can run without a prepare.
        enabled = self.client.post("%s/take/arm/enable" % TUNING_URL)
        self.assertEqual(enabled.status_code, 200)
        for _ in range(3):
            stepped = self.client.post("%s/take/session/step/next" % TUNING_URL)
            self.assertEqual(stepped.status_code, 200)
        preview = self.client.get("%s/take/session/step/preview" % TUNING_URL).json()

        self.assertEqual(preview["step_name"], "预抓取位")
        self.assertIsNone(preview["motion"])

    def test_arm_enable_recovers_after_gripper_dropout(self):
        first = self.client.post("%s/take/arm/enable" % TUNING_URL)
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()["was_enabled"])

        second = self.client.post("%s/take/arm/enable" % TUNING_URL)
        self.assertTrue(second.json()["was_enabled"])

        # 夹爪过流掉使能（臂体仍在 Motor 模式）：使能必须重跑完整序列恢复
        # 夹爪，而不是看到臂体已使能就空转返回。
        arm = self.tuning.take.session._devices.arm
        arm._gripper_ready = False
        third = self.client.post("%s/take/arm/enable" % TUNING_URL)
        self.assertEqual(third.status_code, 200)
        self.assertFalse(third.json()["was_enabled"])

        fourth = self.client.post("%s/take/arm/enable" % TUNING_URL)
        self.assertTrue(fourth.json()["was_enabled"])

    def test_step_preview_follows_hot_applied_plan_changes(self):
        self.prepare("take")
        for _ in range(3):
            self.client.post("%s/take/session/step/next" % TUNING_URL)
        before = self.client.get("%s/take/session/step/preview" % TUNING_URL).json()
        self.assertEqual(before["step_name"], "预抓取位")
        self.assertEqual(before["motion"]["type"], "tcp")
        old_offset = self.client.get("%s/take/profile" % TUNING_URL).json()[
            "pregrasp_offset_cm"
        ]

        applied = self.client.put(
            "%s/take/profile?persist=false" % TUNING_URL,
            json={"pregrasp_offset_cm": [old_offset[0] + 1.0, old_offset[1], old_offset[2]]},
        )
        self.assertEqual(applied.status_code, 200)

        after = self.client.get("%s/take/session/step/preview" % TUNING_URL).json()
        self.assertAlmostEqual(
            after["motion"]["x_mm"] - before["motion"]["x_mm"], 10.0, places=3
        )

    def test_step_preview_matches_what_run_step_actually_sends(self):
        self.prepare("take")
        count = len(self.client.get("%s/take/session" % TUNING_URL).json()["steps"])
        for index in range(count):
            preview = self.client.get("%s/take/session/step/preview" % TUNING_URL).json()
            self.assertEqual(preview["step_index"], index)
            ran = self.client.post(
                "%s/take/session/step/run" % TUNING_URL, json={"index": index}
            )
            self.assertEqual(ran.status_code, 200, "step %d failed" % index)
            detail = ran.json()["detail"]
            motion = preview["motion"]
            if "base_pose" in detail:
                self.assertIsNotNone(motion, "step %d lacks a preview" % index)
                self.assertEqual(motion["type"], "tcp")
                expected = detail["base_pose"]
                self.assertAlmostEqual(motion["x_mm"], expected[0] * 1000.0, places=3)
                self.assertAlmostEqual(motion["y_mm"], expected[1] * 1000.0, places=3)
                self.assertAlmostEqual(motion["z_mm"], expected[2] * 1000.0, places=3)
                self.assertAlmostEqual(motion["rx_deg"], math.degrees(expected[3]), places=3)
                self.assertAlmostEqual(motion["ry_deg"], math.degrees(expected[4]), places=3)
                self.assertAlmostEqual(motion["rz_deg"], math.degrees(expected[5]), places=3)
            elif motion is not None and motion["type"] == "joints":
                # Compound steps report the last motion in the run detail while
                # the preview shows the first; only same-kind data is compared.
                for key in ("debug_joint_rad", "zero_joint_rad"):
                    if key in detail:
                        for actual_deg, wanted_rad in zip(motion["joints_deg"], detail[key]):
                            self.assertAlmostEqual(actual_deg, math.degrees(wanted_rad), places=3)

    def test_step_preview_by_index_without_prepare_reports_reason(self):
        static = self.client.get(
            "%s/take/session/step/preview?step_index=0" % TUNING_URL
        ).json()
        self.assertEqual(static["step_name"], "调试位")
        self.assertEqual(static["motion"]["type"], "joints")
        self.assertIsNone(static["reason"])

        dependent = self.client.get(
            "%s/take/session/step/preview?step_index=3" % TUNING_URL
        ).json()
        self.assertEqual(dependent["step_name"], "预抓取位")
        self.assertIsNone(dependent["motion"])
        self.assertIn("准备", dependent["reason"])

        # A non-motion step reports neither motion nor reason.
        gripper = self.client.get(
            "%s/take/session/step/preview?step_index=1" % TUNING_URL
        ).json()
        self.assertIsNone(gripper["motion"])
        self.assertIsNone(gripper["reason"])

    def test_step_preview_index_out_of_range_is_422(self):
        for index in (99, -1):
            response = self.client.get(
                "%s/take/session/step/preview?step_index=%s" % (TUNING_URL, index)
            )
            self.assertEqual(response.status_code, 422)

    def test_run_step_by_index_executes_that_step_directly(self):
        self.prepare("take")
        preview = self.client.get(
            "%s/take/session/step/preview?step_index=3" % TUNING_URL
        ).json()

        ran = self.client.post(
            "%s/take/session/step/run" % TUNING_URL, json={"index": 3}
        )

        self.assertEqual(ran.status_code, 200)
        self.assertEqual(ran.json()["step_name"], "预抓取位")
        # The fake arm received exactly the pose the preview advertised.
        arm = self.tuning.book_stand._arm
        _method, target, _duration = arm.pose_moves[-1]
        motion = preview["motion"]
        self.assertAlmostEqual(target.x_m, motion["x_mm"] / 1000.0, places=6)
        self.assertAlmostEqual(target.z_m, motion["z_mm"] / 1000.0, places=6)

    def test_run_step_by_index_out_of_range_is_422(self):
        response = self.client.post(
            "%s/take/session/step/run" % TUNING_URL, json={"index": 99}
        )
        self.assertEqual(response.status_code, 422)

    def test_run_step_by_index_requires_prepare_for_target_steps(self):
        enabled = self.client.post("%s/take/arm/enable" % TUNING_URL)
        self.assertEqual(enabled.status_code, 200)

        response = self.client.post(
            "%s/take/session/step/run" % TUNING_URL, json={"index": 3}
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("准备", response.json()["detail"])

    def test_run_step_without_index_keeps_sequential_behaviour(self):
        enabled = self.client.post("%s/take/arm/enable" % TUNING_URL)
        self.assertEqual(enabled.status_code, 200)

        first = self.client.post("%s/take/session/step/run" % TUNING_URL, json={})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["step_index"], 1)
        second = self.client.post("%s/take/session/step/run" % TUNING_URL, json={})
        self.assertEqual(second.json()["step_index"], 2)

    def test_step_preview_all_covers_every_step_and_follows_hot_apply(self):
        view = self.client.get("%s/take/session/step/preview?all=1" % TUNING_URL).json()
        self.assertEqual(view["step_index"], 0)
        steps = view["steps"]
        self.assertEqual(len(steps), 16)
        self.assertEqual([entry["step_index"] for entry in steps], list(range(16)))
        self.assertEqual(steps[13]["step_name"], "松开拨杆")
        self.assertEqual(steps[0]["motion"]["type"], "joints")
        self.assertIsNone(steps[3]["motion"])
        self.assertIn("准备", steps[3]["reason"])

        self.prepare("take")
        baseline = self.client.get("%s/take/session/step/preview?all=1" % TUNING_URL).json()
        before_x = baseline["steps"][3]["motion"]["x_mm"]
        old_offset = self.client.get("%s/take/profile" % TUNING_URL).json()[
            "pregrasp_offset_cm"
        ]
        applied = self.client.put(
            "%s/take/profile?persist=false" % TUNING_URL,
            json={"pregrasp_offset_cm": [old_offset[0] + 1.0, old_offset[1], old_offset[2]]},
        )
        self.assertEqual(applied.status_code, 200)

        updated = self.client.get("%s/take/session/step/preview?all=1" % TUNING_URL).json()
        pregrasp = updated["steps"][3]["motion"]
        self.assertIsNotNone(pregrasp)
        self.assertEqual(pregrasp["type"], "tcp")
        self.assertAlmostEqual(pregrasp["x_mm"] - before_x, 10.0, places=3)
        # The single-step view agrees with the batched one.
        single = self.client.get(
            "%s/take/session/step/preview?step_index=3" % TUNING_URL
        ).json()
        self.assertAlmostEqual(single["motion"]["x_mm"], pregrasp["x_mm"])

    def test_midreturn_insert_preview_follows_the_retarget_step(self):
        self.prepare("midreturn")
        view = self.client.get(
            "%s/midreturn/session/step/preview?all=1" % TUNING_URL
        ).json()

        # The insert pose waits for the post-pickup retarget step.
        insert = view["steps"][25]
        self.assertEqual(insert["step_name"], "运动到插入位")
        self.assertIsNone(insert["motion"])
        self.assertIn("识别目标书", insert["reason"])
        # The book-slot step previews the selected pickup slot right away.
        slot_step = view["steps"][20]
        self.assertEqual(slot_step["step_name"], "运动到书槽(pre_grasp→grasp)")
        self.assertAlmostEqual(slot_step["motion"]["x_mm"], -300.0)

        # After the retarget the insert pose previews off the fresh target.
        ran = self.client.post(
            "%s/midreturn/session/step/run" % TUNING_URL, json={"index": 23}
        )
        self.assertEqual(ran.status_code, 200)
        view = self.client.get(
            "%s/midreturn/session/step/preview?all=1" % TUNING_URL
        ).json()
        insert = view["steps"][25]
        self.assertIsNotNone(insert["motion"])
        self.assertAlmostEqual(insert["motion"]["y_mm"], -30.0)

    def test_return_step_preview_matches_run_step_for_pickup_slots(self):
        self.prepare("return", slot=2)
        # Walk past 调试位 / 张开夹爪 / 识别目标书, then compare
        # 运动到书槽's preview with the run detail (the compound step drives
        # pre_grasp→grasp).
        for index in (0, 1, 2):
            self.client.post(
                "%s/return/session/step/run" % TUNING_URL, json={"index": index}
            )
        preview = self.client.get(
            "%s/return/session/step/preview" % TUNING_URL
        ).json()
        self.assertEqual(preview["step_index"], 3)
        self.assertEqual(preview["step_name"], "运动到书槽")
        ran = self.client.post(
            "%s/return/session/step/run" % TUNING_URL, json={"index": 3}
        )
        self.assertEqual(ran.status_code, 200)
        expected = ran.json()["detail"]["pre_grasp_pose"]
        motion = preview["motion"]
        self.assertEqual(motion["type"], "tcp")
        self.assertTrue(motion["continues"])
        self.assertAlmostEqual(motion["x_mm"], expected[0] * 1000.0, places=3)
        self.assertAlmostEqual(motion["z_mm"], expected[2] * 1000.0, places=3)
        # Slot 2: y = -0.05 - 0.06; the grasp follows through move_l.
        self.assertAlmostEqual(expected[1], -0.11)
        self.assertAlmostEqual(ran.json()["detail"]["grasp_pose"][1], -0.11)

    def test_book_stand_get_returns_both_groups(self):
        view = self.client.get("%s/book-stand/slots" % TUNING_URL).json()

        self.assertEqual(sorted(view["place"]["slots"].keys()), ["1", "2", "3", "4"])
        self.assertEqual(sorted(view["preplace"]["slots"].keys()), ["1", "2", "3"])
        pre_place = view["place"]["slots"]["1"]["pre_place"]
        self.assertEqual(len(pre_place), 6)
        self.assertAlmostEqual(pre_place[0], -0.30)

    def test_book_stand_put_persist_false_swaps_without_writing(self):
        document = self.client.get("%s/book-stand/slots" % TUNING_URL).json()["place"]
        document["slots"]["1"]["pre_place"][0] = -0.29

        response = self.client.put(
            "%s/book-stand/slots/place?persist=false" % TUNING_URL, json=document
        )

        self.assertEqual(response.status_code, 200)
        current = self.client.get("%s/book-stand/slots" % TUNING_URL).json()
        self.assertAlmostEqual(current["place"]["slots"]["1"]["pre_place"][0], -0.29)
        self.assertFalse((self.tuning_dir / "book_stand_slots.json").exists())

    def test_book_stand_put_persist_true_writes_the_file(self):
        document = self.client.get("%s/book-stand/slots" % TUNING_URL).json()["place"]
        document["slots"]["2"]["retreat_duration_s"] = 4.5

        response = self.client.put("%s/book-stand/slots/place" % TUNING_URL, json=document)

        self.assertEqual(response.status_code, 200)
        persisted = self.tuning_dir / "book_stand_slots.json"
        self.assertTrue(persisted.is_file())
        reloaded = load_book_stand_slots(persisted)
        self.assertEqual(reloaded[2].retreat_duration_s, 4.5)

    def test_book_stand_put_then_save_persists_live_values(self):
        document = self.client.get("%s/book-stand/slots" % TUNING_URL).json()["preplace"]
        document["slots"]["1"]["grasp"][2] = 0.19
        applied = self.client.put(
            "%s/book-stand/slots/preplace?persist=false" % TUNING_URL, json=document
        )
        self.assertEqual(applied.status_code, 200)
        self.assertFalse((self.tuning_dir / "book_stand_pickup_slots.json").exists())

        saved = self.client.post("%s/book-stand/slots/preplace/save" % TUNING_URL)

        self.assertEqual(saved.status_code, 200)
        reloaded = load_book_stand_pickup_slots(
            self.tuning_dir / "book_stand_pickup_slots.json"
        )
        self.assertAlmostEqual(reloaded[1].grasp.z_m, 0.19)

    def test_book_stand_put_rejects_unknown_slot_and_bad_pose(self):
        document = self.client.get("%s/book-stand/slots" % TUNING_URL).json()["place"]
        document["slots"]["9"] = document["slots"]["1"]

        unknown = self.client.put("%s/book-stand/slots/place" % TUNING_URL, json=document)
        self.assertEqual(unknown.status_code, 422)

        bad = self.client.get("%s/book-stand/slots" % TUNING_URL).json()["place"]
        bad["slots"]["1"]["place"] = [0.0] * 5
        invalid = self.client.put("%s/book-stand/slots/place" % TUNING_URL, json=bad)
        self.assertEqual(invalid.status_code, 422)

        current = self.client.get("%s/book-stand/slots" % TUNING_URL).json()
        self.assertAlmostEqual(current["place"]["slots"]["1"]["pre_place"][0], -0.30)
        self.assertFalse((self.tuning_dir / "book_stand_slots.json").exists())

    def test_book_stand_put_conflicts_with_a_busy_runner(self):
        document = self.client.get("%s/book-stand/slots" % TUNING_URL).json()["place"]
        self.runner.busy_lock.acquire()
        try:
            response = self.client.put(
                "%s/book-stand/slots/place" % TUNING_URL, json=document
            )
        finally:
            self.runner.busy_lock.release()

        self.assertEqual(response.status_code, 409)

    def test_book_stand_swap_refreshes_the_cached_step_preview(self):
        self.prepare("take", slot=1)
        # Walk to 书立预放置 (index 10) so the placement is cached.
        for _ in range(10):
            stepped = self.client.post("%s/take/session/step/next" % TUNING_URL)
            self.assertEqual(stepped.status_code, 200)
        before = self.client.get("%s/take/session/step/preview" % TUNING_URL).json()
        self.assertEqual(before["step_name"], "书立预放置(pre_place)")

        document = self.client.get("%s/book-stand/slots" % TUNING_URL).json()["place"]
        document["slots"]["1"]["pre_place"][0] += 0.01
        applied = self.client.put(
            "%s/book-stand/slots/place?persist=false" % TUNING_URL, json=document
        )
        self.assertEqual(applied.status_code, 200)

        after = self.client.get("%s/take/session/step/preview" % TUNING_URL).json()
        self.assertAlmostEqual(
            after["motion"]["x_mm"] - before["motion"]["x_mm"], 10.0, places=3
        )

    def _enable_fake_arm(self):
        enabled = self.client.post("%s/take/arm/enable" % TUNING_URL)
        self.assertEqual(enabled.status_code, 200)
        return self.tuning.book_stand._arm

    def test_book_stand_execute_requires_an_enabled_arm(self):
        response = self.client.post(
            "%s/book-stand/slots/place/execute" % TUNING_URL,
            json={"slot_id": "1", "action": "pre_place"},
        )

        self.assertEqual(response.status_code, 409)

    def test_book_stand_execute_sends_the_live_pose_with_flow_methods(self):
        arm = self._enable_fake_arm()

        pre_place = self.client.post(
            "%s/book-stand/slots/place/execute" % TUNING_URL,
            json={"slot_id": "1", "action": "pre_place"},
        )
        self.assertEqual(pre_place.status_code, 200)
        payload = pre_place.json()
        self.assertEqual(payload["method"], "move_p_by_j")
        self.assertEqual(payload["duration_s"], 3.0)
        method, target, duration = arm.pose_moves[-1]
        self.assertEqual(method, "move_p_by_j")
        self.assertAlmostEqual(target.x_m, -0.30)
        self.assertAlmostEqual(duration, 3.0)

        place = self.client.post(
            "%s/book-stand/slots/place/execute" % TUNING_URL,
            json={"slot_id": "2", "action": "place", "duration_s": 0.1},
        )
        self.assertEqual(place.status_code, 200)
        self.assertEqual(place.json()["method"], "move_l")
        # Duration is clamped into [0.5, 10].
        self.assertEqual(place.json()["duration_s"], 0.5)
        method, target, duration = arm.pose_moves[-1]
        self.assertEqual(method, "move_l")
        self.assertAlmostEqual(target.x_m, -0.34)
        self.assertAlmostEqual(duration, 0.5)

        grasp = self.client.post(
            "%s/book-stand/slots/preplace/execute" % TUNING_URL,
            json={"slot_id": "1", "action": "grasp", "duration_s": 12.0},
        )
        self.assertEqual(grasp.status_code, 200)
        self.assertEqual(grasp.json()["method"], "move_l")
        self.assertEqual(grasp.json()["duration_s"], 10.0)

    def test_book_stand_execute_uses_hot_applied_values(self):
        arm = self._enable_fake_arm()
        document = self.client.get("%s/book-stand/slots" % TUNING_URL).json()["place"]
        document["slots"]["1"]["pre_place"][0] = -0.25
        applied = self.client.put(
            "%s/book-stand/slots/place?persist=false" % TUNING_URL, json=document
        )
        self.assertEqual(applied.status_code, 200)

        executed = self.client.post(
            "%s/book-stand/slots/place/execute" % TUNING_URL,
            json={"slot_id": "1", "action": "pre_place"},
        )

        self.assertEqual(executed.status_code, 200)
        _method, target, _duration = arm.pose_moves[-1]
        self.assertAlmostEqual(target.x_m, -0.25)

    def test_step_run_maps_parameter_range_errors_to_422(self):
        self._enable_fake_arm()
        rod = self.tuning.take.session._devices.rod
        arm = self.tuning.book_stand._arm

        def bad_rod(*args, **kwargs):
            raise ValueError(
                "rod target angle must be between -100.0 and 90.0 deg; got -120.0"
            )

        rod.move_to_deg = bad_rod

        response = self.client.post(
            "%s/take/session/step/run" % TUNING_URL, json={"index": 2}
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("between -100.0 and 90.0", response.json()["detail"])
        self.assertIn("参数范围", response.json()["detail"])
        # The parameter rejection happened before any arm motion was sent.
        self.assertEqual(arm.pose_moves, [])

    def test_step_run_maps_kinematics_errors_to_422(self):
        self._enable_fake_arm()
        self.prepare("take")
        arm = self.tuning.book_stand._arm

        def unreachable(target, duration_s):
            raise RuntimeError(
                "MovePByJ inverse kinematics failed for target %s: "
                "arm inverse kinematics did not converge" % (target.values(),)
            )

        arm.move_to_pose_by_move_j = unreachable

        response = self.client.post(
            "%s/take/session/step/run" % TUNING_URL, json={"index": 3}
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("inverse kinematics", response.json()["detail"])
        self.assertIn("不可达", response.json()["detail"])

    def test_step_run_keeps_hardware_runtime_errors_at_503(self):
        self._enable_fake_arm()
        self.prepare("take")
        arm = self.tuning.book_stand._arm

        def fault(target, duration_s):
            raise RuntimeError("arm fault code 0x21: motor 3 offline")

        arm.move_to_pose_by_move_j = fault

        response = self.client.post(
            "%s/take/session/step/run" % TUNING_URL, json={"index": 3}
        )

        self.assertEqual(response.status_code, 503)
        self.assertIn("硬件状态不可用", response.json()["detail"])

    def test_profile_update_rejects_out_of_range_rod_angles(self):
        bad_take = self.client.put("%s/take/profile" % TUNING_URL, json={"rod_grasp_deg": -120.0})
        self.assertEqual(bad_take.status_code, 422)
        self.assertIn("between -100.0 and 150.0", bad_take.json()["detail"])
        self.assertEqual(self.runner.take_plan.rod_grasp_deg, -70.0)
        self.assertFalse((self.tuning_dir / "take_profile.json").exists())

        bad_home = self.client.put("%s/take/profile" % TUNING_URL, json={"rod_home_deg": 160.0})
        self.assertEqual(bad_home.status_code, 422)
        self.assertIn("rod_home_deg", bad_home.json()["detail"])

        bad_mid_home = self.client.put(
            "%s/midreturn/profile" % TUNING_URL, json={"rod_home_deg": -120.0}
        )
        self.assertEqual(bad_mid_home.status_code, 422)
        self.assertIn("rod_home_deg", bad_mid_home.json()["detail"])

        bad_return = self.client.put(
            "%s/return/profile" % TUNING_URL, json={"rod_prepare_deg": 160.0}
        )
        self.assertEqual(bad_return.status_code, 422)
        self.assertIn("rod_prepare_deg", bad_return.json()["detail"])

        bad_mid = self.client.put(
            "%s/midreturn/profile" % TUNING_URL, json={"rod_grasp_deg": 160.0}
        )
        self.assertEqual(bad_mid.status_code, 422)
        self.assertIn("rod_grasp_deg", bad_mid.json()["detail"])

        # The device limits are inclusive.
        ok = self.client.put("%s/take/profile" % TUNING_URL, json={"rod_grasp_deg": -100.0})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.runner.take_plan.rod_grasp_deg, -100.0)

    def test_rod_home_deg_hot_swap_reaches_the_rod_home_step(self):
        self.prepare("take")
        applied = self.client.put(
            "%s/take/profile?persist=false" % TUNING_URL, json={"rod_home_deg": 12.0}
        )
        self.assertEqual(applied.status_code, 200)

        # 推杆归零 is step index 2 in the take session (调试位, 打开夹爪, 推杆归零).
        ran = self.client.post("%s/take/session/step/run" % TUNING_URL, json={"index": 2})
        self.assertEqual(ran.status_code, 200)
        self.assertEqual(ran.json()["step_name"], "推杆归零")
        self.assertEqual(ran.json()["detail"]["rod_deg"], 12.0)

    def test_midreturn_rod_home_deg_hot_swap_reaches_the_rod_home_step(self):
        self.prepare("midreturn")
        applied = self.client.put(
            "%s/midreturn/profile?persist=false" % TUNING_URL, json={"rod_home_deg": 8.0}
        )
        self.assertEqual(applied.status_code, 200)

        # 推杆归零 is step index 1 in the mid-return session.
        ran = self.client.post(
            "%s/midreturn/session/step/run" % TUNING_URL, json={"index": 1}
        )
        self.assertEqual(ran.status_code, 200)
        self.assertEqual(ran.json()["step_name"], "推杆归零")
        self.assertEqual(ran.json()["detail"]["rod_deg"], 8.0)

    def test_book_stand_execute_maps_kinematics_errors_to_422(self):
        arm = self._enable_fake_arm()

        def unreachable(target, duration_s):
            raise RuntimeError("arm inverse kinematics did not converge")

        arm.move_to_pose_by_move_j = unreachable

        response = self.client.post(
            "%s/book-stand/slots/place/execute" % TUNING_URL,
            json={"slot_id": "1", "action": "pre_place"},
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("不可达", response.json()["detail"])

    def test_book_stand_execute_keeps_hardware_errors_at_503(self):
        arm = self._enable_fake_arm()

        def can_error(target, duration_s):
            raise OSError("socketcan send failed: no buffer space")

        arm.move_to_pose_by_move_j = can_error

        response = self.client.post(
            "%s/book-stand/slots/place/execute" % TUNING_URL,
            json={"slot_id": "1", "action": "pre_place"},
        )

        self.assertEqual(response.status_code, 503)
        self.assertIn("硬件状态不可用", response.json()["detail"])

    def test_book_stand_execute_rejects_unknown_slot_and_action(self):
        self._enable_fake_arm()

        unknown_slot = self.client.post(
            "%s/book-stand/slots/place/execute" % TUNING_URL,
            json={"slot_id": "9", "action": "pre_place"},
        )
        self.assertEqual(unknown_slot.status_code, 404)

        unknown_action = self.client.post(
            "%s/book-stand/slots/place/execute" % TUNING_URL,
            json={"slot_id": "1", "action": "bogus"},
        )
        self.assertEqual(unknown_action.status_code, 422)

        # Duration-only rows (e.g. preplace retreat) are not executable.
        duration_only = self.client.post(
            "%s/book-stand/slots/preplace/execute" % TUNING_URL,
            json={"slot_id": "1", "action": "retreat"},
        )
        self.assertEqual(duration_only.status_code, 422)

    def test_book_stand_execute_conflicts_with_a_busy_runner(self):
        self._enable_fake_arm()
        self.runner.busy_lock.acquire()
        try:
            response = self.client.post(
                "%s/book-stand/slots/place/execute" % TUNING_URL,
                json={"slot_id": "1", "action": "pre_place"},
            )
        finally:
            self.runner.busy_lock.release()

        self.assertEqual(response.status_code, 409)

    def test_take_session_walks_every_step(self):
        session = self.client.get("%s/take/session" % TUNING_URL).json()
        self.assertEqual(session["step_index"], 0)
        self.assertFalse(session["has_target"])
        self.assertIsNone(session["slot"])
        self.assertEqual(len(session["steps"]), 16)
        self.assertEqual(session["steps"][0], "调试位")
        self.assertEqual(session["steps"][-1], "待机位(zero)")

        # Target-dependent steps require a prepared session first.
        unprepared = self.client.post(
            "%s/take/session/step/run" % TUNING_URL, json={"index": 3}
        )
        self.assertEqual(unprepared.status_code, 409)
        self.assertIn("请先准备", unprepared.json()["detail"])

        prepared = self.prepare("take", slot=1)
        self.assertTrue(prepared["ok"])
        self.assertEqual(len(prepared["target_base_pose"]), 6)
        self.assertGreater(prepared["confidence"], 0.0)

        session = self.client.get("%s/take/session" % TUNING_URL).json()
        self.assertTrue(session["has_target"])
        self.assertEqual(session["slot"], 1)

        for expected_index in range(1, 17):
            step = self.client.post("%s/take/session/step/next" % TUNING_URL)
            self.assertEqual(step.status_code, 200)
            self.assertTrue(step.json()["ok"])
            self.assertEqual(step.json()["step_index"], expected_index)
        finished = self.client.post("%s/take/session/step/next" % TUNING_URL)
        self.assertEqual(finished.status_code, 200)
        self.assertIsNone(finished.json()["step_name"])
        self.assertTrue(finished.json()["detail"]["finished"])

        # previous re-executes the last step (motions are absolute, idempotent).
        previous = self.client.post("%s/take/session/step/previous" % TUNING_URL)
        self.assertEqual(previous.status_code, 200)
        self.assertEqual(previous.json()["step_name"], "待机位(zero)")
        self.assertEqual(previous.json()["step_index"], 16)

        # run executes one arbitrary step for single-step verification.
        ran = self.client.post("%s/take/session/step/run" % TUNING_URL, json={"index": 0})
        self.assertEqual(ran.status_code, 200)
        self.assertEqual(ran.json()["step_name"], "调试位")
        self.assertEqual(ran.json()["step_index"], 1)
        out_of_range = self.client.post(
            "%s/take/session/step/run" % TUNING_URL, json={"index": 99}
        )
        self.assertEqual(out_of_range.status_code, 422)

        # reset rewinds the cursor but keeps the cached target.
        reset = self.client.post("%s/take/session/reset" % TUNING_URL)
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reset.json()["step_index"], 0)
        session = self.client.get("%s/take/session" % TUNING_URL).json()
        self.assertTrue(session["has_target"])

    def test_return_session_walks_every_step(self):
        # 识别目标书 (index 2) needs the prepared template first.
        unprepared = self.client.post(
            "%s/return/session/step/run" % TUNING_URL, json={"index": 2}
        )
        self.assertEqual(unprepared.status_code, 409)

        session = self.client.get("%s/return/session" % TUNING_URL).json()
        self.assertEqual(len(session["steps"]), 12)
        self.assertEqual(
            session["steps"],
            [
                "调试位",
                "张开夹爪",
                "识别目标书",
                "运动到书槽",
                "闭合夹爪",
                "回调试位",
                "预插入位",
                "运动到插入位",
                "推回书架",
                "张开夹爪",
                "回抽",
                "回调试位",
            ],
        )

        # Return-book only has calibrated pickup slots 1--3.
        bad_slot = self.client.post(
            "%s/return/session/prepare" % TUNING_URL,
            json={"slot": 4, "template_id": self.library_id()},
        )
        self.assertEqual(bad_slot.status_code, 422)

        prepared = self.prepare("return", slot=2)
        self.assertTrue(prepared["ok"])
        details = []
        for expected_index in range(1, 13):
            step = self.client.post("%s/return/session/step/next" % TUNING_URL)
            self.assertEqual(step.status_code, 200)
            self.assertEqual(step.json()["step_index"], expected_index)
            details.append(step.json()["detail"])
        finished = self.client.post("%s/return/session/step/next" % TUNING_URL)
        self.assertTrue(finished.json()["detail"]["finished"])

        # Synchronous re-locate at the debug pose, then the slot pickup:
        # open 50°, pre_grasp→grasp, close, retreat to debug.
        self.assertEqual(details[1]["open_deg"], 50.0)
        self.assertEqual(len(details[2]["target_base_pose"]), 6)
        self.assertAlmostEqual(details[2]["target_base_pose"][1], -0.07)
        self.assertAlmostEqual(details[3]["pre_grasp_pose"][0], -0.30)
        self.assertAlmostEqual(details[3]["grasp_pose"][0], -0.34)
        self.assertIn("gripper_close", details[4])
        self.assertEqual(len(details[5]["debug_joint_rad"]), 6)
        # The insert chain anchors on the cached recognition: y = -0.07 +
        # 4 cm = -0.03, z (0.23) is clamped up to the 0.25 m reachability
        # floor, and the pre-insert x is IK-searched (the fake arm reaches
        # everything, so the plan's -0.20 m cap itself wins).
        self.assertAlmostEqual(details[6]["base_pose"][0], -0.20)
        self.assertAlmostEqual(details[6]["base_pose"][1], -0.03)
        self.assertAlmostEqual(details[6]["base_pose"][2], 0.25)
        self.assertAlmostEqual(details[7]["base_pose"][0], -0.35)
        self.assertAlmostEqual(details[7]["base_pose"][1], -0.03)
        self.assertAlmostEqual(details[7]["base_pose"][2], 0.25)
        self.assertAlmostEqual(details[8]["base_pose"][0], -0.40)
        self.assertEqual(details[9]["open_deg"], 50.0)
        self.assertAlmostEqual(details[10]["base_pose"][0], -0.30)
        self.assertEqual(len(details[11]["debug_joint_rad"]), 6)

        # The retarget step re-runs vision against the cached template.
        relocate = self.client.post(
            "%s/return/session/step/run" % TUNING_URL, json={"index": 2}
        )
        self.assertEqual(relocate.status_code, 200)
        self.assertEqual(relocate.json()["step_name"], "识别目标书")
        self.assertEqual(len(relocate.json()["detail"]["target_base_pose"]), 6)

        # reset rewinds the cursor but keeps the cached recognition, so the
        # insert chain still resolves (same semantics as midreturn).
        reset = self.client.post("%s/return/session/reset" % TUNING_URL)
        self.assertEqual(reset.status_code, 200)
        insert = self.client.post(
            "%s/return/session/step/run" % TUNING_URL, json={"index": 7}
        )
        self.assertEqual(insert.status_code, 200)
        self.assertAlmostEqual(insert.json()["detail"]["base_pose"][0], -0.35)

    def test_return_insert_steps_need_the_retarget_step(self):
        # Without 识别目标书 the insert chain explains what to run first.
        self.prepare("return", slot=1)
        preview = self.client.get(
            "%s/return/session/step/preview?step_index=6" % TUNING_URL
        ).json()
        self.assertIsNone(preview["motion"])
        self.assertIn("识别目标书", preview["reason"])
        blocked = self.client.post(
            "%s/return/session/step/run" % TUNING_URL, json={"index": 6}
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("识别目标书", blocked.json()["detail"])

        # Step 2 is synchronous: it returns the located target directly.
        retarget = self.client.post(
            "%s/return/session/step/run" % TUNING_URL, json={"index": 2}
        )
        self.assertEqual(retarget.status_code, 200)
        self.assertEqual(retarget.json()["step_name"], "识别目标书")
        self.assertEqual(len(retarget.json()["detail"]["target_base_pose"]), 6)

        # Insert steps compute from the cache; the dynamic-x search routes
        # off the arm worker (one probe per pose computation).
        arm = self.tuning.return_book.session._devices.arm
        self.assertEqual(arm.ik_probes, [])
        preview = self.client.get(
            "%s/return/session/step/preview?step_index=7" % TUNING_URL
        ).json()
        self.assertIsNotNone(preview["motion"])
        self.assertAlmostEqual(preview["motion"]["y_mm"], -30.0)
        self.assertAlmostEqual(preview["motion"]["z_mm"], 250.0)
        ran = self.client.post(
            "%s/return/session/step/run" % TUNING_URL, json={"index": 6}
        )
        self.assertEqual(ran.status_code, 200)
        self.assertEqual(ran.json()["step_name"], "预插入位")
        self.assertAlmostEqual(ran.json()["detail"]["base_pose"][0], -0.20)
        self.assertEqual(arm.ik_probes, ["pre_insert_x_search", "pre_insert_x_search"])

    def test_return_insert_steps_follow_a_profile_hot_swap_after_the_retarget(self):
        self.prepare("return", slot=1)
        retarget = self.client.post(
            "%s/return/session/step/run" % TUNING_URL, json={"index": 2}
        )
        self.assertEqual(retarget.status_code, 200)
        # The chain computes lazily from the cached recognition: y = -0.03 m.
        before = self.client.get(
            "%s/return/session/step/preview?step_index=7" % TUNING_URL
        ).json()
        self.assertAlmostEqual(before["motion"]["y_mm"], -30.0)

        applied = self.client.put(
            "%s/return/profile?persist=false" % TUNING_URL,
            json={"target_y_offset_cm": 8.0},
        )
        self.assertEqual(applied.status_code, 200)

        # No invalidation needed: the next preview recomputes from the cached
        # target with the live plan (y = -0.07 + 8 cm = 0.01 m).
        after = self.client.get(
            "%s/return/session/step/preview?step_index=7" % TUNING_URL
        ).json()
        self.assertIsNotNone(after["motion"])
        self.assertAlmostEqual(after["motion"]["y_mm"], 10.0)

    def test_return_retarget_step_reports_a_failed_recognition(self):
        self.prepare("return", slot=1)
        vision = self.tuning.return_book.session._devices.vision

        def failed_locate(template):
            raise RuntimeError("simulated spine detection failure")

        vision.locate_book = failed_locate
        ran = self.client.post(
            "%s/return/session/step/run" % TUNING_URL, json={"index": 2}
        )
        # Same mapping as a mid-return retarget vision failure.
        self.assertEqual(ran.status_code, 503)
        self.assertIn("simulated spine detection failure", ran.json()["detail"])

    def test_prepare_resolves_templates_through_the_studio_library(self):
        missing = self.client.post(
            "%s/take/session/prepare" % TUNING_URL,
            json={"slot": 1, "template_id": "no-such-image"},
        )
        self.assertEqual(missing.status_code, 422)
        self.assertIn("unknown template_id", missing.json()["detail"])

        # Without a template_id the newest library image is used; an empty
        # library is a 422 instead of a guessing vision run.
        empty = self.client.post("%s/take/session/prepare" % TUNING_URL, json={"slot": 1})
        self.assertEqual(empty.status_code, 422)

        self.library_id()
        latest = self.client.post("%s/take/session/prepare" % TUNING_URL, json={"slot": 1})
        self.assertEqual(latest.status_code, 200)
        self.assertTrue(latest.json()["ok"])

        # The return session resolves templates the same way (it locates the
        # reference book with locate_book, like take and midreturn).
        returned = self.client.post("%s/return/session/prepare" % TUNING_URL, json={"slot": 1})
        self.assertEqual(returned.status_code, 200)
        self.assertTrue(returned.json()["ok"])
        missing_return = self.client.post(
            "%s/return/session/prepare" % TUNING_URL,
            json={"slot": 1, "template_id": "no-such-image"},
        )
        self.assertEqual(missing_return.status_code, 422)
        self.assertIn("unknown template_id", missing_return.json()["detail"])

    def test_session_actions_conflict_with_a_busy_runner(self):
        template_id = self.library_id()
        # The run-step path now preempts: cancel any in-flight workflow (none
        # here) and wait on the lock — shrink that wait so the test stays fast.
        self.tuning.take.session._run_preempt_timeout_s = 0.05
        self.runner.busy_lock.acquire()
        try:
            self.assertEqual(
                self.client.post(
                    "%s/take/session/prepare" % TUNING_URL,
                    json={"slot": 1, "template_id": template_id},
                ).status_code,
                409,
            )
            self.assertEqual(
                self.client.post("%s/take/session/step/next" % TUNING_URL).status_code, 409
            )
            self.assertEqual(
                self.client.post("%s/take/arm/enable" % TUNING_URL).status_code, 409
            )
            self.assertEqual(
                self.client.post("%s/return/arm/debug-pose" % TUNING_URL).status_code, 409
            )
        finally:
            self.runner.busy_lock.release()

    def test_arm_endpoints_enable_debug_and_disable(self):
        enabled = self.client.post("%s/take/arm/enable" % TUNING_URL)
        self.assertEqual(enabled.status_code, 200)
        self.assertTrue(enabled.json()["ok"])
        self.assertTrue(enabled.json()["enabled"])

        debug = self.client.post("%s/take/arm/debug-pose" % TUNING_URL)
        self.assertEqual(debug.status_code, 200)
        self.assertTrue(debug.json()["ok"])
        self.assertEqual(len(debug.json()["joints_rad"]), 6)

        disabled = self.client.post("%s/take/arm/disable" % TUNING_URL)
        self.assertEqual(disabled.status_code, 200)
        self.assertTrue(disabled.json()["ok"])
        self.assertFalse(disabled.json()["enabled"])

        self.assertEqual(self.client.post("%s/return/arm/enable" % TUNING_URL).status_code, 200)
        self.assertEqual(
            self.client.post("%s/return/arm/disable" % TUNING_URL).status_code, 200
        )

    def test_hardware_transport_error_is_reported_as_service_unavailable(self):
        class BrokenArm:
            def is_enabled(self):
                raise OSError(6, "No such device or address")

        # Replace only the test session's arm; no hardware-backed component is
        # constructed and the route should translate the device error itself.
        self.tuning.take.session._devices = TuningDevices(
            arm=BrokenArm(),
            rod=self.tuning.take.session._devices.rod,
            vision=self.tuning.take.session._devices.vision,
        )

        response = self.client.post("%s/take/arm/enable" % TUNING_URL)

        self.assertEqual(response.status_code, 503)
        self.assertIn("硬件状态不可用", response.json()["detail"])
        self.assertIn("can0", response.json()["detail"])

    def test_hardware_feedback_error_is_reported_as_service_unavailable(self):
        class BrokenArm:
            def is_enabled(self):
                raise RuntimeError("joint 5 reports fault code 16")

        self.tuning.take.session._devices = TuningDevices(
            arm=BrokenArm(),
            rod=self.tuning.take.session._devices.rod,
            vision=self.tuning.take.session._devices.vision,
        )

        response = self.client.post("%s/take/arm/enable" % TUNING_URL)

        self.assertEqual(response.status_code, 503)
        self.assertIn("硬件状态不可用", response.json()["detail"])
        self.assertIn("fault code 16", response.json()["detail"])

    def test_persisted_return_profile_loads_with_the_stock_loader(self):
        response = self.client.put(
            "%s/return/profile" % TUNING_URL,
            json={"release_open_deg": 75.0, "target_y_offset_cm": 6.0},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.runner.return_plan.release_open_deg, 75.0)
        self.assertEqual(self.runner.return_plan.target_y_offset_cm, 6.0)
        persisted = self.tuning_dir / "return_profile.json"
        self.assertTrue(persisted.is_file())
        reloaded = load_return_book_motion_plan(persisted)
        self.assertEqual(reloaded.release_open_deg, 75.0)
        self.assertEqual(reloaded.target_y_offset_cm, 6.0)
        self.assertEqual(reloaded.insert_rpy_deg, (95.0, 0.0, 90.0))

    def test_return_profile_accepts_push_rpy_deg(self):
        response = self.client.put(
            "%s/return/profile" % TUNING_URL,
            json={"push_rpy_deg": [5.0, -10.0, 2.0]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.runner.return_plan.push_rpy_deg, (5.0, -10.0, 2.0))
        reloaded = load_return_book_motion_plan(self.tuning_dir / "return_profile.json")
        self.assertEqual(reloaded.push_rpy_deg, (5.0, -10.0, 2.0))

    def test_profile_reports_the_standard_level_by_default(self):
        profile = self.client.get("%s/take/profile" % TUNING_URL).json()
        self.assertEqual(profile["speed_level"], "standard")
        self.assertEqual(profile["zero_duration_s"], 3.0)

    def test_put_speed_level_fast_scales_the_live_plan_and_persists_base(self):
        response = self.client.put("%s/take/profile" % TUNING_URL, json={"speed_level": "fast"})

        self.assertEqual(response.status_code, 200)
        # The profile document keeps the tuned base values plus the level…
        profile = self.client.get("%s/take/profile" % TUNING_URL).json()
        self.assertEqual(profile["speed_level"], "fast")
        self.assertEqual(profile["zero_duration_s"], 3.0)
        self.assertEqual(profile["approach_duration_s"], 2.0)
        self.assertEqual(profile["rod_speed"], 1000)
        self.assertEqual(profile["gripper"]["close_speed_deg_s"], 16.7)
        # …while the hot-swapped live plan runs the level-adjusted values.
        live = self.runner.take_plan
        self.assertEqual(live.speed_level, "fast")
        self.assertEqual(live.zero_duration_s, 1.8)
        self.assertEqual(live.approach_duration_s, 1.2)
        self.assertEqual(live.rod_speed, 1800)
        self.assertEqual(live.gripper.close_speed_deg_s, 60.12)
        self.assertEqual(live.gripper.open_speed_deg_s, 60.12)
        self.assertEqual(live.gripper.command_lead_s, 0.15)
        self.assertEqual(live.gripper.stall_time_s, 0.51)
        self.assertEqual(live.gripper.contact_hold_duration_s, 0.6)
        # Grasp-safety constants stay at the calibrated values.
        self.assertEqual(live.gripper.min_monitor_s, 0.4)
        self.assertEqual(live.gripper.effort, 0.5)
        # The persisted file holds the base document; the stock loader
        # reproduces the same effective plan at startup.
        persisted = self.tuning_dir / "take_profile.json"
        self.assertTrue(persisted.is_file())
        reloaded = load_take_book_motion_plan(persisted)
        self.assertEqual(reloaded, live)

    def test_put_speed_level_then_another_patch_never_scales_twice(self):
        first = self.client.put("%s/take/profile" % TUNING_URL, json={"speed_level": "fast"})
        self.assertEqual(first.status_code, 200)

        second = self.client.put(
            "%s/take/profile" % TUNING_URL, json={"approach_offset_cm": [1.0, 0.0, 0.0]}
        )

        self.assertEqual(second.status_code, 200)
        live = self.runner.take_plan
        self.assertEqual(live.pregrasp_duration_s, 1.8)
        self.assertEqual(live.rod_speed, 1800)
        profile = self.client.get("%s/take/profile" % TUNING_URL).json()
        self.assertEqual(profile["speed_level"], "fast")
        self.assertEqual(profile["pregrasp_duration_s"], 3.0)
        self.assertEqual(profile["approach_offset_cm"], [1.0, 0.0, 0.0])

    def test_put_invalid_speed_level_is_422_and_changes_nothing(self):
        response = self.client.put("%s/take/profile" % TUNING_URL, json={"speed_level": "turbo"})

        self.assertEqual(response.status_code, 422)
        self.assertIn("speed_level", response.json()["detail"])
        self.assertEqual(self.runner.take_plan.speed_level, "standard")
        self.assertEqual(self.runner.take_plan.zero_duration_s, 3.0)
        self.assertFalse((self.tuning_dir / "take_profile.json").exists())

    def test_speed_level_back_to_standard_restores_the_legacy_plan(self):
        self.client.put("%s/take/profile" % TUNING_URL, json={"speed_level": "fast"})
        response = self.client.put("%s/take/profile" % TUNING_URL, json={"speed_level": "standard"})

        self.assertEqual(response.status_code, 200)
        live = self.runner.take_plan
        self.assertEqual(live.speed_level, "standard")
        self.assertEqual(live.zero_duration_s, 3.0)
        self.assertEqual(live.rod_speed, 1000)
        self.assertEqual(live.gripper.close_speed_deg_s, 16.7)
        self.assertEqual(live.gripper.command_lead_s, 0.25)

    def test_speed_level_applies_to_return_and_midreturn_kinds(self):
        response = self.client.put("%s/return/profile" % TUNING_URL, json={"speed_level": "slow"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.runner.return_plan.pre_insert_duration_s, 4.5)
        self.assertEqual(self.runner.return_plan.insert_duration_s, 4.5)
        self.assertEqual(self.runner.return_plan.push_duration_s, 3.0)
        self.assertEqual(self.runner.return_plan.retract_duration_s, 3.0)
        self.assertEqual(self.runner.return_plan.gripper.close_speed_deg_s, 11.69)

        response = self.client.put(
            "%s/midreturn/profile" % TUNING_URL, json={"speed_level": "fast"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.runner.mid_return_plan.insert_duration_s, 1.8)
        self.assertEqual(self.runner.mid_return_plan.push_duration_s, 1.2)
        self.assertEqual(self.runner.mid_return_plan.rod_speed, 1800)

        bad = self.client.put("%s/return/profile" % TUNING_URL, json={"speed_level": "max"})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(self.runner.return_plan.speed_level, "slow")

    def test_step_preview_uses_the_scaled_effective_durations(self):
        before = self.client.get("%s/take/session/step/preview?step_index=0" % TUNING_URL)
        self.assertEqual(before.json()["motion"]["duration_s"], 3.0)

        self.client.put("%s/take/profile" % TUNING_URL, json={"speed_level": "fast"})

        after = self.client.get("%s/take/session/step/preview?step_index=0" % TUNING_URL)
        self.assertEqual(after.status_code, 200)
        self.assertEqual(after.json()["motion"]["duration_s"], 1.8)


class TuningRunPreemptTests(unittest.TestCase):
    """运行 steps preempt a busy runner: cancel the workflow, wait, then run."""

    def setUp(self):
        self._dirs = tempfile.TemporaryDirectory()
        self.addCleanup(self._dirs.cleanup)
        root = Path(self._dirs.name)
        # A long stage delay keeps the fake workflow parked inside lift_move
        # (holding the busy lock) until its cancellation lands.
        self.runner = FakeWorkflowRunner(stage_delay_s=30.0)
        self.addCleanup(self.runner.shutdown)
        self.studio = FakeStudioBackend(
            StudioStore(root / "studio"), busy_lock=self.runner.busy_lock
        )
        self.tuning = create_fake_tuning(self.runner, self.studio, store_dir=root / "tuning")
        self.client = TestClient(
            create_app(
                self.runner,
                upload_dir=root / "uploads",
                studio=self.studio,
                tuning=self.tuning,
            )
        )

    def run_rod_step(self):
        # 推杆归零 (take session, index 2) touches only the fake rod: no arm
        # enable and no prepare needed.
        return self.client.post("%s/take/session/step/run" % TUNING_URL, json={"index": 2})

    def test_run_step_cancels_the_in_flight_workflow_then_runs(self):
        request = TakeBookRequest(
            book=BookIdentity("preempt-book"),
            target_height_mm=300.0,
            spine_template=SpineTemplate(Path("book.png")),
            destination_slot=BookStandSlot.SLOT_1,
            stop_after_stage=TakeBookStage.ARM_STANDBY,
        )
        task = self.runner.submit_take(request)

        response = self.run_rod_step()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["step_name"], "推杆归零")
        self.assertEqual(response.json()["detail"]["rod_deg"], 0.0)
        # The preempted workflow was cancelled at the next stage boundary; its
        # busy-lock release is what let the tuning step acquire and run.
        cancelled = self.runner.status(task.id.value)
        self.assertEqual(cancelled.state, TaskState.CANCELLED)
        self.assertEqual([stage.name for stage in cancelled.stages], ["lift_move"])

    def test_run_step_waits_out_a_short_action_holding_the_lock(self):
        # No workflow task is active: the preempt cancel is a no-op and the
        # bounded wait simply covers the seconds-long tuning motion ahead.
        self.runner.busy_lock.acquire()

        def release_later():
            time.sleep(0.3)
            self.runner.busy_lock.release()

        releaser = threading.Thread(target=release_later, daemon=True)
        started = time.monotonic()
        releaser.start()
        try:
            response = self.run_rod_step()
        finally:
            releaser.join(timeout=5.0)
        elapsed = time.monotonic() - started

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["step_name"], "推杆归零")
        self.assertGreaterEqual(elapsed, 0.2)

    def test_run_step_still_busy_after_the_timeout_reports_busy(self):
        self.tuning.take.session._run_preempt_timeout_s = 0.05
        self.runner.busy_lock.acquire()
        try:
            response = self.run_rod_step()
        finally:
            self.runner.busy_lock.release()

        self.assertEqual(response.status_code, 409)
        self.assertIn("already running", response.json()["detail"])

    def test_prepare_keeps_fail_fast_busy_behaviour(self):
        # Only the run-step paths preempt; prepare still fails fast on 409.
        template_id = self.studio.import_library_image(
            np.full((12, 8, 3), 180, np.uint8), source="test"
        ).id
        self.runner.busy_lock.acquire()
        try:
            response = self.client.post(
                "%s/take/session/prepare" % TUNING_URL,
                json={"slot": 1, "template_id": template_id},
            )
        finally:
            self.runner.busy_lock.release()

        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
