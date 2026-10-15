"""HTTP return service end-to-end tests with the hardware-free runner."""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path
import unittest

from fastapi.testclient import TestClient

from bookarm.service.app import create_app
from bookarm.service.runner import FakeWorkflowRunner


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
RETURN_TASKS_URL = "/api/v1/return/tasks"
TAKEOUT_TASKS_URL = "/api/v1/takeout/tasks"
ALL_RETURN_STAGES = (
    "lift_move",
    "arm_debug_pose",
    "vision_retarget",
    "book_stand_pickup",
    "insert_target",
    "reachability_check",
    "shelf_insert",
)


def create_return_task(client: TestClient, **form):
    data = {"target_height_mm": "300"}
    data.update({key: str(value) for key, value in form.items()})
    return client.post(
        RETURN_TASKS_URL,
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
        view = client.get("%s/%s" % (RETURN_TASKS_URL, task_id)).json()
        if view["state"] in {"succeeded", "failed", "cancelled", "unsupported", "recovery_required"}:
            return view
        time.sleep(0.01)
    raise AssertionError("task %s did not reach a terminal state" % task_id)


class ReturnServiceTests(unittest.TestCase):
    def setUp(self):
        self._upload = tempfile.TemporaryDirectory()
        self.addCleanup(self._upload.cleanup)
        self.runner = FakeWorkflowRunner(stage_delay_s=0.01)
        self.addCleanup(self.runner.shutdown)
        self.client = TestClient(
            create_app(self.runner, upload_dir=Path(self._upload.name))
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
        client = TestClient(create_app(runner, upload_dir=Path(self._upload.name)))
        return client, entered, gate

    def test_return_task_runs_to_success_with_all_stages(self):
        response = create_return_task(self.client)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["kind"], "return_book")
        task_id = response.json()["id"]
        view = poll_terminal(self.client, task_id)

        self.assertEqual(view["kind"], "return_book")
        self.assertEqual(view["state"], "succeeded")
        self.assertEqual([stage["name"] for stage in view["stages"]], list(ALL_RETURN_STAGES))
        self.assertTrue(all(stage["status"] == "succeeded" for stage in view["stages"]))
        self.assertIsNone(view["error"])
        self.assertTrue(view["payload"])

    def test_stop_after_stage_truncates_the_workflow(self):
        response = create_return_task(self.client, stop_after_stage="vision_retarget")

        self.assertEqual(response.status_code, 201)
        view = poll_terminal(self.client, response.json()["id"])

        self.assertEqual(view["state"], "succeeded")
        self.assertEqual(
            [stage["name"] for stage in view["stages"]],
            ["lift_move", "arm_debug_pose", "vision_retarget"],
        )

    def test_source_slot_out_of_range_is_rejected(self):
        response = create_return_task(self.client, source_slot=6)

        self.assertEqual(response.status_code, 422)

    def test_height_out_of_range_is_rejected(self):
        response = create_return_task(self.client, target_height_mm=5)

        self.assertEqual(response.status_code, 422)

    def test_non_image_upload_is_rejected(self):
        response = self.client.post(
            RETURN_TASKS_URL,
            data={"target_height_mm": "300"},
            files={"left_neighbor_image": ("notes.txt", b"not an image", "text/plain")},
        )

        self.assertEqual(response.status_code, 422)

    def test_return_submission_while_take_runs_is_rejected(self):
        client, entered, gate = self.gated_runner("lift_move")
        first = create_takeout_task(client)
        self.assertEqual(first.status_code, 201)
        self.assertTrue(entered.wait(timeout=5.0))

        second = create_return_task(client)

        self.assertEqual(second.status_code, 409)
        gate.set()
        # Both routers share one task store, so the take task is visible here.
        self.assertEqual(poll_terminal(client, first.json()["id"])["state"], "succeeded")

    def test_cancel_stops_the_return_task_at_a_stage_boundary(self):
        client, entered, gate = self.gated_runner("lift_move")
        first = create_return_task(client)
        self.assertEqual(first.status_code, 201)
        task_id = first.json()["id"]
        self.assertTrue(entered.wait(timeout=5.0))

        cancelled = client.post("%s/%s/cancel" % (RETURN_TASKS_URL, task_id))

        self.assertEqual(cancelled.status_code, 200)
        gate.set()
        view = poll_terminal(client, task_id)
        self.assertEqual(view["state"], "cancelled")
        self.assertEqual([stage["name"] for stage in view["stages"]], ["lift_move"])

    def test_unknown_task_returns_404(self):
        self.assertEqual(self.client.get("%s/nope" % RETURN_TASKS_URL).status_code, 404)
        self.assertEqual(self.client.post("%s/nope/cancel" % RETURN_TASKS_URL).status_code, 404)

    def test_takeout_task_view_reports_take_book_kind(self):
        response = create_takeout_task(self.client)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["kind"], "take_book")
        view = poll_terminal(self.client, response.json()["id"])
        self.assertEqual(view["kind"], "take_book")
        self.assertEqual(view["state"], "succeeded")


if __name__ == "__main__":
    unittest.main()
