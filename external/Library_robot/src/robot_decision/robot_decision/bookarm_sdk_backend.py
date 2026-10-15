"""Lazy, process-local adapter for the independent BookArm Python SDK.

The module deliberately does not import BookArm at import time. A vehicle must
explicitly select this backend and provide deployment-only calibration before
the first workflow can own CAN, camera, or serial devices.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

from robot_decision.bookarm_client import BookArmApiError, BookArmHealth


_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


@dataclass(frozen=True)
class BookArmSdkConfig:
    sdk_root: Path
    book_stand_slots_file: Path
    camera_calibration_file: Path
    camera_device_config_file: Path | None = None
    take_book_profile_file: Path | None = None
    book_stand_pickup_slots_file: Path | None = None
    return_book_profile_file: Path | None = None
    spine_storage_root: Path = Path("/var/lib/lr-robot/book-spines")
    rod_device_path: Path = Path("/dev/rodmotor")
    lift_device_path: Path = Path("/dev/lift_port")


@dataclass
class _SdkWorkflow:
    workflow_id: str
    operation: str = "borrow"
    active: bool = True
    waiting: bool = True
    step_index: int = 0
    step_count: int = 6
    step_name: str = "sdk_preflight"
    message: str = "BookArm SDK 任务已进入本机执行队列"
    mechanism_motion_started: bool = False
    inventory_disposition: str = "on_shelf"
    safety_stop_state: str | None = None
    capture_result: dict[str, Any] | None = None
    recognition_result: dict[str, Any] | None = None
    stages: list[dict[str, Any]] = field(default_factory=list)
    cancel_requested: bool = False
    arm_auto_disabled: bool = False
    arm_moved_to_disable_pose: bool = False

    def snapshot(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.workflow_id,
            "operation": self.operation,
            "active": self.active,
            "waiting": self.waiting,
            "step_index": self.step_index,
            "step_count": self.step_count,
            "step_name": self.step_name,
            "message": self.message[:240],
            "mechanism_motion_started": self.mechanism_motion_started,
            "inventory_disposition": self.inventory_disposition,
            "stages": list(self.stages),
            "arm_auto_disabled": self.arm_auto_disabled,
            "arm_moved_to_disable_pose": self.arm_moved_to_disable_pose,
        }
        if self.safety_stop_state:
            value["safety_stop_state"] = self.safety_stop_state
        if self.capture_result is not None:
            value["capture_result"] = dict(self.capture_result)
        if self.recognition_result is not None:
            value["recognition_result"] = dict(self.recognition_result)
        return value


class BookArmSdkBackend:
    """Execute one SDK take/return workflow at a time on a background thread."""

    def __init__(
        self,
        config: BookArmSdkConfig,
        *,
        runtime_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self._runtime_factory = runtime_factory
        self._runtime: Any | None = None
        self._runtime_operation: str | None = None
        self._lock = threading.RLock()
        self._workflows: dict[str, _SdkWorkflow] = {}
        self._active_workflow_id: str | None = None

    def capabilities(self) -> dict[str, Any]:
        return {
            "schemaVersion": "lr-vehicle-hardware-v1",
            "profile": "LR-01-bookarm-sdk-v1",
            "navigation": True,
            "mapSwitching": True,
            "manualJog": True,
            "softwareStop": False,
            "liftRelativeMotion": False,
            # AIM uses a calibrated absolute coordinate and measured encoder
            # arrival, but the SDK does not expose a separate home switch.
            "liftHomeSensor": False,
            "liftAbsolutePositionFeedback": True,
            "liftTargetArrivalFeedback": True,
            "armMotion": True,
            "armFeedback": True,
            "armHomeFeedback": True,
            "rodMotion": True,
            "cameraCapture": True,
            # The monitored gripper detects sustained object contact and the
            # workflow verifies every subsequent arm arrival.
            "bookHandoffFeedback": True,
            "bookWorkflowModes": ["takeout", "putback"],
        }

    def health(self) -> BookArmHealth:
        issues = self.configuration_issues()
        if issues:
            return BookArmHealth(False, False, False, False, False, "; ".join(issues))
        runtime_started = bool(self._runtime is not None and getattr(self._runtime, "started", False))
        arm_status: dict[str, Any] | None = None
        if runtime_started:
            for service in tuple(getattr(self._runtime, "_services", ())):
                reader = getattr(service, "health", None)
                if not callable(reader):
                    continue
                try:
                    candidate = reader()
                except Exception:
                    continue
                if isinstance(candidate, dict) and "healthy_feedback" in candidate:
                    arm_status = candidate
                    break
        arm_feedback_healthy = bool(arm_status and arm_status.get("healthy_feedback") is True)
        arm_enabled = bool(arm_status and arm_status.get("enabled") is True)
        arm_ready = bool(arm_feedback_healthy and arm_enabled)
        if arm_ready:
            arm_state = "ready"
        elif not runtime_started or (
            arm_status is not None and not arm_enabled and arm_feedback_healthy
        ):
            arm_state = "standby"
        else:
            arm_state = "fault"
        feedback_reason = (
            str(arm_status.get("reason") or "SDK 机械臂反馈正常")[:240]
            if arm_status is not None
            else "等待 SDK 机械臂实时反馈"
        )
        return BookArmHealth(
            True,
            arm_ready,
            self.config.lift_device_path.exists(),
            True,
            self.config.rod_device_path.exists(),
            (
                "BookArm SDK 机械臂已使能且反馈正常"
                if arm_ready
                else "BookArm SDK 机械臂已安全失能，等待任务"
                if arm_state == "standby" and runtime_started
                else "BookArm SDK 配置已就绪，等待任务时连接硬件"
                if arm_state == "standby"
                else f"BookArm SDK 机械臂反馈异常：{feedback_reason}"
            ),
            arm_connected=arm_status is not None,
            arm_feedback_healthy=arm_feedback_healthy,
            arm_can_fps=(
                float(arm_status.get("can_fps", 0.0))
                if arm_status is not None and isinstance(arm_status.get("can_fps"), (int, float))
                else 0.0
            ),
            arm_feedback_reason=feedback_reason,
            arm_enabled=arm_enabled,
            arm_state=arm_state,
            lift_absolute_positioning_ready=True,
        )

    def configuration_issues(self) -> list[str]:
        issues: list[str] = []
        if not (self.config.sdk_root / "src" / "bookarm" / "api" / "sdk.py").is_file():
            issues.append("BookArm SDK 根目录无效")
        if not self.config.book_stand_slots_file.is_file():
            issues.append("车载书架槽位标定文件不存在")
        if not self.config.camera_calibration_file.is_file():
            issues.append("相机到车体外参文件不存在")
        if (
            self.config.camera_device_config_file is not None
            and not self.config.camera_device_config_file.is_file()
        ):
            issues.append("D435 设备配置文件不存在")
        if self.config.take_book_profile_file is not None and not self.config.take_book_profile_file.is_file():
            issues.append("取书动作配置文件不存在")
        if self.config.return_book_profile_file is not None and not self.config.return_book_profile_file.is_file():
            issues.append("还书动作配置文件不存在")
        if not self.config.rod_device_path.exists():
            issues.append("推书杆设备不存在")
        if not self.config.lift_device_path.exists():
            issues.append("升降台设备不存在")
        return issues

    def prepare_book_workflow(self, context: dict[str, Any]) -> BookArmHealth:
        """Perform a non-motion configuration preflight before shelf navigation."""

        health = self.health()
        if not health.available:
            raise BookArmApiError(health.message)
        operation = str(context.get("operation", "borrow"))
        if operation not in {"borrow", "return"}:
            raise BookArmApiError("BookArm SDK 操作必须为 borrow 或 return")
        if operation == "return":
            if self.config.book_stand_pickup_slots_file is None:
                raise BookArmApiError("还书缺少车载书架 pickup 槽位标定文件")
            if not self.config.book_stand_pickup_slots_file.is_file():
                raise BookArmApiError("还书车载书架 pickup 槽位标定文件不存在")
        try:
            # Import dependencies and construct the composition root now. The
            # factory only parses calibration and creates service objects; it
            # does not open CAN, camera, RS485, or rod devices until start().
            symbols = self._load_sdk_symbols()
            self._ensure_runtime(symbols, operation)
        except BookArmApiError:
            raise
        except Exception as error:
            raise BookArmApiError(f"BookArm SDK 非动作预检失败：{error}") from error
        return health

    def prepare_take_book(self, context: dict[str, Any]) -> BookArmHealth:
        """Backward-compatible take-book preflight entry point."""

        return self.prepare_book_workflow({**context, "operation": "borrow"})

    def start_take_book(self, context: dict[str, Any]) -> dict[str, Any]:
        return self._start_workflow(context, "borrow")

    def start_return_book(self, context: dict[str, Any]) -> dict[str, Any]:
        return self._start_workflow(context, "return")

    def _start_workflow(self, context: dict[str, Any], operation: str) -> dict[str, Any]:
        try:
            task_id = str(context["taskId"])
            book_id = str(context["bookId"])
            target_height_mm = float(context["targetHeightMm"])
            template_key = "spineTemplatePath" if operation == "borrow" else "leftNeighborSpineTemplatePath"
            spine_template_path = Path(str(context[template_key]))
            destination_slot = int(context.get("destinationSlot", 1))
            source_slot = int(context.get("sourceSlot", 1))
            speed_level = int(context.get("liftSpeedLevel", 1))
        except (KeyError, TypeError, ValueError) as error:
            raise BookArmApiError(f"BookArm SDK 请求参数无效：{error}") from error
        if not _IDENTIFIER_PATTERN.fullmatch(task_id) or not _IDENTIFIER_PATTERN.fullmatch(book_id):
            raise BookArmApiError("BookArm SDK taskId 和 bookId 格式无效")
        if not 10.0 <= target_height_mm <= 1700.0:
            raise BookArmApiError("BookArm SDK 目标高度必须在 10..1700 mm")
        if operation == "borrow" and destination_slot not in {1, 2, 3, 4}:
            raise BookArmApiError("车载书架 destinationSlot 必须为 1..4")
        if operation == "return" and source_slot not in {1, 2, 3}:
            raise BookArmApiError("还书车载书架 sourceSlot 必须为 1..3")
        if operation == "return" and (
            self.config.book_stand_pickup_slots_file is None
            or not self.config.book_stand_pickup_slots_file.is_file()
        ):
            raise BookArmApiError("还书车载书架 pickup 槽位标定文件不可用")
        if speed_level not in {1, 2, 3}:
            raise BookArmApiError("升降台速度档必须为 1..3")
        if not spine_template_path.is_file() or spine_template_path.is_symlink():
            raise BookArmApiError("已校验的本地书脊模板不可用")
        try:
            storage_root = self.config.spine_storage_root.resolve()
            resolved_template = spine_template_path.resolve()
            if resolved_template.parent != (storage_root / task_id).resolve():
                raise BookArmApiError("书脊模板不在车端任务私有缓存目录")
        except OSError as error:
            raise BookArmApiError(f"书脊模板路径校验失败：{error}") from error
        issues = self.configuration_issues()
        if issues:
            raise BookArmApiError("；".join(issues))

        with self._lock:
            if self._active_workflow_id is not None:
                active = self._workflows.get(self._active_workflow_id)
                if active is not None and active.active:
                    raise BookArmApiError("另一个 BookArm SDK 任务仍在执行")
            service_template_path = self._stage_template(task_id, spine_template_path)
            workflow_id = "BA-" + uuid.uuid4().hex
            workflow = _SdkWorkflow(
                workflow_id,
                operation=operation,
                step_count=6 if operation == "borrow" else 5,
                inventory_disposition="on_shelf" if operation == "borrow" else "off_shelf",
            )
            self._workflows[workflow_id] = workflow
            self._active_workflow_id = workflow_id
        worker = threading.Thread(
            target=self._run_workflow,
            args=(
                workflow_id,
                operation,
                book_id,
                target_height_mm,
                service_template_path,
                destination_slot,
                source_slot,
                speed_level,
            ),
            name="lr-bookarm-sdk-task",
            daemon=True,
        )
        worker.start()
        return workflow.snapshot()

    def workflow(self, workflow_id: str) -> dict[str, Any]:
        with self._lock:
            workflow = self._workflows.get(str(workflow_id))
            if workflow is None:
                raise BookArmApiError("未知 BookArm SDK 工作流")
            return workflow.snapshot()

    def cancel_workflow(self, workflow_id: str) -> dict[str, Any]:
        with self._lock:
            workflow = self._workflows.get(str(workflow_id))
            if workflow is None:
                raise BookArmApiError("未知 BookArm SDK 工作流")
            workflow.cancel_requested = True
            workflow.safety_stop_state = "cancel_requested"
            workflow.message = "已请求在安全边界停止；运动中仍须使用现场物理急停"
        self._interrupt_arm_motion()
        return self.workflow(workflow_id)

    def safe_stop(self) -> None:
        self._interrupt_arm_motion()
        raise BookArmApiError("SDK 已请求机械臂保持当前位置；升降台运动中必须使用现场物理急停")

    def emergency_stop(self) -> None:
        self.safe_stop()

    def shutdown(self) -> None:
        """Release devices when the dedicated service is intentionally stopped."""

        runtime = self._runtime
        if runtime is None:
            self._runtime_operation = None
            return
        try:
            self._interrupt_arm_motion()
            runtime.stop()
        finally:
            self._runtime = None
            self._runtime_operation = None

    def _run_workflow(
        self,
        workflow_id: str,
        operation: str,
        book_id: str,
        target_height_mm: float,
        service_template_path: Path,
        destination_slot: int,
        source_slot: int,
        speed_level: int,
    ) -> None:
        try:
            symbols = self._load_sdk_symbols()
            runtime = self._ensure_runtime(symbols, operation)
            runtime.start()
            with self._lock:
                workflow = self._workflows[workflow_id]
                workflow.mechanism_motion_started = True
                workflow.step_name = "lift_move"
                workflow.message = "BookArm SDK 正在执行%s流程" % ("取书" if operation == "borrow" else "还书")
            if operation == "borrow":
                request = symbols["TakeBookRequest"](
                    book=symbols["BookIdentity"](book_id),
                    target_height_mm=target_height_mm,
                    spine_template=symbols["SpineTemplate"](service_template_path),
                    destination_slot=symbols["BookStandSlot"](destination_slot),
                    lift_speed_level=speed_level,
                    stop_after_stage=symbols["TakeBookStage"].ARM_STANDBY,
                )
                task = runtime.sdk.take_book(request)
            else:
                request = symbols["ReturnBookRequest"](
                    book=symbols["BookIdentity"](book_id),
                    target_height_mm=target_height_mm,
                    left_neighbor_spine_template=symbols["SpineTemplate"](service_template_path),
                    source_slot=symbols["BookStandSlot"](source_slot),
                    lift_speed_level=speed_level,
                    stop_after_stage=symbols["ReturnBookStage"].SHELF_PLACE,
                )
                task = runtime.sdk.return_book(request)
            result = runtime.sdk.wait(task.id)
            disable_result: dict[str, Any] | None = None
            disable_error: str | None = None
            if str(getattr(getattr(result, "state", None), "value", "failed")) == "succeeded":
                try:
                    candidate = runtime.sdk.disable_arm(confirmed=True)
                    if not isinstance(candidate, dict) or not (
                        candidate.get("already_disabled") is True
                        or candidate.get("disable_command_sent") is True
                    ):
                        raise RuntimeError("安全失能接口未返回电机已失能的验证结果")
                    disable_result = candidate
                except Exception as error:
                    disable_error = str(error)[:240]
            self._finish_from_result(
                workflow_id,
                result,
                operation,
                disable_result=disable_result,
                disable_error=disable_error,
            )
        except BaseException as error:
            with self._lock:
                workflow = self._workflows[workflow_id]
                workflow.active = False
                workflow.waiting = False
                workflow.safety_stop_state = "failed"
                workflow.message = str(error)[:240]
        finally:
            try:
                service_template_path.unlink(missing_ok=True)
                service_template_path.parent.rmdir()
            except OSError:
                pass
            with self._lock:
                if self._active_workflow_id == workflow_id:
                    self._active_workflow_id = None

    def _stage_template(self, task_id: str, source: Path) -> Path:
        cache_dir = self.config.spine_storage_root / ".sdk-cache" / task_id
        try:
            cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(cache_dir, 0o700)
            destination = cache_dir / ("spine-template" + source.suffix)
            shutil.copyfile(source, destination)
            os.chmod(destination, 0o600)
            return destination
        except OSError as error:
            raise BookArmApiError(f"BookArm SDK 执行副本保存失败：{error}") from error

    def _finish_from_result(
        self,
        workflow_id: str,
        result: Any,
        operation: str,
        *,
        disable_result: dict[str, Any] | None = None,
        disable_error: str | None = None,
    ) -> None:
        stages = []
        succeeded_names: set[str] = set()
        for stage in getattr(result, "stages", []):
            status = str(getattr(getattr(stage, "status", None), "value", "unknown"))
            name = str(getattr(stage, "name", "unknown"))
            details = _json_safe(getattr(stage, "details", {}))
            stages.append({"name": name, "status": status, "message": str(getattr(stage, "message", ""))[:240], "details": details})
            if status == "succeeded":
                succeeded_names.add(name)
        state = str(getattr(getattr(result, "state", None), "value", "failed"))
        error = getattr(result, "error", None)
        with self._lock:
            workflow = self._workflows[workflow_id]
            workflow.active = False
            workflow.waiting = False
            workflow.stages = stages
            workflow.step_index = workflow.step_count if state == "succeeded" else min(len(stages), workflow.step_count)
            workflow.step_name = stages[-1]["name"] if stages else ("sdk_take_book" if operation == "borrow" else "sdk_return_book")
            workflow.arm_auto_disabled = bool(
                disable_result
                and (
                    disable_result.get("already_disabled") is True
                    or disable_result.get("disable_command_sent") is True
                )
            )
            workflow.arm_moved_to_disable_pose = bool(
                disable_result and disable_result.get("moved_to_disable_pose") is True
            )
            if state == "succeeded" and disable_error is None:
                workflow.message = (
                    "BookArm SDK 取书流程已完成，机械臂已安全失能"
                    if operation == "borrow"
                    else "BookArm SDK 还书流程已完成，机械臂已安全失能"
                )
            elif state == "succeeded":
                workflow.message = f"BookArm SDK 流程已完成，但机械臂安全失能失败：{disable_error}"
            else:
                workflow.message = str(error or state)[:240]
            if operation == "borrow":
                if "book_grasp" in succeeded_names:
                    workflow.inventory_disposition = "off_shelf"
                elif any(item["name"] == "book_grasp" for item in stages):
                    workflow.inventory_disposition = "unknown"
            else:
                if "shelf_place" in succeeded_names:
                    workflow.inventory_disposition = "on_shelf"
                elif any(item["name"] == "shelf_place" for item in stages):
                    workflow.inventory_disposition = "unknown"
            if state != "succeeded" or disable_error is not None:
                workflow.safety_stop_state = "arm_disable_failed" if disable_error is not None else state
            for item in stages:
                if item["name"] == "vision_target":
                    workflow.capture_result = dict(item["details"])
                    workflow.recognition_result = dict(item["details"])

    def _ensure_runtime(self, symbols: dict[str, Any], operation: str = "borrow") -> Any:
        with self._lock:
            if self._runtime is not None and getattr(self, "_runtime_operation", "borrow") != operation:
                stopper = getattr(self._runtime, "stop", None)
                if callable(stopper):
                    stopper()
                self._runtime = None
            if self._runtime is None:
                if operation == "borrow":
                    factory = self._runtime_factory or symbols["create_bookarm_runtime"]
                    self._runtime = factory(
                        book_stand_slots_path=self.config.book_stand_slots_file,
                        camera_calibration_path=self.config.camera_calibration_file,
                        take_book_profile_path=self.config.take_book_profile_file,
                        camera_config=self._load_camera_config(symbols),
                        take_book_stop_after_stage=symbols["TakeBookStage"].ARM_STANDBY,
                    )
                else:
                    factory = self._runtime_factory or symbols["create_return_book_runtime"]
                    pickup_slots = self.config.book_stand_pickup_slots_file
                    if pickup_slots is None:
                        raise BookArmApiError("还书缺少车载书架 pickup 槽位标定文件")
                    self._runtime = factory(
                        book_stand_pickup_slots_path=pickup_slots,
                        camera_calibration_path=self.config.camera_calibration_file,
                        return_book_profile_path=self.config.return_book_profile_file,
                        camera_config=self._load_camera_config(symbols),
                        stop_after_stage=symbols["ReturnBookStage"].SHELF_PLACE,
                    )
                self._runtime_operation = operation
            return self._runtime

    def _load_camera_config(self, symbols: dict[str, Any]) -> Any | None:
        path = self.config.camera_device_config_file
        if path is None:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError as error:
            raise BookArmApiError(f"无法读取 D435 设备配置：{error}") from error
        except json.JSONDecodeError as error:
            raise BookArmApiError(f"D435 设备配置不是有效 JSON：{error}") from error
        if not isinstance(payload, dict):
            raise BookArmApiError("D435 设备配置必须是 JSON 对象")
        config_type = symbols["D435BookSpineLocatorConfig"]
        allowed = {item.name for item in fields(config_type)}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise BookArmApiError(f"D435 设备配置包含未知字段：{', '.join(unknown)}")
        values = dict(payload)
        if "calibration_path" in values:
            values["calibration_path"] = Path(values["calibration_path"])
        try:
            return config_type(**values)
        except (TypeError, ValueError) as error:
            raise BookArmApiError(f"D435 设备配置无效：{error}") from error

    def _load_sdk_symbols(self) -> dict[str, Any]:
        source_root = str((self.config.sdk_root / "src").resolve())
        if source_root not in sys.path:
            sys.path.insert(0, source_root)
        try:
            from bookarm.api.requests import ReturnBookRequest, ReturnBookStage, TakeBookRequest, TakeBookStage
            from bookarm.devices.camera import D435BookSpineLocatorConfig
            from bookarm.domain.models.book import BookIdentity, SpineTemplate
            from bookarm.domain.models.shelf import BookStandSlot
            from bookarm.infrastructure import create_bookarm_runtime, create_return_book_runtime
        except ImportError as error:
            raise BookArmApiError(f"无法导入 BookArm SDK：{error}") from error
        return {
            "TakeBookRequest": TakeBookRequest,
            "TakeBookStage": TakeBookStage,
            "ReturnBookRequest": ReturnBookRequest,
            "ReturnBookStage": ReturnBookStage,
            "D435BookSpineLocatorConfig": D435BookSpineLocatorConfig,
            "BookIdentity": BookIdentity,
            "SpineTemplate": SpineTemplate,
            "BookStandSlot": BookStandSlot,
            "create_bookarm_runtime": create_bookarm_runtime,
            "create_return_book_runtime": create_return_book_runtime,
        }

    def _interrupt_arm_motion(self) -> None:
        runtime = self._runtime
        if runtime is None:
            return
        # BookArmRuntime does not yet publish an aggregate interrupt method.
        # Use only the device's public out-of-band interrupt, never its private
        # CAN transport or AIM drive.
        for service in tuple(getattr(runtime, "_services", ())):
            interrupt = getattr(service, "interrupt_motion", None)
            if callable(interrupt):
                try:
                    interrupt()
                except Exception:
                    pass


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, (str, int, float, bool)):
        return enum_value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)
