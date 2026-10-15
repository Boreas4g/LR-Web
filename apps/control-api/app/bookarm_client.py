"""Minimal HTTP client for the on-robot bookarm service over its Unix socket.

The bookarm daemon serves FastAPI on a private Unix socket (default
/run/lr-bookarm/bookarm.sock). A full HTTP stack (httpx/requests) is
overkill here and neither ships unix-socket support out of the box without
extra adapters, so this module speaks just enough HTTP/1.1 for JSON and
multipart form posts.
"""

from __future__ import annotations

import json
import socket
from typing import Any
from uuid import uuid4


class BookarmError(RuntimeError):
    """Raised when the bookarm service is unreachable or rejects a request."""

    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status


class BookarmClient:
    """Synchronous, short-lived connections; safe to use from worker threads."""

    def __init__(self, socket_path: str, token: str | None = None, timeout_s: float = 10.0) -> None:
        self._socket_path = socket_path
        self._token = token
        self._timeout_s = float(timeout_s)

    def create_takeout_task(
        self,
        *,
        template_id: str,
        target_height_mm: float,
        destination_slot: int,
        lift_speed_level: int,
        book_id: str | None = None,
    ) -> dict[str, Any]:
        fields = {
            "template_id": template_id,
            "target_height_mm": str(target_height_mm),
            "destination_slot": str(destination_slot),
            "lift_speed_level": str(lift_speed_level),
        }
        if book_id:
            fields["book_id"] = book_id
        return self._post_task_form("/api/v1/takeout/tasks", fields)

    def get_takeout_task(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/takeout/tasks/{task_id}", None, None)

    def cancel_takeout_task(self, task_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/takeout/tasks/{task_id}/cancel", b"", None)

    def create_midreturn_task(
        self,
        *,
        template_id: str,
        target_height_mm: float,
        source_slot: int,
        lift_speed_level: int,
        book_id: str | None = None,
        injected_grasp_pose: list[float] | None = None,
    ) -> dict[str, Any]:
        fields = {
            "template_id": template_id,
            "target_height_mm": str(target_height_mm),
            "source_slot": str(source_slot),
            "lift_speed_level": str(lift_speed_level),
        }
        if book_id:
            fields["book_id"] = book_id
        if injected_grasp_pose is not None:
            fields["injected_grasp_pose"] = ",".join(str(float(value)) for value in injected_grasp_pose)
        return self._post_task_form("/api/v1/midreturn/tasks", fields)

    def get_midreturn_task(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/midreturn/tasks/{task_id}", None, None)

    def cancel_midreturn_task(self, task_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/midreturn/tasks/{task_id}/cancel", b"", None)

    def create_return_task(
        self,
        *,
        template_id: str,
        target_height_mm: float,
        source_slot: int,
        lift_speed_level: int,
        book_id: str | None = None,
    ) -> dict[str, Any]:
        fields = {
            "template_id": template_id,
            "target_height_mm": str(target_height_mm),
            "source_slot": str(source_slot),
            "lift_speed_level": str(lift_speed_level),
        }
        if book_id:
            fields["book_id"] = book_id
        return self._post_task_form("/api/v1/return/tasks", fields)

    def get_return_task(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/return/tasks/{task_id}", None, None)

    def cancel_return_task(self, task_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/return/tasks/{task_id}/cancel", b"", None)

    def dismiss_recovery(self, task_id: str) -> dict[str, Any]:
        """Clear a RECOVERY_REQUIRED task after the operator confirms the arm
        has been handled manually. 200 on success; 409 when the task is not in
        the recovery state."""
        return self._post_json(f"/api/v1/tasks/{task_id}/dismiss-recovery", {})

    def safety_stop(self, emergency: bool = False) -> dict[str, Any]:
        """Stop arm/lift motion and interrupt any running bookarm task.

        emergency=True hits the hard emergency-stop endpoint (used by the
        control station's E-stop interlock); False is the softer stop.
        Returns {"accepted", "armStopped", "liftStopped", "taskInterrupted"}.
        """
        path = "/api/v1/safety/emergency-stop" if emergency else "/api/v1/safety/stop"
        return self._post_json(path, {})

    def get_hardware_status(self) -> dict[str, Any]:
        """Read the studio hardware status snapshot (read-only, no motion)."""
        return self._request("GET", "/api/v1/studio/hardware/status", None, None)

    def list_library(self) -> dict[str, Any]:
        """List the on-robot spine-library templates (id/name/created_at)."""
        return self._request("GET", "/api/v1/studio/library", None, None)

    def import_library_image(
        self, *, filename: str, data: bytes, name: str = "", source: str = "batch-import"
    ) -> dict[str, Any]:
        """Upload one spine image file into the on-robot library; returns the
        created entry (its id becomes a book's templateId)."""
        return self._post_file_form(
            "/api/v1/studio/library",
            file_field="image",
            filename=filename,
            data=data,
            fields={"source": source, **({"name": name} if name else {})},
        )

    def _post_file_form(
        self,
        path: str,
        *,
        file_field: str,
        filename: str,
        data: bytes,
        fields: dict[str, str],
    ) -> dict[str, Any]:
        boundary = f"----lr-bookarm-{uuid4().hex}"
        parts: list[bytes] = []
        for key, value in fields.items():
            parts.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode("utf-8")
            )
        safe_name = filename.replace('"', "_")
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{safe_name}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8")
            + data
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        return self._request(
            "POST",
            path,
            b"".join(parts),
            f"multipart/form-data; boundary={boundary}",
        )

    _VISION_DEBUG_KINDS = {"annotated", "gap_overlay"}

    def get_vision_debug_image(self, kind: str) -> bytes:
        """Fetch the latest workflow vision debug JPEG (spine-match annotated
        overlay or mid-return gap overlay) as raw bytes."""
        if kind not in self._VISION_DEBUG_KINDS:
            raise BookarmError(None, f"未知的识别图类型：{kind}")
        status, payload = self._roundtrip(
            "GET", f"/api/v1/studio/vision-debug/image?kind={kind}", None, None
        )
        if status >= 400:
            raise BookarmError(status, f"bookarm 识别图不可用（{status}）")
        if not payload:
            raise BookarmError(status, "bookarm 识别图响应为空")
        return payload

    def disable_arm(self, duration_s: float = 3.0) -> dict[str, Any]:
        """Move the arm to its safe pose, verify arrival, then disable it."""
        return self._post_json("/api/v1/studio/arm/disable", {"duration_s": duration_s})

    def move_lift(self, height_mm: float, speed_level: int = 2) -> dict[str, Any]:
        """Move the lift to an absolute height (studio endpoint, 10..1700 mm)."""
        return self._post_json(
            "/api/v1/studio/lift/move",
            {"height_mm": height_mm, "speed_level": speed_level},
        )

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST", path, json.dumps(payload).encode("utf-8"), "application/json"
        )

    def _post_task_form(self, path: str, fields: dict[str, str]) -> dict[str, Any]:
        boundary = f"----lr-bookarm-{uuid4().hex}"
        parts = []
        for name, value in fields.items():
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            )
        body = ("".join(parts) + f"--{boundary}--\r\n").encode("utf-8")
        return self._request(
            "POST",
            path,
            body,
            f"multipart/form-data; boundary={boundary}",
        )

    def _request(
        self,
        method: str,
        path: str,
        body: bytes | None,
        content_type: str | None,
    ) -> dict[str, Any]:
        status, response_body = self._roundtrip(method, path, body, content_type)

        try:
            document = json.loads(response_body.decode("utf-8")) if response_body else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BookarmError(status, "bookarm 返回了无效的 JSON 响应") from error

        if status >= 400:
            detail = document.get("detail") if isinstance(document, dict) else None
            if isinstance(detail, list):
                detail = "；".join(
                    str(item.get("msg", item)) if isinstance(item, dict) else str(item)
                    for item in detail
                )
            raise BookarmError(status, str(detail or f"bookarm 请求失败（{status}）"))
        if not isinstance(document, dict):
            raise BookarmError(status, "bookarm 返回了非对象 JSON 响应")
        return document

    def _roundtrip(
        self,
        method: str,
        path: str,
        body: bytes | None,
        content_type: str | None,
    ) -> tuple[int, bytes]:
        """One short-lived unix-socket HTTP exchange; returns (status, body)."""
        headers = [f"{method} {path} HTTP/1.1", "Host: bookarm.local", "Connection: close"]
        if self._token:
            headers.append(f"Authorization: Bearer {self._token}")
        payload = body or b""
        if body is not None and content_type:
            headers.append(f"Content-Type: {content_type}")
        headers.append(f"Content-Length: {len(payload)}")
        wire = ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8") + payload

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self._timeout_s)
                connection.connect(self._socket_path)
                connection.sendall(wire)
                chunks: list[bytes] = []
                while True:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
        except OSError as error:
            raise BookarmError(
                None, f"bookarm 服务不可达（{self._socket_path}）：{error}"
            ) from error

        raw = b"".join(chunks)
        head, _, response_body = raw.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("utf-8", errors="replace")
        try:
            status = int(status_line.split()[1])
        except (IndexError, ValueError) as error:
            raise BookarmError(None, f"bookarm 返回了无法解析的响应：{status_line}") from error
        return status, response_body
