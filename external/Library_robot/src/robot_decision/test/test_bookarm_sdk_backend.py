import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from robot_decision.bookarm_client import BookArmApiError
from robot_decision.bookarm_sdk_backend import BookArmSdkBackend, BookArmSdkConfig


class ValueObject:
    def __init__(self, value=None, **values) -> None:
        self.value = value
        self.values = values


class FakeSdk:
    def __init__(self) -> None:
        self.request = None
        self.return_request = None
        self.disable_calls = 0
        self.disable_error: Exception | None = None

    def take_book(self, request):
        self.request = request
        return SimpleNamespace(id="SDK-TASK-1")

    def return_book(self, request):
        self.return_request = request
        return SimpleNamespace(id="SDK-RETURN-TASK-1")

    def wait(self, _task_id):
        if self.return_request is not None:
            names = ("lift_move", "arm_debug_pose", "book_stand_grasp", "vision_target", "shelf_place")
        else:
            names = ("lift_move", "vision_target", "arm_debug_pose", "book_grasp", "book_stand_place", "arm_standby")
        stages = [
            SimpleNamespace(
                name=name,
                status=SimpleNamespace(value="succeeded"),
                message="ok",
                details={"capture_id": "CAP-1"} if name == "vision_target" else {},
            )
            for name in names
        ]
        return SimpleNamespace(state=SimpleNamespace(value="succeeded"), stages=stages, error=None)

    def disable_arm(self, *, confirmed: bool):
        assert confirmed is True
        self.disable_calls += 1
        if self.disable_error is not None:
            raise self.disable_error
        return {
            "already_disabled": False,
            "disable_command_sent": True,
            "moved_to_disable_pose": True,
        }


class FakeRuntime:
    def __init__(self) -> None:
        self.started = False
        self.sdk = FakeSdk()
        self._services = ()

    def start(self) -> None:
        self.started = True


@dataclass(frozen=True)
class FakeCameraConfig:
    serial: str = ""
    width: int = 1280


def _backend(tmp_path: Path) -> tuple[BookArmSdkBackend, FakeRuntime, Path]:
    sdk_root = tmp_path / "bookarm"
    (sdk_root / "src/bookarm/api").mkdir(parents=True)
    (sdk_root / "src/bookarm/api/sdk.py").touch()
    slots = tmp_path / "slots.json"
    camera = tmp_path / "camera.json"
    rod = tmp_path / "rod"
    lift = tmp_path / "lift"
    for path in (slots, camera, rod, lift):
        path.touch()
    spine_root = tmp_path / "spines"
    template_dir = spine_root / "TSK-SDK-001"
    template_dir.mkdir(parents=True)
    template = template_dir / "spine.jpg"
    template.write_bytes(b"image")
    runtime = FakeRuntime()
    backend = BookArmSdkBackend(
        BookArmSdkConfig(
            sdk_root,
            slots,
            camera,
            spine_storage_root=spine_root,
            rod_device_path=rod,
            lift_device_path=lift,
        ),
        runtime_factory=lambda **_kwargs: runtime,
    )
    symbols = {
        "TakeBookRequest": lambda **values: SimpleNamespace(**values),
        "TakeBookStage": SimpleNamespace(ARM_STANDBY="arm_standby"),
        "BookIdentity": ValueObject,
        "SpineTemplate": ValueObject,
        "BookStandSlot": ValueObject,
    }
    backend._load_sdk_symbols = lambda: symbols
    return backend, runtime, template


def test_sdk_backend_reports_takeout_and_putback_capabilities(tmp_path: Path) -> None:
    backend, _runtime, _template = _backend(tmp_path)

    capabilities = backend.capabilities()

    assert capabilities["bookWorkflowModes"] == ["takeout", "putback"]
    assert capabilities["liftHomeSensor"] is False
    assert capabilities["liftAbsolutePositionFeedback"] is True
    assert capabilities["liftTargetArrivalFeedback"] is True
    assert capabilities["bookHandoffFeedback"] is True


def test_sdk_backend_executes_a_fully_bounded_take_book_request(tmp_path: Path) -> None:
    backend, runtime, template = _backend(tmp_path)

    workflow = backend.start_take_book(
        {
            "taskId": "TSK-SDK-001",
            "bookId": "CPY-001",
            "targetHeightMm": 620,
            "spineTemplatePath": str(template),
            "destinationSlot": 1,
            "liftSpeedLevel": 1,
        }
    )
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        workflow = backend.workflow(workflow["id"])
        if not workflow["active"]:
            break
        time.sleep(0.01)

    assert runtime.started is True
    assert workflow["active"] is False
    assert workflow["step_index"] == workflow["step_count"] == 6
    assert workflow["inventory_disposition"] == "off_shelf"
    assert workflow["arm_auto_disabled"] is True
    assert workflow["arm_moved_to_disable_pose"] is True
    assert runtime.sdk.disable_calls == 1
    assert runtime.sdk.request.target_height_mm == 620
    assert runtime.sdk.request.lift_speed_level == 1


def test_sdk_backend_loads_camera_device_config_separately_from_extrinsics(tmp_path: Path) -> None:
    backend, runtime, _template = _backend(tmp_path)
    camera_config = tmp_path / "camera-device.json"
    camera_config.write_text('{"serial":"262322073961","width":1280}', encoding="utf-8")
    backend.config = BookArmSdkConfig(
        sdk_root=backend.config.sdk_root,
        book_stand_slots_file=backend.config.book_stand_slots_file,
        camera_calibration_file=backend.config.camera_calibration_file,
        camera_device_config_file=camera_config,
        spine_storage_root=backend.config.spine_storage_root,
        rod_device_path=backend.config.rod_device_path,
        lift_device_path=backend.config.lift_device_path,
    )
    captured = {}
    backend._runtime_factory = lambda **values: captured.update(values) or runtime
    backend._load_sdk_symbols = lambda: {
        "TakeBookStage": SimpleNamespace(ARM_STANDBY="arm_standby"),
        "D435BookSpineLocatorConfig": FakeCameraConfig,
    }

    backend.prepare_take_book({})

    assert captured["camera_calibration_path"] == backend.config.camera_calibration_file
    assert captured["camera_config"] == FakeCameraConfig(serial="262322073961", width=1280)


def test_sdk_backend_executes_a_bounded_return_book_request(tmp_path: Path) -> None:
    backend, runtime, _template = _backend(tmp_path)
    pickup_slots = tmp_path / "pickup-slots.json"
    pickup_slots.touch()
    backend.config = BookArmSdkConfig(
        sdk_root=backend.config.sdk_root,
        book_stand_slots_file=backend.config.book_stand_slots_file,
        camera_calibration_file=backend.config.camera_calibration_file,
        book_stand_pickup_slots_file=pickup_slots,
        spine_storage_root=backend.config.spine_storage_root,
        rod_device_path=backend.config.rod_device_path,
        lift_device_path=backend.config.lift_device_path,
    )
    backend._load_sdk_symbols = lambda: {
        "ReturnBookRequest": lambda **values: SimpleNamespace(**values),
        "ReturnBookStage": SimpleNamespace(SHELF_PLACE="shelf_place"),
        "BookIdentity": ValueObject,
        "SpineTemplate": ValueObject,
        "BookStandSlot": ValueObject,
        "D435BookSpineLocatorConfig": FakeCameraConfig,
    }
    template_dir = backend.config.spine_storage_root / "TSK-RETURN-001"
    template_dir.mkdir(parents=True)
    template = template_dir / "left.jpg"
    template.write_bytes(b"left-neighbor")

    workflow = backend.start_return_book(
        {
            "taskId": "TSK-RETURN-001",
            "bookId": "CPY-001",
            "targetHeightMm": 620,
            "leftNeighborSpineTemplatePath": str(template),
            "sourceSlot": 2,
            "liftSpeedLevel": 1,
        }
    )
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        workflow = backend.workflow(workflow["id"])
        if not workflow["active"]:
            break
        time.sleep(0.01)

    assert runtime.started is True
    assert workflow["operation"] == "return"
    assert workflow["active"] is False
    assert workflow["step_index"] == workflow["step_count"] == 5
    assert workflow["inventory_disposition"] == "on_shelf"
    assert workflow["arm_auto_disabled"] is True
    assert runtime.sdk.disable_calls == 1
    assert runtime.sdk.return_request.target_height_mm == 620
    assert runtime.sdk.return_request.source_slot.value == 2
    assert runtime.sdk.return_request.left_neighbor_spine_template.value.name == "spine-template.jpg"


def test_sdk_backend_marks_a_completed_workflow_failed_when_safe_disable_is_unverified(
    tmp_path: Path,
) -> None:
    backend, runtime, template = _backend(tmp_path)
    runtime.sdk.disable_error = RuntimeError("disable pose feedback timeout")

    workflow = backend.start_take_book(
        {
            "taskId": "TSK-SDK-001",
            "bookId": "CPY-001",
            "targetHeightMm": 620,
            "spineTemplatePath": str(template),
            "destinationSlot": 1,
            "liftSpeedLevel": 1,
        }
    )
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        workflow = backend.workflow(workflow["id"])
        if not workflow["active"]:
            break
        time.sleep(0.01)

    assert workflow["inventory_disposition"] == "off_shelf"
    assert workflow["arm_auto_disabled"] is False
    assert workflow["safety_stop_state"] == "arm_disable_failed"
    assert "安全失能失败" in workflow["message"]


def test_sdk_backend_rejects_unknown_camera_device_fields_during_preflight(tmp_path: Path) -> None:
    backend, _runtime, _template = _backend(tmp_path)
    camera_config = tmp_path / "camera-device.json"
    camera_config.write_text('{"matrix":[]}', encoding="utf-8")
    backend.config = BookArmSdkConfig(
        sdk_root=backend.config.sdk_root,
        book_stand_slots_file=backend.config.book_stand_slots_file,
        camera_calibration_file=backend.config.camera_calibration_file,
        camera_device_config_file=camera_config,
        spine_storage_root=backend.config.spine_storage_root,
        rod_device_path=backend.config.rod_device_path,
        lift_device_path=backend.config.lift_device_path,
    )
    backend._load_sdk_symbols = lambda: {
        "TakeBookStage": SimpleNamespace(ARM_STANDBY="arm_standby"),
        "D435BookSpineLocatorConfig": FakeCameraConfig,
    }

    with pytest.raises(BookArmApiError, match="未知字段"):
        backend.prepare_take_book({})
