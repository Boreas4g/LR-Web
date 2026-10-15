"""HTTP spine-studio service tests with the hardware-free backends."""

from __future__ import annotations

import json
import math
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
import unittest
from unittest import mock

from fastapi.testclient import TestClient
import numpy as np

from bookarm.devices.camera.transform import CameraToBaseTransform
from bookarm.domain.models.pose import BasePose, JointPose
from bookarm.service import studio as studio_module
from bookarm.service.app import create_app
from bookarm.service.runner import FakeWorkflowRunner
from bookarm.service.studio import (
    FakeStudioBackend,
    RealStudioBackend,
    SegmentationUnavailableError,
    StudioStore,
)


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
STUDIO_URL = "/api/v1/studio"
TAKEOUT_TASKS_URL = "/api/v1/takeout/tasks"
RETURN_TASKS_URL = "/api/v1/return/tasks"


def poll_terminal(client: TestClient, task_id: str, timeout_s: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        view = client.get("%s/%s" % (TAKEOUT_TASKS_URL, task_id)).json()
        if view["state"] in {"succeeded", "failed", "cancelled", "unsupported", "recovery_required"}:
            return view
        time.sleep(0.01)
    raise AssertionError("task %s did not reach a terminal state" % task_id)


class StudioServiceTests(unittest.TestCase):
    def setUp(self):
        self._dirs = tempfile.TemporaryDirectory()
        self.addCleanup(self._dirs.cleanup)
        root = Path(self._dirs.name)
        self.upload_dir = root / "uploads"
        self.studio_dir = root / "studio"
        self.runner = FakeWorkflowRunner(stage_delay_s=0.01)
        self.addCleanup(self.runner.shutdown)
        self.studio = FakeStudioBackend(
            StudioStore(self.studio_dir), busy_lock=self.runner.busy_lock
        )
        self.client = self._client(self.runner, self.studio)

    def _client(self, runner, studio, token=None) -> TestClient:
        return TestClient(
            create_app(runner, upload_dir=self.upload_dir, token=token, studio=studio)
        )

    def capture(self, client=None) -> dict:
        response = (client or self.client).post("%s/captures" % STUDIO_URL)
        self.assertEqual(response.status_code, 201)
        return response.json()

    def segment_library_id(self) -> str:
        capture = self.capture()
        segmented = self.client.post("%s/captures/%s/segment" % (STUDIO_URL, capture["id"]))
        self.assertEqual(segmented.status_code, 200)
        return segmented.json()["spines"][0]["library_id"]

    def test_lift_move_updates_the_reported_height(self):
        initial = self.client.get("%s/lift/height" % STUDIO_URL)
        self.assertEqual(initial.status_code, 200)
        self.assertEqual(initial.json(), {"height_mm": 10.0})

        moved = self.client.post(
            "%s/lift/move" % STUDIO_URL, json={"height_mm": 300, "speed_level": 2}
        )

        self.assertEqual(moved.status_code, 200)
        self.assertEqual(
            moved.json(), {"target_height_mm": 300.0, "measured_height_mm": 300.0}
        )
        reread = self.client.get("%s/lift/height" % STUDIO_URL)
        self.assertEqual(reread.json(), {"height_mm": 300.0})

    def test_lift_move_defaults_speed_and_validates_ranges(self):
        moved = self.client.post("%s/lift/move" % STUDIO_URL, json={"height_mm": 320})

        self.assertEqual(moved.status_code, 200)
        self.assertEqual(moved.json()["target_height_mm"], 320.0)
        self.assertEqual(
            self.client.post("%s/lift/move" % STUDIO_URL, json={"height_mm": 5}).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                "%s/lift/move" % STUDIO_URL, json={"height_mm": 300, "speed_level": 4}
            ).status_code,
            422,
        )

    def test_capture_serves_a_jpeg_image(self):
        capture = self.capture()

        self.assertEqual(capture["width"], 640)
        self.assertEqual(capture["height"], 480)
        self.assertEqual(capture["frame_number"], 1)
        self.assertTrue(capture["created_at"])

        image = self.client.get("%s/captures/%s/image" % (STUDIO_URL, capture["id"]))

        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.headers["content-type"], "image/jpeg")
        self.assertTrue(image.content.startswith(b"\xff\xd8"))

    def test_segment_populates_the_library_and_serves_png_crops(self):
        capture = self.capture()

        segmented = self.client.post("%s/captures/%s/segment" % (STUDIO_URL, capture["id"]))

        self.assertEqual(segmented.status_code, 200)
        payload = segmented.json()
        self.assertEqual(payload["capture_id"], capture["id"])
        self.assertGreater(payload["spine_count"], 0)
        self.assertEqual(payload["spine_count"], len(payload["spines"]))
        spine = payload["spines"][0]
        self.assertTrue(spine["library_id"])
        self.assertGreater(spine["confidence"], 0.0)
        self.assertGreater(spine["area"], 0)
        self.assertEqual(len(spine["bbox_xyxy"]), 4)
        self.assertEqual(len(spine["polygon_xy"]), 4)

        library = self.client.get("%s/library" % STUDIO_URL)
        self.assertEqual(library.status_code, 200)
        items = library.json()["items"]
        self.assertEqual(len(items), payload["spine_count"])
        self.assertEqual(items[0]["id"], payload["spines"][-1]["library_id"])
        self.assertEqual(items[0]["capture_id"], capture["id"])
        self.assertEqual(items[0]["spine_index"], payload["spines"][-1]["index"])

        image = self.client.get("%s/library/%s/image" % (STUDIO_URL, spine["library_id"]))
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.headers["content-type"], "image/png")
        self.assertTrue(image.content.startswith(b"\x89PNG"))

    def test_segment_accepts_optional_threshold_overrides(self):
        capture = self.capture()

        segmented = self.client.post(
            "%s/captures/%s/segment" % (STUDIO_URL, capture["id"]),
            json={"conf": 0.5, "imgsz": 640},
        )

        self.assertEqual(segmented.status_code, 200)
        self.assertGreater(segmented.json()["spine_count"], 0)

    def test_segment_unknown_capture_returns_404(self):
        response = self.client.post("%s/captures/nope/segment" % STUDIO_URL)

        self.assertEqual(response.status_code, 404)

    def test_manual_segment_stores_a_named_library_template(self):
        capture = self.capture()
        polygon = [[60, 90], [160, 90], [160, 420], [60, 420]]

        response = self.client.post(
            "%s/captures/%s/manual-segment" % (STUDIO_URL, capture["id"]),
            json={"name": "红楼梦 上册", "polygon": polygon},
        )

        self.assertEqual(response.status_code, 201)
        spine = response.json()
        self.assertTrue(spine["library_id"])
        self.assertEqual(spine["confidence"], 1.0)
        self.assertEqual(spine["index"], 0)
        self.assertEqual(
            spine["polygon_xy"],
            [[60.0, 90.0], [160.0, 90.0], [160.0, 420.0], [60.0, 420.0]],
        )
        self.assertEqual(spine["bbox_xyxy"], [52, 82, 169, 429])

        items = self.client.get("%s/library" % STUDIO_URL).json()["items"]
        match = [item for item in items if item["id"] == spine["library_id"]]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0]["name"], "红楼梦_上册")
        self.assertEqual(match[0]["capture_id"], capture["id"])

        image = self.client.get("%s/library/%s/image" % (STUDIO_URL, spine["library_id"]))
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.headers["content-type"], "image/png")
        self.assertTrue(image.content.startswith(b"\x89PNG"))

        # The name survives a manifest reload so takeout can pick it by template_id.
        reloaded = StudioStore(self.studio_dir)
        records = [record for record in reloaded.list_library() if record.id == spine["library_id"]]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].name, "红楼梦_上册")

    def test_manual_segment_validates_name_polygon_and_capture(self):
        capture = self.capture()
        base = "%s/captures/%s/manual-segment" % (STUDIO_URL, capture["id"])
        polygon = [[60, 90], [160, 90], [160, 420]]

        self.assertEqual(
            self.client.post(base, json={"name": "  ", "polygon": polygon}).status_code, 422
        )
        self.assertEqual(
            self.client.post(base, json={"name": "???", "polygon": polygon}).status_code, 422
        )
        self.assertEqual(
            self.client.post(base, json={"name": "book", "polygon": [[0, 0], [10, 10]]}).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                base,
                json={"name": "book", "polygon": [[0, 0], [639, 0], [639, 479], [2000, 300]]},
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(base, json={"name": "book", "polygon": polygon, "pad": 65}).status_code,
            422,
        )
        missing = self.client.post(
            "%s/captures/nope/manual-segment" % STUDIO_URL,
            json={"name": "book", "polygon": polygon},
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(self.client.get("%s/library" % STUDIO_URL).json()["items"], [])

    def test_delete_library_item_removes_it(self):
        library_id = self.segment_library_id()

        deleted = self.client.delete("%s/library/%s" % (STUDIO_URL, library_id))

        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(
            self.client.get("%s/library/%s/image" % (STUDIO_URL, library_id)).status_code, 404
        )
        self.assertEqual(
            self.client.delete("%s/library/%s" % (STUDIO_URL, library_id)).status_code, 404
        )
        remaining = self.client.get("%s/library" % STUDIO_URL).json()["items"]
        self.assertNotIn(library_id, [item["id"] for item in remaining])

    def test_library_survives_a_store_reload(self):
        library_id = self.segment_library_id()

        reloaded = FakeStudioBackend(
            StudioStore(self.studio_dir), busy_lock=self.runner.busy_lock
        )
        client = self._client(self.runner, reloaded)

        items = client.get("%s/library" % STUDIO_URL).json()["items"]
        self.assertIn(library_id, [item["id"] for item in items])
        self.assertEqual(
            client.get("%s/library/%s/image" % (STUDIO_URL, library_id)).status_code, 200
        )

    def test_takeout_accepts_a_library_template_id(self):
        library_id = self.segment_library_id()

        response = self.client.post(
            TAKEOUT_TASKS_URL,
            data={"target_height_mm": "300", "template_id": library_id},
        )

        self.assertEqual(response.status_code, 201)
        view = poll_terminal(self.client, response.json()["id"])
        self.assertEqual(view["state"], "succeeded")
        vision = next(stage for stage in view["stages"] if stage["name"] == "vision_target")
        self.assertTrue(vision["details"]["spine_template"].endswith("%s.png" % library_id))

    def test_return_accepts_a_library_template_id(self):
        library_id = self.segment_library_id()

        response = self.client.post(
            RETURN_TASKS_URL,
            data={"target_height_mm": "300", "template_id": library_id},
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["kind"], "return_book")

    def test_takeout_rejects_both_or_neither_template_source(self):
        library_id = self.segment_library_id()

        both = self.client.post(
            TAKEOUT_TASKS_URL,
            data={"target_height_mm": "300", "template_id": library_id},
            files={"spine_image": ("book1.png", PNG_BYTES, "image/png")},
        )
        self.assertEqual(both.status_code, 422)
        self.assertIn("exactly one", both.json()["detail"])

        neither = self.client.post(TAKEOUT_TASKS_URL, data={"target_height_mm": "300"})
        self.assertEqual(neither.status_code, 422)
        self.assertIn("is required", neither.json()["detail"])

    def test_unknown_template_id_is_rejected(self):
        response = self.client.post(
            TAKEOUT_TASKS_URL,
            data={"target_height_mm": "300", "template_id": "no-such-image"},
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("unknown template_id", response.json()["detail"])

    def test_studio_actions_are_rejected_while_a_workflow_runs(self):
        entered = threading.Event()
        gate = threading.Event()

        def hook(name: str) -> None:
            if name == "lift_move":
                entered.set()
                gate.wait(timeout=10.0)

        gated = FakeWorkflowRunner(stage_delay_s=0.01, stage_hook=hook)
        self.addCleanup(gated.shutdown)
        studio = FakeStudioBackend(
            StudioStore(self.studio_dir / "gated"), busy_lock=gated.busy_lock
        )
        client = self._client(gated, studio)
        first = client.post(
            TAKEOUT_TASKS_URL,
            data={"target_height_mm": "300"},
            files={"spine_image": ("book1.png", PNG_BYTES, "image/png")},
        )
        self.assertEqual(first.status_code, 201)
        self.assertTrue(entered.wait(timeout=5.0))

        self.assertEqual(client.post("%s/captures" % STUDIO_URL).status_code, 409)
        self.assertEqual(
            client.post("%s/lift/move" % STUDIO_URL, json={"height_mm": 300}).status_code, 409
        )
        gate.set()
        self.assertEqual(poll_terminal(client, first.json()["id"])["state"], "succeeded")

    def test_workflow_submission_is_rejected_while_studio_is_busy(self):
        self.runner.busy_lock.acquire()
        try:
            response = self.client.post(
                TAKEOUT_TASKS_URL,
                data={"target_height_mm": "300"},
                files={"spine_image": ("book1.png", PNG_BYTES, "image/png")},
            )
        finally:
            self.runner.busy_lock.release()

        self.assertEqual(response.status_code, 409)

    def test_studio_routes_enforce_the_bearer_token(self):
        client = self._client(self.runner, self.studio, token="secret")

        self.assertEqual(client.get("%s/lift/height" % STUDIO_URL).status_code, 401)
        authorized = client.get(
            "%s/lift/height" % STUDIO_URL, headers={"Authorization": "Bearer secret"}
        )
        self.assertEqual(authorized.status_code, 200)
        self.assertEqual(client.post("%s/captures" % STUDIO_URL).status_code, 401)

    def test_arm_debug_pose_reset_returns_debug_joints(self):
        response = self.client.post("%s/arm/debug-pose" % STUDIO_URL, json={})

        self.assertEqual(response.status_code, 200)
        view = response.json()
        self.assertTrue(view["ok"])
        self.assertEqual(len(view["joints_rad"]), 6)

    def test_arm_debug_pose_reset_conflicts_with_a_busy_runner(self):
        self.runner.busy_lock.acquire()
        try:
            response = self.client.post("%s/arm/debug-pose" % STUDIO_URL, json={})
        finally:
            self.runner.busy_lock.release()

        self.assertEqual(response.status_code, 409)

    def test_rod_move_updates_the_reported_angle(self):
        moved = self.client.post(
            "%s/rod/move" % STUDIO_URL,
            json={"target_deg": -35.0, "speed": 800, "acceleration": 40},
        )

        self.assertEqual(moved.status_code, 200)
        self.assertEqual(
            moved.json(),
            {"target_angle_deg": -35.0, "measured_angle_deg": -35.0},
        )
        status = self.client.get("%s/hardware/status" % STUDIO_URL).json()
        self.assertEqual(status["rod"]["angle_deg"], -35.0)

    def test_rod_move_validates_ranges_and_busy_lock(self):
        self.assertEqual(
            self.client.post(
                "%s/rod/move" % STUDIO_URL, json={"target_deg": 160}
            ).status_code,
            422,
        )
        self.runner.busy_lock.acquire()
        try:
            response = self.client.post(
                "%s/rod/move" % STUDIO_URL, json={"target_deg": 0}
            )
        finally:
            self.runner.busy_lock.release()

        self.assertEqual(response.status_code, 409)

    def test_arm_enable_and_disable_return_acknowledgements(self):
        enabled = self.client.post("%s/arm/enable" % STUDIO_URL)

        self.assertEqual(enabled.status_code, 200)
        self.assertEqual(enabled.json(), {"ok": True, "enabled": True, "was_enabled": True})

        disabled = self.client.post(
            "%s/arm/disable" % STUDIO_URL, json={"duration_s": 3.0}
        )

        self.assertEqual(disabled.status_code, 200)
        view = disabled.json()
        self.assertTrue(view["ok"])
        self.assertFalse(view["enabled"])
        self.assertIn("detail", view)

    def test_arm_joints_read_move_and_zero(self):
        read = self.client.get("%s/arm/joints" % STUDIO_URL)
        self.assertEqual(read.status_code, 200)
        view = read.json()
        self.assertEqual(len(view["joints_rad"]), 6)
        self.assertEqual(len(view["joints_deg"]), 6)

        moved = self.client.post(
            "%s/arm/joints/move" % STUDIO_URL,
            json={"joints_deg": [10.0, -5.0, 20.0, 0.0, 30.0, -15.0], "duration_s": 2.0},
        )
        self.assertEqual(moved.status_code, 200)
        view = moved.json()
        self.assertEqual(
            [round(value, 6) for value in view["joints_deg"]],
            [10.0, -5.0, 20.0, 0.0, 30.0, -15.0],
        )

        zeroed = self.client.post("%s/arm/zero" % STUDIO_URL, json={})
        self.assertEqual(zeroed.status_code, 200)
        self.assertEqual(zeroed.json()["joints_deg"], [0.0] * 6)

    def test_arm_joints_move_validates_input_and_busy_lock(self):
        self.assertEqual(
            self.client.post(
                "%s/arm/joints/move" % STUDIO_URL,
                json={"joints_deg": [10.0] * 5},
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                "%s/arm/joints/move" % STUDIO_URL,
                json={"joints_deg": ["not-a-number"] + [0.0] * 5},
            ).status_code,
            422,
        )
        self.runner.busy_lock.acquire()
        try:
            response = self.client.post(
                "%s/arm/joints/move" % STUDIO_URL,
                json={"joints_deg": [0.0] * 6},
            )
        finally:
            self.runner.busy_lock.release()

        self.assertEqual(response.status_code, 409)

    def test_arm_zero_calibration_requires_confirm_and_motor_range(self):
        missing_confirm = self.client.post(
            "%s/arm/zero-calibration" % STUDIO_URL, json={"motor": 0}
        )
        self.assertEqual(missing_confirm.status_code, 422)

        all_joints = self.client.post(
            "%s/arm/zero-calibration" % STUDIO_URL,
            json={"motor": 0, "confirm": True},
        )
        self.assertEqual(all_joints.status_code, 200)
        view = all_joints.json()
        self.assertTrue(view["ok"])
        self.assertEqual(sorted(view["results"]), ["1", "2", "3", "4", "5", "6"])

        single = self.client.post(
            "%s/arm/zero-calibration" % STUDIO_URL,
            json={"motor": 3, "confirm": True},
        )
        self.assertEqual(single.status_code, 200)
        self.assertEqual(single.json()["results"], {"3": True})

        out_of_range = self.client.post(
            "%s/arm/zero-calibration" % STUDIO_URL,
            json={"motor": 9, "confirm": True},
        )
        self.assertEqual(out_of_range.status_code, 422)

    def test_vision_debug_reports_404_until_a_workflow_persists_a_capture(self):
        self.assertEqual(self.client.get("%s/vision-debug" % STUDIO_URL).status_code, 404)
        self.assertEqual(
            self.client.get("%s/vision-debug/image" % STUDIO_URL).status_code, 404
        )

    def test_vision_debug_serves_metadata_and_images(self):
        debug_dir = self.studio_dir / "vision_debug"
        debug_dir.mkdir(parents=True)
        (debug_dir / "latest.json").write_text(
            json.dumps({"kind": "take_book", "matched": True}), encoding="utf-8"
        )
        (debug_dir / "latest_raw.jpg").write_bytes(b"\xff\xd8raw\xff\xd9")
        (debug_dir / "latest_annotated.jpg").write_bytes(b"\xff\xd8annotated\xff\xd9")

        meta = self.client.get("%s/vision-debug" % STUDIO_URL)
        self.assertEqual(meta.status_code, 200)
        self.assertEqual(meta.json()["kind"], "take_book")

        annotated = self.client.get("%s/vision-debug/image" % STUDIO_URL)
        self.assertEqual(annotated.status_code, 200)
        self.assertEqual(annotated.content, b"\xff\xd8annotated\xff\xd9")

        raw = self.client.get("%s/vision-debug/image?kind=raw" % STUDIO_URL)
        self.assertEqual(raw.content, b"\xff\xd8raw\xff\xd9")

        self.assertEqual(
            self.client.get("%s/vision-debug/image?kind=bogus" % STUDIO_URL).status_code,
            422,
        )

    def test_vision_debug_reports_404_until_a_depth_gap_run(self):
        self.assertEqual(self.client.get("%s/vision-debug/gap" % STUDIO_URL).status_code, 404)
        self.assertEqual(
            self.client.get("%s/vision-debug/image?kind=gap_overlay" % STUDIO_URL).status_code,
            404,
        )

    def test_vision_debug_run_executes_depth_gap_and_exposes_artifacts(self):
        ran = self.client.post("%s/vision-debug/run" % STUDIO_URL, json={})

        self.assertEqual(ran.status_code, 200)
        meta = ran.json()
        # Fake scene: the middle spine block is matched; the left strip reads
        # as deeper shelf back (80 px), the right strip as a depth hole
        # (89 px), so the wider right side wins the default largest select.
        self.assertEqual(meta["gap_method"], "depth")
        self.assertEqual(meta["selected_side"], "right")
        self.assertEqual(meta["left_gap_width_px"], 80.0)
        self.assertEqual(meta["right_gap_width_px"], 89.0)
        self.assertEqual(meta["gap_center_uv"], [385, 186])
        self.assertAlmostEqual(meta["y1_m"], -0.045)
        self.assertAlmostEqual(meta["spine_confidence"], 0.99)

        # The run persists overlay/raw/metadata for the debug routes.
        served = self.client.get("%s/vision-debug/gap" % STUDIO_URL)
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.json()["selected_side"], "right")
        for kind in ("gap_raw", "gap_overlay"):
            image = self.client.get("%s/vision-debug/image?kind=%s" % (STUDIO_URL, kind))
            self.assertEqual(image.status_code, 200)
            self.assertGreater(len(image.content), 100)

    def test_vision_debug_run_validates_scan_params(self):
        bad_band = self.client.post("%s/vision-debug/run" % STUDIO_URL, json={"scan_band_px": 0})
        self.assertEqual(bad_band.status_code, 422)
        bad_select = self.client.post("%s/vision-debug/run" % STUDIO_URL, json={"select": "bogus"})
        self.assertEqual(bad_select.status_code, 422)

    def test_vision_debug_run_conflicts_with_a_busy_runner(self):
        self.runner.busy_lock.acquire()
        try:
            response = self.client.post("%s/vision-debug/run" % STUDIO_URL, json={})
        finally:
            self.runner.busy_lock.release()

        self.assertEqual(response.status_code, 409)

    def test_import_library_image_registers_an_external_template(self):
        import cv2
        import numpy as np

        real_png = cv2.imencode(".png", np.full((12, 8, 3), 180, np.uint8))[1].tobytes()
        imported = self.client.post(
            "%s/library" % STUDIO_URL,
            files={"image": ("custom.png", real_png, "image/png")},
        )

        self.assertEqual(imported.status_code, 201)
        view = imported.json()
        self.assertEqual(view["capture_id"], "manual-import")
        self.assertEqual(view["spine_index"], 0)
        self.assertEqual(view["name"], "")

        listing = self.client.get("%s/library" % STUDIO_URL).json()
        self.assertEqual(listing["items"][0]["id"], view["id"])
        image = self.client.get("%s/library/%s/image" % (STUDIO_URL, view["id"]))
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.headers["content-type"], "image/png")

    def test_import_library_image_accepts_an_optional_name(self):
        import cv2
        import numpy as np

        real_png = cv2.imencode(".png", np.full((12, 8, 3), 180, np.uint8))[1].tobytes()
        imported = self.client.post(
            "%s/library" % STUDIO_URL,
            files={"image": ("custom.png", real_png, "image/png")},
            data={"name": "人性哲学"},
        )

        self.assertEqual(imported.status_code, 201)
        view = imported.json()
        self.assertEqual(view["name"], "人性哲学")
        listing = self.client.get("%s/library" % STUDIO_URL).json()
        self.assertEqual(listing["items"][0]["name"], "人性哲学")

    def test_import_library_image_rejects_invalid_uploads(self):
        bad_type = self.client.post(
            "%s/library" % STUDIO_URL,
            files={"image": ("notes.txt", b"hello", "text/plain")},
        )
        self.assertEqual(bad_type.status_code, 422)

        undecodable = self.client.post(
            "%s/library" % STUDIO_URL,
            files={"image": ("fake.png", b"not-an-image", "image/png")},
        )
        self.assertEqual(undecodable.status_code, 422)

    def test_manual_segment_response_includes_a_self_check(self):
        capture = self.capture()
        polygon = [[60, 90], [160, 90], [160, 420], [60, 420]]

        response = self.client.post(
            "%s/captures/%s/manual-segment" % (STUDIO_URL, capture["id"]),
            json={"name": "自检样例", "polygon": polygon},
        )

        self.assertEqual(response.status_code, 201)
        check = response.json()["selfCheck"]
        self.assertIsInstance(check["ok"], bool)
        self.assertTrue(check["detail"])
        self.assertIsInstance(check["feature_count"], int)

    def test_import_library_image_flags_a_featureless_template(self):
        import cv2
        import numpy as np

        flat_png = cv2.imencode(".png", np.full((64, 48, 3), 180, np.uint8))[1].tobytes()
        imported = self.client.post(
            "%s/library" % STUDIO_URL,
            files={"image": ("flat.png", flat_png, "image/png")},
        )

        # The template is still stored; the self-check only warns that
        # take-out matching will not find enough features in it.
        self.assertEqual(imported.status_code, 201)
        view = imported.json()
        self.assertFalse(view["selfCheck"]["ok"])
        self.assertIn("特征不足", view["selfCheck"]["detail"])
        self.assertIn("建议重拍", view["selfCheck"]["detail"])
        listing = self.client.get("%s/library" % STUDIO_URL).json()
        self.assertEqual(listing["items"][0]["id"], view["id"])

    def test_import_library_image_passes_a_feature_rich_template(self):
        import cv2
        import numpy as np

        noise = np.random.default_rng(7).integers(0, 256, (160, 96, 3), dtype=np.uint8)
        noise_png = cv2.imencode(".png", noise)[1].tobytes()
        imported = self.client.post(
            "%s/library" % STUDIO_URL,
            files={"image": ("noise.png", noise_png, "image/png")},
        )

        self.assertEqual(imported.status_code, 201)
        check = imported.json()["selfCheck"]
        self.assertTrue(check["ok"])
        self.assertGreater(check["feature_count"], 0)

    def test_verify_all_reports_every_library_template(self):
        import cv2
        import numpy as np

        empty = self.client.post("%s/library/verify-all" % STUDIO_URL)
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.json(), [])

        flat_png = cv2.imencode(".png", np.full((64, 48, 3), 180, np.uint8))[1].tobytes()
        noise = np.random.default_rng(7).integers(0, 256, (160, 96, 3), dtype=np.uint8)
        noise_png = cv2.imencode(".png", noise)[1].tobytes()
        flat_view = self.client.post(
            "%s/library" % STUDIO_URL,
            files={"image": ("flat.png", flat_png, "image/png")},
            data={"name": "纯色"},
        ).json()
        noise_view = self.client.post(
            "%s/library" % STUDIO_URL,
            files={"image": ("noise.png", noise_png, "image/png")},
            data={"name": "噪声"},
        ).json()

        report = self.client.post("%s/library/verify-all" % STUDIO_URL)

        self.assertEqual(report.status_code, 200)
        entries = {item["id"]: item for item in report.json()}
        self.assertEqual(set(entries), {flat_view["id"], noise_view["id"]})
        self.assertFalse(entries[flat_view["id"]]["ok"])
        self.assertEqual(entries[flat_view["id"]]["name"], "纯色")
        self.assertIn("建议重拍", entries[flat_view["id"]]["detail"])
        self.assertTrue(entries[noise_view["id"]]["ok"])
        self.assertEqual(entries[noise_view["id"]]["name"], "噪声")
        for item in entries.values():
            self.assertTrue(item["detail"])

    def test_self_check_verdicts_are_cached_while_the_file_is_unchanged(self):
        import cv2
        import numpy as np

        noise = np.random.default_rng(7).integers(0, 256, (160, 96, 3), dtype=np.uint8)
        noise_png = cv2.imencode(".png", noise)[1].tobytes()
        with mock.patch.object(
            studio_module, "BookSpineMatcher", wraps=studio_module.BookSpineMatcher
        ) as wrapped:
            imported = self.client.post(
                "%s/library" % STUDIO_URL,
                files={"image": ("noise.png", noise_png, "image/png")},
            ).json()
            again = self.studio.check_library_template(imported["id"])
            report = self.client.post("%s/library/verify-all" % STUDIO_URL).json()

        # The import, the direct check and verify-all share one matcher build.
        self.assertEqual(wrapped.call_count, 1)
        self.assertTrue(again.ok)
        self.assertTrue(report[0]["ok"])


class RealStudioBackendGuidanceTests(unittest.TestCase):
    """Missing YOLO assets surface actionable Chinese 503 guidance."""

    def setUp(self):
        self._dirs = tempfile.TemporaryDirectory()
        self.addCleanup(self._dirs.cleanup)
        root = Path(self._dirs.name)
        self.root = root
        self.store = StudioStore(root / "studio")
        self.store.captures_dir.mkdir(parents=True, exist_ok=True)
        (self.store.captures_dir / "cap1.jpg").write_bytes(b"\xff\xd8\xff\xd9")

    def _backend(self, weights_path, infer_path) -> RealStudioBackend:
        backend = RealStudioBackend(
            camera=None,
            lift=None,
            store=self.store,
            weights_path=weights_path,
            infer_path=infer_path,
        )
        self.addCleanup(backend.shutdown)
        return backend

    def test_missing_weights_returns_chinese_503_guidance(self):
        runner = FakeWorkflowRunner(stage_delay_s=0.01)
        self.addCleanup(runner.shutdown)
        backend = self._backend(
            weights_path=self.root / "missing-best.pt",
            infer_path=self.root / "missing-infer.py",
        )
        backend._infer_module = types.SimpleNamespace(
            segment_image=lambda *args, **kwargs: (None, [])
        )
        client = TestClient(
            create_app(runner, upload_dir=self.root / "uploads", studio=backend)
        )

        response = client.post("%s/captures/cap1/segment" % STUDIO_URL)

        self.assertEqual(response.status_code, 503)
        self.assertIn("书脊分割模型未安装", response.json()["detail"])
        self.assertIn("book_spine_inference_light", response.json()["detail"])

    def test_missing_infer_script_returns_chinese_503_guidance(self):
        weights = self.root / "best.pt"
        weights.write_bytes(b"fake weights")
        backend = self._backend(
            weights_path=weights,
            infer_path=self.root / "missing-infer.py",
        )

        with self.assertRaisesRegex(SegmentationUnavailableError, "书脊分割模型未安装"):
            backend.segment_capture("cap1", conf=0.5, imgsz=640)


class RealStudioBackendResourceTests(unittest.TestCase):
    """Torch thread capping and idle unload of the lazily loaded YOLO model.

    Real torch/ultralytics are never imported here; both are stub modules
    injected into ``sys.modules`` while the segmentation call runs.
    """

    def setUp(self):
        self._dirs = tempfile.TemporaryDirectory()
        self.addCleanup(self._dirs.cleanup)
        root = Path(self._dirs.name)
        self.store = StudioStore(root / "studio")
        self.store.captures_dir.mkdir(parents=True, exist_ok=True)
        (self.store.captures_dir / "cap1.jpg").write_bytes(b"\xff\xd8\xff\xd9")
        self.weights_path = root / "best.pt"
        self.weights_path.write_bytes(b"fake weights")

    def _fake_torch(self, *, interop_raises: bool = False):
        calls = {"num_threads": [], "interop_threads": []}
        module = types.ModuleType("torch")

        def set_num_threads(count):
            calls["num_threads"].append(count)

        def set_num_interop_threads(count):
            calls["interop_threads"].append(count)
            if interop_raises:
                raise RuntimeError(
                    "cannot set number of interop threads after parallel work has started"
                )

        module.set_num_threads = set_num_threads
        module.set_num_interop_threads = set_num_interop_threads
        return module, calls

    def _fake_ultralytics(self):
        loads = []

        class FakeYOLO:
            def __init__(self, weights):
                loads.append(weights)

            def to(self, device):
                self.device = device

        module = types.ModuleType("ultralytics")
        module.YOLO = FakeYOLO
        return module, loads

    def _backend(self, **overrides) -> RealStudioBackend:
        options = {"torch_threads": 2, "model_idle_unload_s": 0.0}
        options.update(overrides)
        backend = RealStudioBackend(
            camera=None,
            lift=None,
            store=self.store,
            weights_path=self.weights_path,
            infer_path=self.weights_path,  # never read; module stubbed below
            **options
        )
        backend._infer_module = types.SimpleNamespace(
            segment_image=lambda *args, **kwargs: (None, [])
        )
        self.addCleanup(backend.shutdown)
        return backend

    def _segment_once(self, backend, torch_module, ultralytics_module) -> None:
        with mock.patch.dict(
            sys.modules, {"torch": torch_module, "ultralytics": ultralytics_module}
        ):
            result = backend.segment_capture("cap1", conf=0.5, imgsz=640)
        self.assertEqual(result.capture_id, "cap1")

    def test_model_load_caps_torch_threads(self):
        torch_module, calls = self._fake_torch()
        ultralytics_module, _loads = self._fake_ultralytics()
        backend = self._backend(torch_threads=2)

        self._segment_once(backend, torch_module, ultralytics_module)

        self.assertEqual(calls["num_threads"], [2])
        self.assertEqual(calls["interop_threads"], [1])

    def test_model_load_tolerates_interop_threads_runtime_error(self):
        torch_module, calls = self._fake_torch(interop_raises=True)
        ultralytics_module, loads = self._fake_ultralytics()
        backend = self._backend()

        self._segment_once(backend, torch_module, ultralytics_module)

        self.assertEqual(calls["num_threads"], [2])
        self.assertEqual(len(loads), 1)
        self.assertIsNotNone(backend._segment_model)

    def test_idle_model_is_unloaded_and_reloaded_on_next_segment(self):
        torch_module, _calls = self._fake_torch()
        ultralytics_module, loads = self._fake_ultralytics()
        backend = self._backend(model_idle_unload_s=0.05)

        self._segment_once(backend, torch_module, ultralytics_module)
        self.assertEqual(len(loads), 1)
        self.assertIsNotNone(backend._segment_model)

        deadline = time.monotonic() + 2.0
        while backend._segment_model is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNone(backend._segment_model)

        self._segment_once(backend, torch_module, ultralytics_module)
        self.assertEqual(len(loads), 2)

    def test_idle_unload_disabled_keeps_model_resident(self):
        torch_module, _calls = self._fake_torch()
        ultralytics_module, loads = self._fake_ultralytics()
        backend = self._backend(model_idle_unload_s=0.0)

        self._segment_once(backend, torch_module, ultralytics_module)
        time.sleep(0.2)

        self.assertIsNotNone(backend._segment_model)
        self.assertIsNone(backend._idle_timer)
        self._segment_once(backend, torch_module, ultralytics_module)
        self.assertEqual(len(loads), 1)


class RealStudioBackendHardwareStatusTests(unittest.TestCase):
    """The consolidated hardware view exposes the FK TCP pose next to joints."""

    def setUp(self):
        self._dirs = tempfile.TemporaryDirectory()
        self.addCleanup(self._dirs.cleanup)
        root = Path(self._dirs.name)
        self.weights_path = root / "best.pt"
        self.weights_path.write_bytes(b"fake weights")

    def _backend(self, arm) -> RealStudioBackend:
        class StubLift:
            def read_lift_height_mm(self):
                return 800.0

        backend = RealStudioBackend(
            camera=None,
            lift=StubLift(),
            store=StudioStore(root=Path(self._dirs.name) / "studio"),
            weights_path=self.weights_path,
            infer_path=self.weights_path,
            arm=arm,
        )
        self.addCleanup(backend.shutdown)
        return backend

    @staticmethod
    def _stub_arm(**overrides):
        class StubArm:
            def health(self):
                return None

            def thermal_status(self):
                return None

            def read_joint_pose(self):
                return JointPose.from_values((0.1,) * 6)

            def tcp_pose(self, joints):
                return BasePose(0.5, 0.1, 0.2, 0.0, math.pi / 2.0, 0.0)

        for name, value in overrides.items():
            setattr(StubArm, name, value)
        return StubArm()

    def test_hardware_status_pose_includes_tcp_in_mm_and_deg(self):
        status = self._backend(self._stub_arm()).hardware_status()

        pose = status["arm"]["pose"]
        self.assertEqual(pose["values_rad"], [0.1] * 6)
        tcp = pose["tcp"]
        self.assertAlmostEqual(tcp["x_mm"], 500.0)
        self.assertAlmostEqual(tcp["y_mm"], 100.0)
        self.assertAlmostEqual(tcp["z_mm"], 200.0)
        self.assertAlmostEqual(tcp["rx_deg"], 0.0)
        self.assertAlmostEqual(tcp["ry_deg"], 90.0)
        self.assertAlmostEqual(tcp["rz_deg"], 0.0)

    def test_hardware_status_tcp_fk_failure_keeps_joint_values(self):
        def failing_tcp_pose(self, joints):
            raise RuntimeError("pinocchio unavailable")

        status = self._backend(
            self._stub_arm(tcp_pose=failing_tcp_pose)
        ).hardware_status()

        pose = status["arm"]["pose"]
        self.assertEqual(pose["values_rad"], [0.1] * 6)
        self.assertIsNone(pose["tcp"])
        self.assertIsNone(status["arm"]["pose_error"])


EXAMPLE_CAMERA_MATRIX = [
    [0.0, 0.0, -1.0, 0.15],
    [1.0, 0.0, 0.0, -0.25],
    [0.0, -1.0, 0.0, 0.165],
    [0.0, 0.0, 0.0, 1.0],
]


class _StubLift:
    def read_lift_height_mm(self):
        return 800.0


class _StubJogArm:
    """Arm double with a fixed FK answer; move_l simulates exact arrival."""

    def __init__(self):
        self.enabled = True
        self.tcp = BasePose(0.3, 0.0, 0.3, 0.0, 0.0, 0.0)
        self.move_l_calls = []
        self.move_l_error = None

    def is_enabled(self):
        return self.enabled

    def read_joint_pose(self):
        return JointPose.from_values((0.0,) * 6)

    def tcp_pose(self, joints):
        return self.tcp

    def move_l(self, target, duration_s):
        self.move_l_calls.append((target, duration_s))
        if self.move_l_error is not None:
            raise self.move_l_error
        self.tcp = target


class _StubLocator:
    """Camera locator double holding one in-memory 4x4 matrix."""

    def __init__(self, matrix):
        self._matrix = [list(row) for row in matrix]
        self.set_calls = []

    def calibration_matrix(self):
        return [list(row) for row in self._matrix]

    def set_calibration(self, matrix):
        self.set_calls.append([list(row) for row in matrix])
        self._matrix = [list(row) for row in matrix]


class _RealBackendHttpTests(unittest.TestCase):
    """Mount a RealStudioBackend with stub devices behind the HTTP router."""

    def setUp(self):
        self._dirs = tempfile.TemporaryDirectory()
        self.addCleanup(self._dirs.cleanup)
        root = Path(self._dirs.name)
        self.weights_path = root / "best.pt"
        self.weights_path.write_bytes(b"fake weights")
        self.runner = FakeWorkflowRunner(stage_delay_s=0.01)
        self.addCleanup(self.runner.shutdown)
        self.calibration_path = root / "calibration" / "camera_to_base.json"
        self.example_path = root / "calibration" / "camera_to_base.example.json"
        self.example_path.parent.mkdir(parents=True, exist_ok=True)
        self.example_path.write_text(
            json.dumps({"matrix": EXAMPLE_CAMERA_MATRIX}), encoding="utf-8"
        )

    def _mount(self, **kwargs):
        options = {
            "camera": None,
            "lift": _StubLift(),
            "store": StudioStore(Path(self._dirs.name) / "studio"),
            "busy_lock": self.runner.busy_lock,
            "weights_path": self.weights_path,
            "infer_path": self.weights_path,
            "calibration_path": self.calibration_path,
            "calibration_example_path": self.example_path,
        }
        options.update(kwargs)
        backend = RealStudioBackend(**options)
        self.addCleanup(backend.shutdown)
        client = TestClient(
            create_app(
                self.runner,
                upload_dir=Path(self._dirs.name) / "uploads",
                studio=backend,
            )
        )
        return client


class CartesianJogHttpTests(_RealBackendHttpTests):
    def setUp(self):
        super().setUp()
        self.arm = _StubJogArm()
        self.client = self._mount(arm=self.arm)

    def test_jog_requires_an_enabled_arm(self):
        self.arm.enabled = False

        response = self.client.post(
            "%s/arm/cartesian-jog" % STUDIO_URL, json={"axis": "x", "delta": 10}
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.arm.move_l_calls, [])

    def test_jog_clamps_step_and_reports_the_new_tcp(self):
        response = self.client.post(
            "%s/arm/cartesian-jog" % STUDIO_URL,
            json={"axis": "x", "delta": 500, "duration_s": 0.1},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["delta_applied"], 100.0)
        self.assertEqual(payload["duration_s"], 0.5)
        target, duration = self.arm.move_l_calls[0]
        self.assertAlmostEqual(target.x_m, 0.4)
        self.assertAlmostEqual(duration, 0.5)
        self.assertAlmostEqual(payload["tcp"]["x_mm"], 400.0)

    def test_jog_clamps_rotation_step(self):
        response = self.client.post(
            "%s/arm/cartesian-jog" % STUDIO_URL, json={"axis": "ry", "delta": 45}
        )

        self.assertEqual(response.status_code, 200)
        self.assertAlmostEqual(response.json()["delta_applied"], 30.0)
        target, _duration = self.arm.move_l_calls[0]
        self.assertAlmostEqual(target.ry_rad, math.radians(30.0))

    def test_jog_rejects_a_target_outside_the_workspace(self):
        self.arm.tcp = BasePose(0.3, 0.0, 0.75, 0.0, 0.0, 0.0)

        response = self.client.post(
            "%s/arm/cartesian-jog" % STUDIO_URL, json={"axis": "z", "delta": 100}
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("workspace", response.json()["detail"])
        self.assertEqual(self.arm.move_l_calls, [])

    def test_jog_ik_failure_returns_422_without_motion(self):
        self.arm.move_l_error = RuntimeError("arm inverse kinematics did not converge")

        response = self.client.post(
            "%s/arm/cartesian-jog" % STUDIO_URL, json={"axis": "x", "delta": 10}
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("no motion was sent", response.json()["detail"])
        # MoveL validates the whole path before moving: the TCP is untouched.
        self.assertAlmostEqual(self.arm.tcp.x_m, 0.3)

    def test_jog_conflicts_with_a_busy_runner(self):
        self.runner.busy_lock.acquire()
        try:
            response = self.client.post(
                "%s/arm/cartesian-jog" % STUDIO_URL, json={"axis": "x", "delta": 10}
            )
        finally:
            self.runner.busy_lock.release()

        self.assertEqual(response.status_code, 409)


class CameraTransformHttpTests(_RealBackendHttpTests):
    def setUp(self):
        super().setUp()
        self.locator = _StubLocator(EXAMPLE_CAMERA_MATRIX)
        self.client = self._mount(camera=self.locator)

    def test_get_returns_matrix_pose6_and_example_source(self):
        response = self.client.get("%s/camera-transform" % STUDIO_URL)

        self.assertEqual(response.status_code, 200)
        view = response.json()
        self.assertEqual(view["matrix"], EXAMPLE_CAMERA_MATRIX)
        self.assertFalse(view["override"])
        self.assertEqual(view["source"], str(self.example_path))
        self.assertAlmostEqual(view["x_mm"], 150.0)
        self.assertAlmostEqual(view["y_mm"], -250.0)
        self.assertAlmostEqual(view["z_mm"], 165.0)
        self.assertAlmostEqual(view["rx_deg"], -90.0)
        self.assertAlmostEqual(view["ry_deg"], 0.0)
        self.assertAlmostEqual(view["rz_deg"], 90.0)

    def test_put_pose6_persists_hot_swaps_and_round_trips(self):
        response = self.client.put(
            "%s/camera-transform" % STUDIO_URL,
            json={
                "x_mm": 152.0,
                "y_mm": -248.0,
                "z_mm": 165.0,
                "rx_deg": -89.5,
                "ry_deg": 0.2,
                "rz_deg": 90.1,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.calibration_path.is_file())
        self.assertEqual(len(self.locator.set_calls), 1)
        view = response.json()
        self.assertTrue(view["override"])
        self.assertEqual(view["source"], str(self.calibration_path))
        self.assertAlmostEqual(view["x_mm"], 152.0)
        self.assertAlmostEqual(view["rz_deg"], 90.1)
        # The persisted file loads with the stock calibration reader.
        persisted = CameraToBaseTransform.from_json(self.calibration_path)
        np.testing.assert_allclose(
            persisted.matrix.tolist(), self.locator.calibration_matrix(), atol=1e-12
        )

    def test_put_accepts_a_raw_4x4_matrix(self):
        response = self.client.put(
            "%s/camera-transform" % STUDIO_URL,
            json={"matrix": EXAMPLE_CAMERA_MATRIX},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["matrix"], EXAMPLE_CAMERA_MATRIX)

    def test_put_rejects_a_non_rigid_matrix(self):
        scaled = np.eye(4) * 2.0
        scaled[3, 3] = 1.0

        response = self.client.put(
            "%s/camera-transform" % STUDIO_URL,
            json={"matrix": scaled.tolist()},
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("orthonormal", response.json()["detail"])
        self.assertFalse(self.calibration_path.exists())
        self.assertEqual(self.locator.set_calls, [])

    def test_reset_deletes_the_override_and_restores_the_example(self):
        updated = self.client.put(
            "%s/camera-transform" % STUDIO_URL,
            json={
                "x_mm": 200.0,
                "y_mm": -250.0,
                "z_mm": 165.0,
                "rx_deg": -90.0,
                "ry_deg": 0.0,
                "rz_deg": 90.0,
            },
        )
        self.assertEqual(updated.status_code, 200)
        self.assertTrue(self.calibration_path.is_file())

        response = self.client.post("%s/camera-transform/reset" % STUDIO_URL)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.calibration_path.exists())
        view = response.json()
        self.assertFalse(view["override"])
        self.assertEqual(view["source"], str(self.example_path))
        self.assertAlmostEqual(view["x_mm"], 150.0)


if __name__ == "__main__":
    unittest.main()