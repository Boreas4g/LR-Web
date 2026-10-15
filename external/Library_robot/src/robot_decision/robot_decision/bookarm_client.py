"""Small localhost client for the existing MotorStudio book-arm WebUI.

The ROS Gateway owns fleet-facing authorization.  This client only talks to
the WebUI on the same robot and never exposes its hardware API or credentials
to MQTT, the control station, or a browser.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class BookArmApiError(RuntimeError):
    """A local book-arm service request was unavailable or rejected."""


@dataclass(frozen=True)
class BookArmHealth:
    available: bool
    arm_ready: bool
    lift_ready: bool
    camera_ready: bool
    rod_ready: bool
    message: str
    arm_connected: bool = False
    arm_feedback_healthy: bool = False
    arm_can_fps: float = 0.0
    arm_feedback_reason: str = "机械臂状态尚未读取"
    arm_enabled: bool = False
    arm_state: str = "fault"
    # The current WebUI only supports relative lift motion. Keep this false
    # until it exposes a homed absolute-position and arrival contract.
    lift_absolute_positioning_ready: bool = False
    # Recovery needs more than a successful command receipt: both mechanisms
    # must independently report their reference pose. The current lift has no
    # home input or position feedback, so this remains false by design.
    mechanism_home_ready: bool = False


class BookArmClient:
    """Call the existing WebUI API without bypassing its worker safety model."""

    def __init__(
        self,
        api_url: str,
        timeout_seconds: float,
        *,
        rod_device_path: str = "/dev/rodmotor",
        lift_device_path: str = "/dev/lift_port",
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.rod_device_path = Path(rod_device_path)
        self.lift_device_path = Path(lift_device_path)

    def health(self) -> BookArmHealth:
        """Return a conservative readiness snapshot without issuing motion."""
        try:
            health = self._request("GET", "/health")
            state = self._request("GET", "/state")
            camera = self._request("GET", "/vision/camera/status")
        except BookArmApiError as error:
            return BookArmHealth(False, False, False, False, False, str(error))

        service = state.get("state", {}).get("service", {}) if isinstance(state, dict) else {}
        arm = state.get("state", {}).get("arm", {}) if isinstance(state, dict) else {}
        camera_state = camera.get("camera", {}) if isinstance(camera, dict) else {}
        started = bool(health.get("gateway_started")) and bool(service.get("started"))
        arm_status = arm.get("status") if isinstance(arm.get("status"), dict) else {}
        joint_enabled = arm_status.get("joint_enabled") if isinstance(arm_status, dict) else []
        feedback = arm.get("feedback") if isinstance(arm.get("feedback"), dict) else {}
        can_fps = feedback.get("can_fps", arm.get("can_fps"))
        feedback_healthy = bool(feedback.get("healthy"))
        feedback_reason = str(
            feedback.get("reason") or "未收到机械臂 CAN 反馈健康状态"
        )[:240]
        arm_feedback_ready = (
            isinstance(joint_enabled, list)
            and len(joint_enabled) >= 6
            and all(bool(value) for value in joint_enabled[:6])
            and isinstance(can_fps, (int, float))
            and float(can_fps) >= 1.0
            and feedback_healthy
        )
        arm_ready = (
            bool(arm.get("connected"))
            and bool(arm.get("enabled"))
            and arm_feedback_ready
            and not bool(arm.get("emergency_stop_pending") or arm.get("emergency_stop_lockout"))
        )
        arm_enabled = bool(arm.get("enabled"))
        arm_locked_out = bool(arm.get("emergency_stop_pending") or arm.get("emergency_stop_lockout"))
        arm_link_healthy = (
            feedback_healthy
            and isinstance(can_fps, (int, float))
            and float(can_fps) >= 1.0
        )
        if arm_ready:
            arm_state = "ready"
        elif (
            started
            and bool(arm.get("connected"))
            and not arm_enabled
            and not arm_locked_out
            and arm_link_healthy
        ):
            arm_state = "standby"
        else:
            arm_state = "fault"
        rod_ready = self.rod_device_path.exists()
        lift_ready = self.lift_device_path.exists()
        camera_ready = bool(camera_state.get("connected")) and not bool(camera_state.get("busy"))
        if not started:
            message = "机械臂本机服务未就绪"
        # Check fixed peripherals first so one arm fault does not hide another
        # missing BookArm device from the operator.
        elif not rod_ready:
            message = f"未检测到推书杆设备：{self.rod_device_path}"
        elif not lift_ready:
            message = f"未检测到升降台设备：{self.lift_device_path}"
        elif not camera_ready:
            message = "未检测到可用 RealSense 相机"
        elif arm_locked_out:
            message = "机械臂急停仍在处理或处于安全锁定"
        elif not bool(arm.get("connected")):
            message = "机械臂开机连接未确认；请检查本机服务、供电和 CAN 总线"
        elif bool(arm.get("connected")) and not feedback_healthy:
            message = f"机械臂本机通信已打开，但 CAN 反馈未就绪：{feedback_reason}"
        elif bool(arm.get("connected")) and bool(arm.get("enabled")):
            message = "机械臂 CAN 反馈无效，禁止执行运动；请检查供电、线束和 CAN 总线"
        elif not arm_enabled:
            message = "机械臂已连接并保持失能，任务启动后将自动使能并再次校验 CAN 反馈"
        else:
            message = "取还书硬件就绪"
        return BookArmHealth(
            started,
            arm_ready,
            lift_ready,
            camera_ready,
            rod_ready,
            message,
            arm_connected=bool(arm.get("connected")),
            arm_feedback_healthy=feedback_healthy,
            arm_can_fps=float(can_fps) if isinstance(can_fps, (int, float)) else 0.0,
            arm_feedback_reason=feedback_reason,
            arm_enabled=arm_enabled,
            arm_state=arm_state,
        )

    def start_workflow(self, mode: str, *, auto: bool) -> dict[str, Any]:
        response = self._request("POST", "/workflows", {"mode": mode, "auto": bool(auto)})
        workflow = response.get("workflow") if isinstance(response, dict) else None
        if not isinstance(workflow, dict) or not isinstance(workflow.get("id"), str):
            raise BookArmApiError("机械臂服务未返回有效工作流")
        return workflow

    def prepare_arm(self) -> BookArmHealth:
        """Connect and enable the arm only after the Gateway safety command."""
        state = self._request("GET", "/state").get("state", {})
        arm = state.get("arm", {}) if isinstance(state, dict) else {}
        if not isinstance(arm, dict):
            raise BookArmApiError("机械臂服务状态无效")
        if arm.get("emergency_stop_pending") or arm.get("emergency_stop_lockout"):
            raise BookArmApiError("机械臂急停仍在处理或处于安全锁定")
        if not arm.get("connected"):
            self._require_succeeded_job(
                self._request("POST", "/arm/connect", {"can_name": "can0", "wait": True, "timeout_s": 8.0}),
                "机械臂连接",
            )
        state = self._request("GET", "/state").get("state", {})
        arm = state.get("arm", {}) if isinstance(state, dict) else {}
        if not isinstance(arm, dict) or not arm.get("connected"):
            raise BookArmApiError("机械臂连接未确认")
        if not arm.get("enabled"):
            self._require_succeeded_job(
                self._request("POST", "/arm/enable", {"wait": True, "timeout_s": 8.0}),
                "机械臂使能",
            )
        health = self.health()
        if not health.arm_ready:
            raise BookArmApiError(health.message)
        return health

    def workflow(self, workflow_id: str) -> dict[str, Any]:
        response = self._request("GET", f"/workflows/{workflow_id}")
        workflow = response.get("workflow") if isinstance(response, dict) else None
        if not isinstance(workflow, dict):
            raise BookArmApiError("机械臂服务未返回工作流状态")
        return workflow

    def continue_workflow(self, workflow_id: str) -> dict[str, Any]:
        response = self._request("POST", f"/workflows/{workflow_id}/next", {})
        workflow = response.get("workflow") if isinstance(response, dict) else None
        if not isinstance(workflow, dict):
            raise BookArmApiError("机械臂服务未确认下一工作步骤")
        return workflow

    def cancel_workflow(self, workflow_id: str) -> dict[str, Any]:
        """Cancel locally and retain the latched physical handoff state."""

        response = self._request("POST", f"/workflows/{workflow_id}/cancel", {})
        workflow = response.get("workflow") if isinstance(response, dict) else None
        return dict(workflow) if isinstance(workflow, dict) else {}

    def emergency_stop(self) -> None:
        self._request("POST", "/arm/safety/emergency-stop", {})

    def safe_stop(self) -> None:
        """Request both local actuator stop paths without treating HTTP as proof.

        The Gateway uses this after a local action failure or timeout.  Keep
        attempting the lift stop if arm cancellation fails, then return one
        bounded error that can safely be reported to the control station.
        """

        errors: list[str] = []
        for path, label in (("/arm/cancel-motion", "机械臂停止"), ("/lift/stop", "升降台停止")):
            try:
                self._request("POST", path, {"wait": True, "timeout_s": 4.0})
            except BookArmApiError as error:
                errors.append(f"{label}失败：{error}")
        if errors:
            raise BookArmApiError("；".join(errors))

    def move_lift(self, distance_cm: float) -> dict[str, Any]:
        # Waiting for the tagged Worker receipt is meaningful only when its
        # terminal state is checked. A REST 200 merely means the API returned
        # a Job object; the Modbus operation inside that Job may have failed.
        self._require_succeeded_job(
            self._request("POST", "/lift/connect", {"wait": True, "timeout_s": 4.0}),
            "升降台连接",
        )
        return self._require_succeeded_job(
            self._request(
                "POST",
                "/lift/move-distance",
                {"distance_cm": float(distance_cm), "wait": True, "timeout_s": 4.0},
            ),
            "升降台移动命令",
        )

    @staticmethod
    def _require_succeeded_job(response: dict[str, Any], operation: str) -> dict[str, Any]:
        """Turn a terminal WebUI Job failure into a fleet-visible failure."""

        job = response.get("job") if isinstance(response, dict) else None
        if not isinstance(job, dict):
            raise BookArmApiError(f"{operation}未返回有效任务回执")
        status = job.get("status")
        if status != "succeeded":
            error = job.get("error")
            detail = str(error) if error else f"任务状态为 {status or 'unknown'}"
            raise BookArmApiError(f"{operation}失败：{detail}")
        result = job.get("result")
        return dict(result) if isinstance(result, dict) else {}

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(f"{self.api_url}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:300]
            raise BookArmApiError(f"机械臂服务拒绝请求（HTTP {error.code}）：{detail}") from error
        except (URLError, OSError) as error:
            raise BookArmApiError(f"机械臂本机服务不可达：{error}") from error
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BookArmApiError("机械臂服务返回了无效响应") from error
        if not isinstance(parsed, dict):
            raise BookArmApiError("机械臂服务响应必须是 JSON 对象")
        return parsed
