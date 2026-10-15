"""Loopback-only HTTP host for BookArm SDK running in its own Python environment."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from robot_decision.bookarm_client import BookArmApiError
from robot_decision.bookarm_sdk_backend import BookArmSdkBackend, BookArmSdkConfig


_MAX_REQUEST_BYTES = 64 * 1024


class BookArmSdkHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], controller: BookArmSdkBackend) -> None:
        super().__init__(address, BookArmSdkRequestHandler)
        self.controller = controller


class BookArmSdkRequestHandler(BaseHTTPRequestHandler):
    server: BookArmSdkHttpServer

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/api/v1/health":
            health = self.server.controller.health()
            self._send_json(
                200,
                {
                    "available": health.available,
                    "armReady": health.arm_ready,
                    "liftReady": health.lift_ready,
                    "cameraReady": health.camera_ready,
                    "rodReady": health.rod_ready,
                    "message": health.message,
                    "armConnected": health.arm_connected,
                    "armFeedbackHealthy": health.arm_feedback_healthy,
                    "armCanFps": health.arm_can_fps,
                    "armFeedbackReason": health.arm_feedback_reason,
                    "armEnabled": health.arm_enabled,
                    "armState": health.arm_state,
                    "liftAbsolutePositioningReady": health.lift_absolute_positioning_ready,
                },
            )
            return
        if path == "/api/v1/capabilities":
            self._send_json(200, self.server.controller.capabilities())
            return
        prefix = "/api/v1/workflows/"
        if path.startswith(prefix) and "/" not in path[len(prefix):]:
            self._workflow_call(lambda: self.server.controller.workflow(path[len(prefix):]))
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        try:
            payload = self._read_json()
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        if path == "/api/v1/take-book":
            self._workflow_call(lambda: self.server.controller.start_take_book(payload))
            return
        if path == "/api/v1/return-book":
            self._workflow_call(lambda: self.server.controller.start_return_book(payload))
            return
        if path == "/api/v1/preflight":
            try:
                health = self.server.controller.prepare_book_workflow(payload)
            except BookArmApiError as error:
                self._send_json(409, {"error": str(error)[:300]})
            else:
                self._send_json(
                    200,
                    {
                        "health": {
                            "available": health.available,
                            "armReady": health.arm_ready,
                            "liftReady": health.lift_ready,
                            "cameraReady": health.camera_ready,
                            "rodReady": health.rod_ready,
                            "message": health.message,
                            "armConnected": health.arm_connected,
                            "armFeedbackHealthy": health.arm_feedback_healthy,
                            "armCanFps": health.arm_can_fps,
                            "armFeedbackReason": health.arm_feedback_reason,
                            "armEnabled": health.arm_enabled,
                            "armState": health.arm_state,
                            "liftAbsolutePositioningReady": health.lift_absolute_positioning_ready,
                        }
                    },
                )
            return
        prefix = "/api/v1/workflows/"
        suffix = "/cancel"
        if path.startswith(prefix) and path.endswith(suffix):
            workflow_id = path[len(prefix):-len(suffix)]
            if workflow_id and "/" not in workflow_id:
                self._workflow_call(lambda: self.server.controller.cancel_workflow(workflow_id))
                return
        if path in {"/api/v1/safety/stop", "/api/v1/safety/emergency-stop"}:
            try:
                if path.endswith("emergency-stop"):
                    self.server.controller.emergency_stop()
                else:
                    self.server.controller.safe_stop()
            except BookArmApiError as error:
                self._send_json(200, {"accepted": False, "reason": str(error)[:300]})
            else:
                self._send_json(200, {"accepted": True})
            return
        self._send_json(404, {"error": "not found"})

    def log_message(self, format: str, *args: Any) -> None:
        # Avoid logging payloads or private local paths. The service manager
        # already records process lifecycle and bounded errors.
        return

    def _workflow_call(self, action) -> None:
        try:
            workflow = action()
        except BookArmApiError as error:
            self._send_json(409, {"error": str(error)[:300]})
            return
        self._send_json(200, {"workflow": workflow})

    def _read_json(self) -> dict[str, Any]:
        value = self.headers.get("Content-Length")
        try:
            length = int(value) if value is not None else 0
        except ValueError as error:
            raise ValueError("invalid Content-Length") from error
        if not 0 <= length <= _MAX_REQUEST_BYTES:
            raise ValueError("request body exceeds 64 KiB")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("request body must be UTF-8 JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--sdk-root", type=Path, required=True)
    parser.add_argument("--book-stand-slots", type=Path, required=True)
    parser.add_argument("--book-stand-pickup-slots", type=Path)
    parser.add_argument("--camera-calibration", type=Path, required=True)
    parser.add_argument("--camera-config", type=Path)
    parser.add_argument("--take-book-profile", type=Path)
    parser.add_argument("--return-book-profile", type=Path)
    parser.add_argument("--spine-storage-root", type=Path, default=Path("/var/lib/lr-robot/book-spines"))
    parser.add_argument("--rod-device", type=Path, default=Path("/dev/rodmotor"))
    parser.add_argument("--lift-device", type=Path, default=Path("/dev/lift_port"))
    arguments = parser.parse_args(argv)
    if not 1024 <= arguments.port <= 65535:
        parser.error("--port must be 1024..65535")
    controller = BookArmSdkBackend(
        BookArmSdkConfig(
            sdk_root=arguments.sdk_root,
            book_stand_slots_file=arguments.book_stand_slots,
            camera_calibration_file=arguments.camera_calibration,
            camera_device_config_file=arguments.camera_config,
            take_book_profile_file=arguments.take_book_profile,
            book_stand_pickup_slots_file=arguments.book_stand_pickup_slots,
            return_book_profile_file=arguments.return_book_profile,
            spine_storage_root=arguments.spine_storage_root,
            rod_device_path=arguments.rod_device,
            lift_device_path=arguments.lift_device,
        )
    )
    server = BookArmSdkHttpServer(("127.0.0.1", arguments.port), controller)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        controller.shutdown()


if __name__ == "__main__":
    main()
