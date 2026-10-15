"""HTTP mid-return service end-to-end tests with the hardware-free runner."""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path
import unittest

import numpy as np
from fastapi.testclient import TestClient

from bookarm.service.app import create_app
from bookarm.service.runner import FakeWorkflowRunner
from bookarm.service.studio import FakeStudioBackend, StudioStore


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
MIDRETURN_TASKS_URL = "/api/v1/midreturn/tasks"
TAKEOUT_TASKS_URL = "/api/v1/takeout/tasks"
ALL_MIDRETURN_STAGES = (
    "lift_move",
    "arm_debug_pose",
    "vision_target",
    "book_grasp",
    "book_put_back",
    "book_stand_pickup",
    "vision_retarget",
    "insert_target",
    "shelf_insert",
)


def create_midreturn_task(client: TestClient, **form):
    data = {"target_height_mm": "300"}
    data.update({key: str(value) for key, value in form.items()})
    return client.post(
        MIDRETURN_TASKS_URL,
        data=data,
        files={"left_neighbor_image": ("neighbor1.png", PNG_BYTES, "image/png")},
    )


def create_takeout_task(client: TestClient, **form):
    data = {"target_height_mm": "300"}
    data.update({key: str(value) for key, value in form.items()})
    return client.post(
        TAKEOUT_TASKS_URL,
        data=data,
        files={"spine_image": ("book1.png", PNG_BYTES, "image/png")},
    )


def poll_terminal(client: TestClient, task_id: str, timeout_s: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        view = client.get("%s/%s" % (MIDRETURN_TASKS_URL, task_id)).json()
        if view["state"] in {"succeeded", "failed", "cancelled", "unsupported", "recovery_required"}:
            return view
        time.sleep(0.01)
    raise AssertionError("task %s did not reach a terminal state" % task_id)


class MidReturnServiceTests(unittest.TestCase):
    def setUp(self):
        self._dirs = tempfile.TemporaryDirectory()
        self.addCleanup(self._dirs.cleanup)
        root = Path(self._dirs.name)
        self.runner = FakeWorkflowRunner(stage_delay_s=0.01)
        self.addCleanup(self.runner.shutdown)
        self.studio = FakeStudioBackend(
            StudioStore(root / "studio"), busy_lock=self.runner.busy_lock
        )
        self.client = TestClient(
            create_app(self.runner, upload_dir=root / "uploads", studio=self.studio)
        )

    def gated_runner(self, stage: str):
        """Return a runner blocked at one stage plus its release control."""

        entered = threading.Event()
        gate = threading.Event()

        def hook(name: str) -> None:
            if name == stage:
                entered.set()
                gate.wait(timeout=10.0)

        runner = FakeWorkflowRunner(stage_delay_s=0.01, stage_hook=hook)
        self.addCleanup(runner.shutdown)
        client = TestClient(
            create_app(runner, upload_dir=Path(self._dirs.name) / "uploads2", studio=self.studio)
        )
        return client, entered, gate

    def test_midreturn_task_runs_to_success_with_all_stages(self):
        response = create_midreturn_task(self.client, source_slot=2, book_id="BOOK-9")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["kind"], "mid_return_book")
        task_id = response.json()["id"]
        view = poll_terminal(self.client, task_id)

        self.assertEqual(view["kind"], "mid_return_book")
        self.assertEqual(view["state"], "succeeded")
        self.assertEqual([stage["name"] for stage in view["stages"]], list(ALL_MIDRETURN_STAGES))
        self.assertTrue(all(stage["status"] == "succeeded" for stage in view["stages"]))
        self.assertIsNone(view["error"])
        self.assertEqual(view["payload"]["book_stand_source_slot"], 2)
        self.assertEqual(view["payload"]["lift_target_height_mm"], 300.0)
        self.assertTrue(view["payload"]["left_neighbor_spine_template"])

    def test_template_id_resolves_against_the_studio_library(self):
        image_bgr = np.full((12, 8, 3), 180, np.uint8)
        library_id = self.studio.import_library_image(image_bgr, source="test").id

        response = self.client.post(
            MIDRETURN_TASKS_URL,
            data={"target_height_mm": "300", "template_id": library_id},
        )

        self.assertEqual(response.status_code, 201)
        view = poll_terminal(self.client, response.json()["id"])
        self.assertEqual(view["state"], "succeeded")

        unknown = self.client.post(
            MIDRETURN_TASKS_URL,
            data={"target_height_mm": "300", "template_id": "nope"},
        )
        self.assertEqual(unknown.status_code, 422)

    def test_stop_after_stage_truncates_the_workflow(self):
        response = create_midreturn_task(self.client, stop_after_stage="book_grasp")

        self.assertEqual(response.status_code, 201)
        view = poll_terminal(self.client, response.json()["id"])

        self.assertEqual(view["state"], "succeeded")
        self.assertEqual(
            [stage["name"] for stage in view["stages"]],
            ["lift_move", "arm_debug_pose", "vision_target", "book_grasp"],
        )

    def test_invalid_stop_stage_is_rejected(self):
        response = create_midreturn_task(self.client, stop_after_stage="nonsense")

        self.assertEqual(response.status_code, 422)

    def test_source_slot_out_of_range_is_rejected(self):
        response = create_midreturn_task(self.client, source_slot=6)

        self.assertEqual(response.status_code, 422)

    def test_height_out_of_range_is_rejected(self):
        response = create_midreturn_task(self.client, target_height_mm=5)

        self.assertEqual(response.status_code, 422)

    def test_non_image_upload_is_rejected(self):
        response = self.client.post(
            MIDRETURN_TASKS_URL,
            data={"target_height_mm": "300"},
            files={"left_neighbor_image": ("notes.txt", b"not an image", "text/plain")},
        )

        self.assertEqual(response.status_code, 422)

    def test_midreturn_submission_while_take_runs_is_rejected(self):
        client, entered, gate = self.gated_runner("lift_move")
        first = create_takeout_task(client)
        self.assertEqual(first.status_code, 201)
        self.assertTrue(entered.wait(timeout=5.0))

        second = create_midreturn_task(client)

        self.assertEqual(second.status_code, 409)
        gate.set()
        # Both routers share one task store, so the take task is visible here.
        view = poll_terminal(client, first.json()["id"])
        self.assertEqual(view["state"], "succeeded")

    def test_cancel_stops_the_midreturn_task_at_a_stage_boundary(self):
        client, entered, gate = self.gated_runner("book_put_back")
        first = create_midreturn_task(client)
        self.assertEqual(first.status_code, 201)
        task_id = first.json()["id"]
        self.assertTrue(entered.wait(timeout=5.0))

        cancelled = client.post("%s/%s/cancel" % (MIDRETURN_TASKS_URL, task_id))

        self.assertEqual(cancelled.status_code, 200)
        gate.set()
        view = poll_terminal(client, task_id)
        self.assertEqual(view["state"], "cancelled")
        self.assertEqual(
            [stage["name"] for stage in view["stages"]],
            ["lift_move", "arm_debug_pose", "vision_target", "book_grasp", "book_put_back"],
        )

    def test_unknown_task_returns_404(self):
        self.assertEqual(self.client.get("%s/nope" % MIDRETURN_TASKS_URL).status_code, 404)
        self.assertEqual(
            self.client.post("%s/nope/cancel" % MIDRETURN_TASKS_URL).status_code, 404
        )


if __name__ == "__main__":
    unittest.main()
