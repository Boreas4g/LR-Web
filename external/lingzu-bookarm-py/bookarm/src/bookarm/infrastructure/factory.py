"""Independent bookarm composition root for direct device control."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional

from bookarm.api.sdk import BookArmSDK
from bookarm.api.requests import ReturnBookStage, TakeBookStage
from bookarm.application.task_manager import TaskManager
from bookarm.application.use_cases.return_book import ReturnBookUseCase
from bookarm.application.use_cases.return_book_to_end import ReturnBookToEndUseCase
from bookarm.application.use_cases.take_book import TakeBookUseCase
from bookarm.devices.arm.service import ArmDeviceConfig, ArmService
from bookarm.devices.camera.service import D435BookSpineLocator, D435BookSpineLocatorConfig
from bookarm.devices.lift.service import AimLiftService, LiftDeviceConfig
from bookarm.devices.rod.service import RodDeviceConfig, RodMotorService
from bookarm.domain.plans.return_book import ReturnBookMotionPlan
from bookarm.domain.plans.take_book import TakeBookMotionPlan
from bookarm.infrastructure.config.calibration import (
    load_book_stand_pickup_slots,
    load_book_stand_slots,
)
from bookarm.infrastructure.config.profiles import (
    load_return_book_motion_plan,
    load_take_book_motion_plan,
)
from bookarm.runtime.bookarm_runtime import BookArmRuntime
from bookarm.runtime.reservation import RuntimeReservation


def create_bookarm_runtime(
    *,
    book_stand_slots_path: Path,
    camera_calibration_path: Path,
    take_book_profile_path: Optional[Path] = None,
    arm_config: Optional[ArmDeviceConfig] = None,
    camera_config: Optional[D435BookSpineLocatorConfig] = None,
    lift_config: Optional[LiftDeviceConfig] = None,
    rod_config: Optional[RodDeviceConfig] = None,
    task_manager: Optional[TaskManager] = None,
    take_book_stop_after_stage: TakeBookStage = TakeBookStage.ARM_STANDBY,
    grasp_step_approval: Optional[Callable[[str], bool]] = None,
    grasp_step_reporter: Optional[Callable[[str, str, Optional[str]], None]] = None,
) -> BookArmRuntime:
    """Build a self-contained runtime; it imports no GUI or web service.

    The returned runtime owns device startup and shutdown. It is deliberately
    not started here: enable physical hardware only after the caller completes
    its own operator safety checks.
    """

    manager = task_manager or TaskManager()
    plan: TakeBookMotionPlan
    if take_book_profile_path is None:
        plan = TakeBookMotionPlan()
    else:
        plan = load_take_book_motion_plan(Path(take_book_profile_path))
    arm = ArmService(arm_config or ArmDeviceConfig())
    camera = D435BookSpineLocator(
        replace(camera_config or D435BookSpineLocatorConfig(), calibration_path=Path(camera_calibration_path))
    )
    lift = AimLiftService(lift_config or LiftDeviceConfig())
    rod = RodMotorService(rod_config or RodDeviceConfig())
    reservation = RuntimeReservation()
    sdk = BookArmSDK(
        task_manager=manager,
        take_book=TakeBookUseCase(
            task_manager=manager,
            reservation=reservation,
            lift=lift,
            vision=camera,
            arm=arm,
            rod=rod,
            slots=(
                load_book_stand_slots(Path(book_stand_slots_path))
                if take_book_stop_after_stage in {TakeBookStage.BOOK_STAND_PLACE, TakeBookStage.ARM_STANDBY}
                else {}
            ),
            plan=plan,
            grasp_step_approval=grasp_step_approval,
            grasp_step_reporter=grasp_step_reporter,
        ),
        return_book=ReturnBookUseCase(
            task_manager=manager,
            reservation=reservation,
            lift=lift,
            vision=camera,
            arm=arm,
            rod=rod,
            pickup_slots={},
        ),
        return_book_to_end=ReturnBookToEndUseCase(manager),
    )
    services = {
        TakeBookStage.LIFT_MOVE: (lift,),
        TakeBookStage.VISION_TARGET: (lift, camera),
        TakeBookStage.ARM_DEBUG_POSE: (lift, camera, arm),
        TakeBookStage.BOOK_GRASP: (lift, camera, arm, rod),
        TakeBookStage.BOOK_STAND_PLACE: (lift, camera, arm, rod),
        TakeBookStage.ARM_STANDBY: (arm, camera, lift, rod),
    }
    try:
        active_services = services[take_book_stop_after_stage]
    except KeyError as error:
        raise ValueError("unsupported take-book stop stage: %s" % take_book_stop_after_stage) from error
    return BookArmRuntime(sdk, active_services)


def create_return_book_runtime(
    *,
    book_stand_pickup_slots_path: Path,
    camera_calibration_path: Path,
    return_book_profile_path: Optional[Path] = None,
    arm_config: Optional[ArmDeviceConfig] = None,
    camera_config: Optional[D435BookSpineLocatorConfig] = None,
    lift_config: Optional[LiftDeviceConfig] = None,
    rod_config: Optional[RodDeviceConfig] = None,
    task_manager: Optional[TaskManager] = None,
    stop_after_stage: ReturnBookStage = ReturnBookStage.SHELF_INSERT,
) -> BookArmRuntime:
    """Build a direct-device runtime for the seven-stage return-book workflow."""

    manager = task_manager or TaskManager()
    plan: ReturnBookMotionPlan
    if return_book_profile_path is None:
        plan = ReturnBookMotionPlan()
    else:
        plan = load_return_book_motion_plan(Path(return_book_profile_path))
    arm = ArmService(arm_config or ArmDeviceConfig())
    camera = D435BookSpineLocator(
        replace(
            camera_config or D435BookSpineLocatorConfig(),
            calibration_path=Path(camera_calibration_path),
        )
    )
    lift = AimLiftService(lift_config or LiftDeviceConfig())
    # The SDK facade still exposes take_book and its confirmed disable entry.
    # This rod service is not started by a return-book runtime.
    rod = RodMotorService(rod_config or RodDeviceConfig())
    reservation = RuntimeReservation()
    take = TakeBookUseCase(
        task_manager=manager,
        reservation=reservation,
        lift=lift,
        vision=camera,
        arm=arm,
        rod=rod,
        slots={},
    )
    sdk = BookArmSDK(
        task_manager=manager,
        take_book=take,
        return_book=ReturnBookUseCase(
            task_manager=manager,
            reservation=reservation,
            lift=lift,
            vision=camera,
            arm=arm,
            rod=rod,
            pickup_slots=(
                load_book_stand_pickup_slots(Path(book_stand_pickup_slots_path))
                if stop_after_stage
                in {
                    ReturnBookStage.BOOK_STAND_PICKUP,
                    ReturnBookStage.INSERT_TARGET,
                    ReturnBookStage.REACHABILITY_CHECK,
                    ReturnBookStage.SHELF_INSERT,
                }
                else {}
            ),
            plan=plan,
        ),
        return_book_to_end=ReturnBookToEndUseCase(manager),
    )
    services = {
        ReturnBookStage.LIFT_MOVE: (lift,),
        ReturnBookStage.ARM_DEBUG_POSE: (lift, arm, rod),
        ReturnBookStage.VISION_RETARGET: (lift, arm, rod, camera),
        ReturnBookStage.BOOK_STAND_PICKUP: (lift, arm, rod, camera),
        ReturnBookStage.INSERT_TARGET: (lift, arm, rod, camera),
        ReturnBookStage.REACHABILITY_CHECK: (lift, arm, rod, camera),
        ReturnBookStage.SHELF_INSERT: (lift, arm, rod, camera),
    }
    try:
        active_services = services[stop_after_stage]
    except KeyError as error:
        raise ValueError("unsupported return-book stop stage: %s" % stop_after_stage) from error
    return BookArmRuntime(sdk, active_services)
