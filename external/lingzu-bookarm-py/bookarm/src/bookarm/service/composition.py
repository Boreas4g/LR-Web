"""Service composition root combining the take-book and return-book workflows.

The stock factories in ``bookarm.infrastructure.factory`` each load only one
side of the book-stand calibration: take-book placement slots or return-book
pickup slots. The HTTP service exposes both workflows on one runtime, so this
module hand-assembles a combined runtime following those factories.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Optional

from bookarm.api.sdk import BookArmSDK
from bookarm.application.task_manager import TaskManager
from bookarm.application.use_cases.mid_return import MidReturnBookUseCase
from bookarm.infrastructure.vehicle_align import VehicleAlignClient
from bookarm.application.use_cases.return_book import ReturnBookUseCase
from bookarm.application.use_cases.return_book_to_end import ReturnBookToEndUseCase
from bookarm.application.use_cases.take_book import TakeBookUseCase
from bookarm.infrastructure.vehicle_align import VehicleAlignClient
from bookarm.devices.arm.service import ArmDeviceConfig, ArmService
from bookarm.devices.camera.service import D435BookSpineLocator, D435BookSpineLocatorConfig
from bookarm.devices.lift.service import AimLiftService, LiftDeviceConfig
from bookarm.devices.rod.service import RodDeviceConfig, RodMotorService
from bookarm.domain.plans.mid_return import MidReturnMotionPlan
from bookarm.domain.plans.return_book import ReturnBookMotionPlan
from bookarm.domain.plans.take_book import TakeBookMotionPlan
from bookarm.infrastructure.config.calibration import (
    load_book_stand_pickup_slots,
    load_book_stand_slots,
)
from bookarm.infrastructure.config.profiles import (
    load_mid_return_motion_plan,
    load_return_book_motion_plan,
    load_take_book_motion_plan,
)
from bookarm.runtime.bookarm_runtime import BookArmRuntime
from bookarm.runtime.reservation import RuntimeReservation


@dataclass(frozen=True)
class CombinedRuntime:
    """The combined take/return runtime plus its shared device services.

    The device references let the HTTP layer wire interactive consumers such
    as the spine studio onto the same camera and lift the workflows use.
    """

    runtime: BookArmRuntime
    arm: ArmService
    camera: D435BookSpineLocator
    lift: AimLiftService
    rod: RodMotorService
    vehicle: "VehicleAlignClient" 


def _load_lift_config() -> LiftDeviceConfig:
    """Load the lift device config from config/devices/lift.json when present."""

    path = Path(__file__).resolve().parents[3] / "config" / "devices" / "lift.json"
    if not path.is_file():
        return LiftDeviceConfig()
    payload = json.loads(path.read_text(encoding="utf-8"))
    allowed = {field.name for field in LiftDeviceConfig.__dataclass_fields__.values()}
    values = {key: value for key, value in payload.items() if key in allowed}
    return replace(LiftDeviceConfig(), **values)


def _load_camera_config() -> D435BookSpineLocatorConfig:
    """Load the camera device config from config/devices/camera.json when present."""

    path = Path(__file__).resolve().parents[3] / "config" / "devices" / "camera.json"
    if not path.is_file():
        return D435BookSpineLocatorConfig()
    payload = json.loads(path.read_text(encoding="utf-8"))
    allowed = {field.name for field in D435BookSpineLocatorConfig.__dataclass_fields__.values()}
    values = {key: value for key, value in payload.items() if key in allowed}
    return replace(D435BookSpineLocatorConfig(), **values)


def create_combined_runtime(
    *,
    book_stand_slots_path: Path,
    book_stand_pickup_slots_path: Path,
    camera_calibration_path: Path,
    take_book_profile_path: Optional[Path] = None,
    return_book_profile_path: Optional[Path] = None,
    mid_return_profile_path: Optional[Path] = None,
    task_manager: Optional[TaskManager] = None,
    arm_feedback_max_age_s: Optional[float] = None,
    camera_debug_capture_dir: Optional[Path] = None,
    arm_inertia_config_path: Optional[Path] = None,
    arm_thermal_limit_c: Optional[float] = None,
    arm_thermal_check_interval_s: Optional[float] = None,
    arm_telemetry_dir: Optional[Path] = None,
    arm_telemetry_interval_s: Optional[float] = None,
    arm_telemetry_retention_days: Optional[float] = None,
) -> CombinedRuntime:
    """Build one runtime serving the full take-book and return-book workflows.

    Both use cases share one device set, one hardware reservation and one task
    manager. Unlike the stock factories, both calibration files are always
    loaded so every stop stage stays available, and the runtime starts every
    device service for the same reason. The returned runtime is deliberately
    not started here: enable physical hardware only after the caller completes
    its own operator safety checks.
    """

    manager = task_manager or TaskManager()
    take_plan: TakeBookMotionPlan
    if take_book_profile_path is None:
        take_plan = TakeBookMotionPlan()
    else:
        take_plan = load_take_book_motion_plan(Path(take_book_profile_path))
    return_plan: ReturnBookMotionPlan
    if return_book_profile_path is None:
        return_plan = ReturnBookMotionPlan()
    else:
        return_plan = load_return_book_motion_plan(Path(return_book_profile_path))
    mid_return_plan: MidReturnMotionPlan
    if mid_return_profile_path is None:
        mid_return_plan = MidReturnMotionPlan()
    else:
        mid_return_plan = load_mid_return_motion_plan(Path(mid_return_profile_path))
    arm_config = ArmDeviceConfig()
    if arm_feedback_max_age_s is not None:
        # Loaded mini-PCs occasionally stall the feedback keepalive for a few
        # hundred milliseconds; joint 1 is requested first in every Type-24
        # burst, so its sample is always the oldest and trips the stock 0.35 s
        # limit first. A slightly larger limit still catches dead feedback
        # quickly while tolerating scheduling jitter. It must stay above the
        # keepalive interval, which ArmService validates as well.
        max_age_s = float(arm_feedback_max_age_s)
        if max_age_s <= arm_config.feedback_keepalive_interval_s:
            raise ValueError(
                "arm_feedback_max_age_s must be greater than the keepalive interval %.3fs"
                % arm_config.feedback_keepalive_interval_s
            )
        arm_config = replace(arm_config, feedback_max_age_s=max_age_s)
    if arm_inertia_config_path is not None:
        arm_config = replace(
            arm_config, inertia_config_path=Path(arm_inertia_config_path)
        )
    if arm_thermal_limit_c is not None:
        arm_config = replace(arm_config, thermal_limit_c=float(arm_thermal_limit_c))
    if arm_thermal_check_interval_s is not None:
        arm_config = replace(
            arm_config, thermal_check_interval_s=float(arm_thermal_check_interval_s)
        )
    if arm_telemetry_dir is not None:
        arm_config = replace(arm_config, telemetry_dir=Path(arm_telemetry_dir))
    if arm_telemetry_interval_s is not None:
        arm_config = replace(
            arm_config, telemetry_interval_s=float(arm_telemetry_interval_s)
        )
    if arm_telemetry_retention_days is not None:
        arm_config = replace(
            arm_config, telemetry_retention_days=float(arm_telemetry_retention_days)
        )
    arm = ArmService(arm_config)
    camera = D435BookSpineLocator(
        replace(
            _load_camera_config(),
            calibration_path=Path(camera_calibration_path),
            debug_capture_dir=(
                Path(camera_debug_capture_dir) if camera_debug_capture_dir is not None else None
            ),
        )
    )
    lift = AimLiftService(_load_lift_config())
    rod = RodMotorService(RodDeviceConfig())
    reservation = RuntimeReservation()
    vehicle_align = VehicleAlignClient()
    pickup_slots = load_book_stand_pickup_slots(Path(book_stand_pickup_slots_path))
    sdk = BookArmSDK(
        task_manager=manager,
        take_book=TakeBookUseCase(
            task_manager=manager,
            reservation=reservation,
            lift=lift,
            vision=camera,
            arm=arm,
            rod=rod,
            slots=load_book_stand_slots(Path(book_stand_slots_path)),
            plan=take_plan,
            vehicle_align=vehicle_align,
        ),
        return_book=ReturnBookUseCase(
            task_manager=manager,
            reservation=reservation,
            lift=lift,
            vision=camera,
            arm=arm,
            rod=rod,
            pickup_slots=pickup_slots,
            plan=return_plan,
            vehicle_align=vehicle_align,
        ),
        return_book_to_end=ReturnBookToEndUseCase(manager),
        mid_return=MidReturnBookUseCase(
            task_manager=manager,
            reservation=reservation,
            lift=lift,
            vision=camera,
            arm=arm,
            rod=rod,
            pickup_slots=pickup_slots,
            plan=mid_return_plan,
            vehicle_align=vehicle_align,
        ),
    )
    return CombinedRuntime(
        runtime=BookArmRuntime(sdk, (arm, camera, lift, rod)),
        arm=arm,
        camera=camera,
        lift=lift,
        rod=rod,
        vehicle=vehicle_align,
    )
