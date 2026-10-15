"""Loopback HTTP client for the isolated BookArm SDK runtime service."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from robot_decision.bookarm_client import BookArmApiError, BookArmHealth


class BookArmSdkClient:
    def __init__(self, api_url: str, timeout_seconds: float) -> None:
        self.api_url = api_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)

    def capabilities(self) -> dict[str, Any]:
        # Static so telemetry never depends on a successful hardware call.
        return {
            "schemaVersion": "lr-vehicle-hardware-v1",
            "profile": "LR-01-bookarm-sdk-v1",
            "navigation": True,
            "mapSwitching": True,
            "manualJog": True,
            "softwareStop": False,
            "liftRelativeMotion": False,
            "liftHomeSensor": False,
            "liftAbsolutePositionFeedback": True,
            "liftTargetArrivalFeedback": True,
            "armMotion": True,
            "armFeedback": True,
            "armHomeFeedback": True,
            "rodMotion": True,
            "cameraCapture": True,
            "bookHandoffFeedback": True,
            "bookWorkflowModes": ["takeout", "putback"],
        }

    def health(self) -> BookArmHealth:
        try:
            response = self._request("GET", "/health")
        except BookArmApiError as error:
            return BookArmHealth(False, False, False, False, False, str(error))
        return self._health_from_response(response)

    def prepare_take_book(self, context: dict[str, Any]) -> BookArmHealth:
        return self.prepare_book_workflow({**context, "operation": "borrow"})

    def prepare_book_workflow(self, context: dict[str, Any]) -> BookArmHealth:
        try:
            response = self._request("POST", "/preflight", context)
        except BookArmApiError:
            raise
        health_payload = response.get("health")
        if not isinstance(health_payload, dict):
            raise BookArmApiError("BookArm SDK 服务未返回非动作预检状态")
        health = self._health_from_response(health_payload)
        if not health.available:
            raise BookArmApiError(health.message)
        return health

    @staticmethod
    def _health_from_response(response: dict[str, Any]) -> BookArmHealth:
        return BookArmHealth(
            available=response.get("available") is True,
            arm_ready=response.get("armReady") is True,
            lift_ready=response.get("liftReady") is True,
            camera_ready=response.get("cameraReady") is True,
            rod_ready=response.get("rodReady") is True,
            message=str(response.get("message") or "BookArm SDK 服务状态无效")[:240],
            arm_connected=response.get("armConnected") is True,
            arm_feedback_healthy=response.get("armFeedbackHealthy") is True,
            arm_can_fps=(
                float(response["armCanFps"])
                if isinstance(response.get("armCanFps"), (int, float))
                else 0.0
            ),
            arm_feedback_reason=str(response.get("armFeedbackReason") or "机械臂状态尚未读取")[:240],
            arm_enabled=response.get("armEnabled") is True,
            arm_state=(
                str(response["armState"])
                if response.get("armState") in {"ready", "standby", "fault"}
                else "fault"
            ),
            lift_absolute_positioning_ready=response.get("liftAbsolutePositioningReady") is True,
        )

    def start_take_book(self, context: dict[str, Any]) -> dict[str, Any]:
        response = self._request("POST", "/take-book", context)
        return self._workflow_from_response(response)

    def start_return_book(self, context: dict[str, Any]) -> dict[str, Any]:
        response = self._request("POST", "/return-book", context)
        return self._workflow_from_response(response)

    def workflow(self, workflow_id: str) -> dict[str, Any]:
        return self._workflow_from_response(self._request("GET", f"/workflows/{workflow_id}"))

    def cancel_workflow(self, workflow_id: str) -> dict[str, Any]:
        return self._workflow_from_response(self._request("POST", f"/workflows/{workflow_id}/cancel", {}))

    def safe_stop(self) -> None:
        response = self._request("POST", "/safety/stop", {})
        if response.get("accepted") is not True:
            raise BookArmApiError(str(response.get("reason") or "BookArm SDK 停止请求未被确认"))

    def emergency_stop(self) -> None:
        response = self._request("POST", "/safety/emergency-stop", {})
        if response.get("accepted") is not True:
            raise BookArmApiError(str(response.get("reason") or "BookArm SDK 急停请求未被确认"))

    @staticmethod
    def _workflow_from_response(response: dict[str, Any]) -> dict[str, Any]:
        workflow = response.get("workflow")
        if not isinstance(workflow, dict) or not isinstance(workflow.get("id"), str):
            raise BookArmApiError("BookArm SDK 服务未返回有效工作流")
        return workflow

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(f"{self.api_url}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(128 * 1024 + 1)
        except HTTPError as error:
            detail = error.read(1024).decode("utf-8", errors="replace")
            raise BookArmApiError(f"BookArm SDK 服务拒绝请求（HTTP {error.code}）：{detail}") from error
        except (URLError, OSError) as error:
            raise BookArmApiError(f"BookArm SDK 本机服务不可达：{error}") from error
        if len(raw) > 128 * 1024:
            raise BookArmApiError("BookArm SDK 服务响应超过大小上限")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BookArmApiError("BookArm SDK 服务返回了无效 JSON") from error
        if not isinstance(value, dict):
            raise BookArmApiError("BookArm SDK 服务响应必须是 JSON 对象")
        return value
