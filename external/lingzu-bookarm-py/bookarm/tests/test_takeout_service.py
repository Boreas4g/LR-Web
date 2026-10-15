"""HTTP take-out service end-to-end tests with the hardware-free runner."""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path
import unittest

from fastapi.testclient import TestClient

from bookarm.service.app import create_app
from bookarm.service.runner import FakeTakeoutRunner


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
TASKS_URL = "/api/v1/takeout/tasks"
HEALTH_URL = "/api/v1/takeout/health"
ALL_STAGES = (
    "lift_move",
    "vision_target",
    "arm_debug_pose",
    "book_grasp",
    "book_stand_place",
    "arm_standby",
)


def create_task(client: TestClient, **form):
    data = {"target_height_mm": "300"}
    data.update({key: str(value) for key, value in form.items()})
    return client.post(
        TASKS_URL,
        data=data,
        files={"spine_image": ("book1.png", PNG_BYTES, "image/png")},
    )


def poll_terminal(client: TestClient, task_id: str, timeout_s: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        view = client.get("%s/%s" % (TASKS_URL, task_id)).json()
        if view["state"] in {"succeeded", "failed", "cancelled", "unsupported", "recovery_required"}:
            return view
        time.sleep(0.01)
    raise AssertionError("task %s did not reach a terminal state" % task_id)


class TakeoutServiceTests(unittest.TestCase):
    def setUp(self):
        self._upload = tempfile.TemporaryDirectory()
        self.addCleanup(self._upload.cleanup)
        self.runner = FakeTakeoutRunner(stage_delay_s=0.01)
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

        runner = FakeTakeoutRunner(stage_delay_s=0.01, stage_hook=hook)
        self.addCleanup(runner.shutdown)
        client = TestClient(create_app(runner, upload_dir=Path(self._upload.name)))
        return client, entered, gate

    def test_task_runs_to_success_with_all_stages(self):
        response = create_task(self.client)

        self.assertEqual(response.status_code, 201)
        task_id = response.json()["id"]
        view = poll_terminal(self.client, task_id)

        self.assertEqual(view["state"], "succeeded")
        self.assertEqual([stage["name"] for stage in view["stages"]], list(ALL_STAGES))
        self.assertTrue(all(stage["status"] == "succeeded" for stage in view["stages"]))
        self.assertIsNone(view["error"])
        self.assertTrue(view["payload"])

    def test_stop_after_stage_truncates_the_workflow(self):
        response = create_task(self.client, stop_after_stage="vision_target")

        self.assertEqual(response.status_code, 201)
        view = poll_terminal(self.client, response.json()["id"])

        self.assertEqual(view["state"], "succeeded")
        self.assertEqual(
            [stage["name"] for stage in view["stages"]],
            ["lift_move", "vision_target"],
        )

    def test_height_out_of_range_is_rejected(self):
        response = create_task(self.client, target_height_mm=5)

        self.assertEqual(response.status_code, 422)

    def test_non_image_upload_is_rejected(self):
        response = self.client.post(
            TASKS_URL,
            data={"target_height_mm": "300"},
            files={"spine_image": ("notes.txt", b"not an image", "text/plain")},
        )

        self.assertEqual(response.status_code, 422)

    def test_second_task_while_busy_is_rejected(self):
        client, entered, gate = self.gated_runner("lift_move")
        first = create_task(client)
        self.assertEqual(first.status_code, 201)
        self.assertTrue(entered.wait(timeout=5.0))

        second = create_task(client)

        self.assertEqual(second.status_code, 409)
        gate.set()
        self.assertEqual(poll_terminal(client, first.json()["id"])["state"], "succeeded")

    def test_cancel_stops_the_task_at_a_stage_boundary(self):
        client, entered, gate = self.gated_runner("lift_move")
        first = create_task(client)
        self.assertEqual(first.status_code, 201)
        task_id = first.json()["id"]
        self.assertTrue(entered.wait(timeout=5.0))

        cancelled = client.post("%s/%s/cancel" % (TASKS_URL, task_id))

        self.assertEqual(cancelled.status_code, 200)
        gate.set()
        view = poll_terminal(client, task_id)
        self.assertEqual(view["state"], "cancelled")
        self.assertEqual([stage["name"] for stage in view["stages"]], ["lift_move"])

    def test_unknown_task_returns_404(self):
        self.assertEqual(self.client.get("%s/nope" % TASKS_URL).status_code, 404)
        self.assertEqual(self.client.post("%s/nope/cancel" % TASKS_URL).status_code, 404)

    def test_bearer_token_is_enforced_when_configured(self):
        client = TestClient(
            create_app(
                FakeTakeoutRunner(stage_delay_s=0.01),
                upload_dir=Path(self._upload.name),
                token="secret",
            )
        )

        self.assertEqual(client.get(HEALTH_URL).status_code, 401)
        healthy = client.get(HEALTH_URL, headers={"Authorization": "Bearer secret"})
        self.assertEqual(healthy.status_code, 200)
        self.assertEqual(healthy.json(), {"started": True, "mode": "fake"})
        self.assertEqual(create_task(client).status_code, 401)


if __name__ == "__main__":
    unittest.main()
