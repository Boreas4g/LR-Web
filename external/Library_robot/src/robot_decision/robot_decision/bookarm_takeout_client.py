"""HTTP-over-Unix-socket client for the BookArm takeout service.

The production takeout daemon listens only on a private Unix socket
(``/run/lr-bookarm/bookarm.sock``); no TCP port exists, so the urllib-based
clients cannot reach it.  This client speaks plain HTTP/1.1 over AF_UNIX
through a small ``http.client`` subclass and carries the optional Bearer
token the daemon enforces when one is configured.
"""

from __future__ import annotations

import http.client
import json
import socket
from typing import Any

from robot_decision.bookarm_client import BookArmApiError, BookArmHealth


class _UnixSocketHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that connects through a Unix domain socket."""

    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._socket_path)


class BookArmTakeoutClient:
    """Call the takeout service's safety and health routes over its socket."""

    def __init__(self, socket_path: str, timeout_seconds: float, token: str = "") -> None:
        self.socket_path = str(socket_path)
        self.timeout_seconds = float(timeout_seconds)
        self.token = str(token or "")

    def capabilities(self) -> dict[str, Any]:
        # Static so telemetry never depends on a successful hardware call.
        # Only the immediate stop chain is wired through this backend; the
        # legacy relative-lift and maintenance-workflow contracts stay off
        # instead of being faked against the takeout API.
        return {
            "schemaVersion": "lr-vehicle-hardware-v1",
            "profile": "LR-01-bookarm-takeout-v1",
            "navigation": True,
            "mapSwitching": True,
            "manualJog": True,
            "softwareStop": True,
            "liftRelativeMotion": False,
            "liftHomeSensor": False,
            "liftAbsolutePositionFeedback": False,
            "liftTargetArrivalFeedback": False,
            "armMotion": True,
            "armFeedback": True,
            "armHomeFeedback": False,
            "rodMotion": True,
            "cameraCapture": True,
            "bookHandoffFeedback": False,
            "bookWorkflowModes": [],
        }

    def health(self) -> BookArmHealth:
        """Map the takeout health route; readiness fields stay honestly unknown."""

        try:
            response = self._request("GET", "/api/v1/takeout/health")
        except BookArmApiError as error:
            return BookArmHealth(False, False, False, False, False, str(error))
        started = response.get("started") is True
        mode = str(response.get("mode") or "unknown")
        return BookArmHealth(
            started,
            False,
            False,
            False,
            False,
            (
                "takeout 服务运行中（模式 %s）；机械臂/升降台/相机/推杆就绪状态"
                "不经由本后端上报" % mode
                if started
                else "BookArm takeout 服务未就绪"
            )[:240],
            arm_connected=started,
            arm_state="standby" if started else "fault",
        )

    def safe_stop(self) -> dict[str, Any]:
        """Interrupt the workflow and halt arm and lift motion immediately."""

        return self._safety_request("/api/v1/safety/stop", "安全停止")

    def emergency_stop(self) -> dict[str, Any]:
        """Interrupt the workflow and disable all arm motors in place now."""

        return self._safety_request("/api/v1/safety/emergency-stop", "急停")

    def _safety_request(self, path: str, label: str) -> dict[str, Any]:
        response = self._request("POST", path, {})
        if response.get("accepted") is not True:
            raise BookArmApiError("BookArm takeout %s请求未被确认" % label)
        return response

    # The remaining BookArmClient surface is deliberately present but refuses:
    # the maintenance-test and legacy lift contracts do not exist on the
    # takeout service, and a clear rejection keeps the Gateway loop healthy.
    def move_lift(self, _distance_cm: float) -> dict[str, Any]:
        raise BookArmApiError("takeout 后端不支持旧式相对脉冲升降接口")

    def prepare_arm(self) -> BookArmHealth:
        raise BookArmApiError("takeout 后端不支持机械臂维护测试接口")

    def start_workflow(self, _mode: str, auto: bool = True) -> dict[str, Any]:
        raise BookArmApiError("takeout 后端不支持机械臂维护测试工作流")

    def workflow(self, _workflow_id: str) -> dict[str, Any]:
        raise BookArmApiError("takeout 后端不支持机械臂维护测试工作流")

    def continue_workflow(self, _workflow_id: str) -> dict[str, Any]:
        raise BookArmApiError("takeout 后端不支持机械臂维护测试工作流")

    def cancel_workflow(self, _workflow_id: str) -> dict[str, Any]:
        raise BookArmApiError("takeout 后端不支持机械臂维护测试工作流")

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False)
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token
        connection = _UnixSocketHTTPConnection(self.socket_path, self.timeout_seconds)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(128 * 1024 + 1)
        except (OSError, http.client.HTTPException) as error:
            raise BookArmApiError(f"BookArm takeout 本机服务不可达：{error}") from error
        finally:
            connection.close()
        if response.status >= 400:
            detail = raw[:1024].decode("utf-8", errors="replace")
            raise BookArmApiError(
                f"BookArm takeout 服务拒绝请求（HTTP {response.status}）：{detail}"
            )
        if len(raw) > 128 * 1024:
            raise BookArmApiError("BookArm takeout 服务响应超过大小上限")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BookArmApiError("BookArm takeout 服务返回了无效 JSON") from error
        if not isinstance(value, dict):
            raise BookArmApiError("BookArm takeout 服务响应必须是 JSON 对象")
        return value
