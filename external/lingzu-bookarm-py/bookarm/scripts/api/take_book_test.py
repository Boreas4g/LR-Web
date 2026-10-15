#!/usr/bin/env python3
"""通过公开 BookArm SDK 执行一次完整的实机取书测试。

从 BookArm 仓库根目录运行：

    # 一阶段，升降台测试
    python3 scripts/api/take_book_test.py \
        --height-mm 800 --speed-level 2 --stop-after lift_move --confirm

    # 升降台+相机
    python3 scripts/api/take_book_test.py \
        --height-mm 800 \
        --speed-level 2 \
        --stop-after vision_target \
        --template assets/pic/book_spine/book1.jpeg \
        --camera-calibration config/calibration/camera_to_base.example.json \
        --confirm

    # 升降台+相机+机械臂调试位：机械臂会根据使能和当前位置决定是否运动
    python3 scripts/api/take_book_test.py \
        --height-mm 800 \
        --speed-level 2 \
        --template assets/pic/book_spine/book1.jpeg \
        --stop-after arm_debug_pose \
        --camera-calibration config/calibration/camera_to_base.example.json \
        --visualize \
        --confirm

    # 升降台+相机+调试位+目标点
    python3 scripts/api/take_book_test.py \
        --height-mm 800 \
        --speed-level 2 \
        --template assets/pic/book_spine/book1.jpeg \
        --stop-after book_grasp \
        --camera-calibration config/calibration/camera_to_base.example.json \
        --step-book-grasp \
        --grasp-point-from-top 1/3 \
        --confirm
    
    python3 scripts/api/take_book_test.py \
        --height-mm 800 \
        --speed-level 2 \
        --template assets/pic/book_spine/book1.jpeg \
        --stop-after book_grasp \
        --camera-calibration config/calibration/camera_to_base.example.json \
        --grasp-point-from-top 1/3 \
        --confirm

    # 升降台+相机+调试位+目标点+还书
    python3 scripts/api/take_book_test.py \
        --height-mm 750 \
        --speed-level 2 \
        --template assets/pic/book_spine/book1.jpeg \
        --camera-calibration config/calibration/camera_to_base.example.json \
        --slot 1 \
        --stop-after book_stand_place \
        --grasp-point-from-top 1/3 \
        --visualize\
        --confirm


    # 完整参数
    python3 scripts/api/take_book_test.py \
        --height-mm 800 \
        --speed-level 1 \
        --slot 1 \
        --book-stand-slots config/calibration/book_stand_slots.json \
        --camera-calibration config/calibration/camera_to_base.json \
        --arm-config config/devices/arm.json \
        --camera-config config/devices/camera.json \
        --lift-config config/devices/lift.json \
        --rod-config config/devices/rod.json \
        --confirm

``--stop-after lift_move`` 仅启动并控制升降台，不需要模板、相机外参或槽位
标定。执行更后续的阶段时，必须提供该阶段需要的已校准文件和参数。完整流程
会控制升降台、相机、机械臂和拨书杆；退出时运行时会停止已启动设备，机械臂会
仅关闭通信，保持当前使能状态。仅当用户在退出前明确确认时，才会发送机械臂失能
命令。它不会修改任何电机零位或标定文件。
"""

from __future__ import annotations

import argparse
from dataclasses import is_dataclass, replace
from enum import Enum
import json
import math
from pathlib import Path
import sys
from typing import Any, Optional, Sequence, TypeVar


BOOKARM_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = BOOKARM_ROOT / "src"
Aim_SDK_SOURCE_ROOT = BOOKARM_ROOT.parent.parent / "aim_sdk"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))
if str(Aim_SDK_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(Aim_SDK_SOURCE_ROOT))

from bookarm.api import TakeBookRequest, TakeBookStage
from bookarm.api.results import WorkflowResult
from bookarm.devices.arm import ArmDeviceConfig
from bookarm.devices.camera import D435BookSpineLocatorConfig
from bookarm.devices.camera.matcher import BookSpineMatcher
from bookarm.devices.lift import LiftDeviceConfig
from bookarm.devices.rod import RodDeviceConfig
from bookarm.domain.models.book import BookIdentity, SpineTemplate
from bookarm.domain.models.shelf import BookStandSlot
from bookarm.domain.models.task import TaskState
from bookarm.infrastructure import create_bookarm_runtime


DEFAULT_SLOTS = BOOKARM_ROOT / "config" / "calibration" / "book_stand_slots.json"
DEFAULT_CAMERA_CALIBRATION = BOOKARM_ROOT / "config" / "calibration" / "camera_to_base.json"
DEFAULT_SPINE_TEMPLATE = BOOKARM_ROOT / "assets" / "pic" / "book_spine" / "book1.jpeg"
ConfigT = TypeVar("ConfigT")


def grasp_point_from_top(value: str) -> float:
    """Parse supported book-spine positions measured from the top edge."""

    ratios = {
        "1/6": BookSpineMatcher.TOP_DOWN_ONE_SIXTH,
        "1/5": BookSpineMatcher.TOP_DOWN_ONE_FIFTH,
        "2/5": BookSpineMatcher.TOP_DOWN_TWO_FIFTHS,
        "1/4": BookSpineMatcher.TOP_DOWN_ONE_QUARTER,
        "1/3": BookSpineMatcher.TOP_DOWN_ONE_THIRD,
        "1/2": BookSpineMatcher.TOP_DOWN_ONE_HALF,
        "2/3": BookSpineMatcher.TOP_DOWN_TWO_THIRDS,
    }
    try:
        return ratios[value]
    except KeyError as error:
        raise argparse.ArgumentTypeError("must be one of: %s" % ", ".join(ratios)) from error


def finite_float(value: str) -> float:
    try:
        result = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(result):
        raise argparse.ArgumentTypeError("must be finite")
    return result


def positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if result <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def resolve_path(path: Path) -> Path:
    """Resolve all relative paths from the BookArm root, not the caller's CWD."""

    return path if path.is_absolute() else BOOKARM_ROOT / path


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="通过公开 BookArm SDK 执行一次完整的升降、定位、抓取和放书测试。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    request = parser.add_argument_group("取书请求")
    request.add_argument("--book-id", default="take-book-test", help="本次测试的书籍标识。")
    request.add_argument(
        "--template",
        type=Path,
        default=DEFAULT_SPINE_TEMPLATE,
        help="现场可见书脊的模板图片；默认使用 assets/pic/book_spine/book1.jpeg。",
    )
    request.add_argument("--height-mm", type=finite_float, required=True, help="AIM 标定目标高度，范围 10--670 mm。")
    request.add_argument("--speed-level", type=int, choices=(1, 2, 3), default=1, help="AIM 速度档位：1 慢、2 中、3 快。")
    request.add_argument("--slot", type=int, choices=(1, 2, 3, 4), help="目标书架槽位；放书阶段及之后必填。")
    request.add_argument(
        "--stop-after", choices=tuple(stage.value for stage in TakeBookStage),
        default=TakeBookStage.ARM_STANDBY.value,
        help="成功执行到指定工作流阶段后结束；默认执行完整流程。",
    )
    request.add_argument(
        "--step-book-grasp",
        action="store_true",
        help="前三阶段保持自动执行；进入 book_grasp 后，每个内部动作前都要求终端确认。",
    )

    calibration = parser.add_argument_group("标定")
    calibration.add_argument("--book-stand-slots", type=Path, default=DEFAULT_SLOTS, help="三个槽位的已校准 JSON。")
    calibration.add_argument("--camera-calibration", type=Path, default=DEFAULT_CAMERA_CALIBRATION, help="相机到基座的已校准外参 JSON。")
    calibration.add_argument("--take-book-profile", type=Path, help="可选的取书动作 profile JSON。")
    calibration.add_argument(
        "--grasp-point-from-top",
        type=grasp_point_from_top,
        choices=(
            BookSpineMatcher.TOP_DOWN_ONE_SIXTH,
            BookSpineMatcher.TOP_DOWN_ONE_FIFTH,
            BookSpineMatcher.TOP_DOWN_TWO_FIFTHS,
            BookSpineMatcher.TOP_DOWN_ONE_QUARTER,
            BookSpineMatcher.TOP_DOWN_ONE_THIRD,
            BookSpineMatcher.TOP_DOWN_ONE_HALF,
            BookSpineMatcher.TOP_DOWN_TWO_THIRDS,
        ),
        metavar="{1/6,1/5,2/5,1/4,1/3,1/2,2/3}",
        help="书脊抓取点距顶部的比例；未指定时沿用相机配置或默认 1/6。",
    )

    visualization = parser.add_argument_group("视觉显示")
    visualization.add_argument(
        "--visualize",
        action="store_true",
        help="自动显示本次定位实际使用的 D435 画面、书脊模板和匹配结果；不等待按键。",
    )
    visualization.add_argument(
        "--visualize-duration-ms",
        type=positive_int,
        default=2500,
        help="开启 --visualize 后，每个画面的自动展示时长，单位毫秒。",
    )

    devices = parser.add_argument_group("设备配置")
    devices.add_argument("--arm-config", type=Path, help="可选 ArmDeviceConfig JSON。")
    devices.add_argument("--camera-config", type=Path, help="可选 D435BookSpineLocatorConfig JSON。")
    devices.add_argument("--lift-config", type=Path, help="可选 LiftDeviceConfig JSON。")
    devices.add_argument("--rod-config", type=Path, help="可选 RodDeviceConfig JSON。")
    parser.add_argument("--confirm", action="store_true", help="确认现场安全，允许控制所有实机设备。")
    return parser.parse_args(argv)


def load_config(path: Optional[Path], config_type: type[ConfigT]) -> Optional[ConfigT]:
    """Load one optional device dataclass config and reject spelling mistakes."""

    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError("cannot read config file %s: %s" % (path, error)) from error
    except json.JSONDecodeError as error:
        raise ValueError("invalid JSON config %s: %s" % (path, error)) from error
    if not isinstance(payload, dict):
        raise ValueError("config %s must be a JSON object" % path)
    fields = getattr(config_type, "__dataclass_fields__", None)
    if fields is None or not is_dataclass(config_type):
        raise TypeError("config type must be a dataclass")
    unknown = sorted(set(payload) - set(fields))
    if unknown:
        raise ValueError("unsupported config fields in %s: %s" % (path, ", ".join(unknown)))
    values: dict[str, Any] = dict(payload)
    if config_type is ArmDeviceConfig:
        if "urdf_path" in values:
            values["urdf_path"] = Path(values["urdf_path"])
        if "tcp_offset" in values:
            values["tcp_offset"] = tuple(float(value) for value in values["tcp_offset"])
        if "joint_directions" in values:
            values["joint_directions"] = {int(key): float(value) for key, value in values["joint_directions"].items()}
        if "joint_offsets" in values:
            values["joint_offsets"] = {int(key): float(value) for key, value in values["joint_offsets"].items()}
        if "gravity_joint_scale" in values:
            values["gravity_joint_scale"] = {
                int(key): float(value) for key, value in values["gravity_joint_scale"].items()
            }
        if "joint_limits" in values:
            values["joint_limits"] = {
                int(key): tuple(float(limit) for limit in limits)
                for key, limits in values["joint_limits"].items()
            }
    elif config_type is D435BookSpineLocatorConfig and "calibration_path" in values:
        values["calibration_path"] = Path(values["calibration_path"])
    try:
        return config_type(**values)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid config %s: %s" % (path, error)) from error


def validate_args(args: argparse.Namespace) -> None:
    stop_after = TakeBookStage(args.stop_after)
    if args.visualize and stop_after is TakeBookStage.LIFT_MOVE:
        raise ValueError("--visualize requires --stop-after vision_target or a later stage")
    if args.step_book_grasp and stop_after not in {
        TakeBookStage.BOOK_GRASP,
        TakeBookStage.BOOK_STAND_PLACE,
        TakeBookStage.ARM_STANDBY,
    }:
        raise ValueError("--step-book-grasp requires --stop-after book_grasp or a later stage")
    required = []
    if stop_after is not TakeBookStage.LIFT_MOVE:
        if args.template is None:
            raise ValueError("--template is required after the lift_move stage")
        required.append("template")
        required.append("camera_calibration")
    if stop_after in {TakeBookStage.BOOK_STAND_PLACE, TakeBookStage.ARM_STANDBY}:
        if args.slot is None:
            raise ValueError("--slot is required for the book_stand_place stage")
        required.append("book_stand_slots")
    for name in required:
        path = getattr(args, name)
        if not path.is_file():
            raise FileNotFoundError("required file does not exist: %s" % path)
    for name in ("take_book_profile", "arm_config", "camera_config", "lift_config", "rod_config"):
        path = getattr(args, name)
        if path is not None and not path.is_file():
            raise FileNotFoundError("config file does not exist: %s" % path)
    if not 10.0 <= args.height_mm <= 670.0:
        raise ValueError("--height-mm must be within the AIM calibrated range 10..670 mm")


def to_json(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): to_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json(item) for item in value]
    return value


def print_result(result: WorkflowResult) -> None:
    print("取书任务：%s" % result.task_id.value, flush=True)
    print("最终状态：%s" % result.state.value, flush=True)
    for stage in result.stages:
        print("[%s] %s: %s" % (stage.status.value.upper(), stage.name, stage.message), flush=True)
        if stage.details:
            print(json.dumps(to_json(stage.details), ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    if result.error:
        print("错误：%s" % result.error, file=sys.stderr, flush=True)


def confirm_arm_disable(runtime) -> bool:
    """Disable the arm only after an interactive, explicit operator decision."""

    if not sys.stdin.isatty():
        print("非交互终端未确认失能；机械臂保持当前使能状态。", file=sys.stderr)
        return False
    try:
        reply = input("取书测试结束。是否移动到失能位、确认到位后再失能机械臂 1--7 轴？[y/N]: ")
    except (EOFError, KeyboardInterrupt):
        print("\n未收到失能确认；机械臂保持当前使能状态。", file=sys.stderr)
        return False
    if reply.strip().lower() not in {"y", "yes", "是"}:
        print("未发送机械臂失能命令；机械臂保持当前使能状态。")
        return False
    result = runtime.sdk.disable_arm(confirmed=True)
    print("[OK] 机械臂已到达失能位并发送失能命令：%s" % json.dumps(result, ensure_ascii=False))
    return True


_BOOK_GRASP_STEP_LABELS = {
    "open_gripper": "打开夹爪",
    "rod_home": "拨书杆回零位",
    "arm_pregrasp": "机械臂 MoveJ 逆解到预抓取位",
    "arm_approach": "机械臂直线靠近书脊",
    "rod_grasp": "拨书杆转到抓书位",
    "arm_fine_grasp": "机械臂 MoveJ 逆解到最终抓取位",
    "close_gripper": "闭合夹爪并监测夹持",
    "arm_post_grasp": "机械臂 MoveJ 逆解撤离",
    "arm_return_debug": "机械臂回取书调试位",
}


def confirm_book_grasp_step(step_name: str) -> bool:
    """Obtain explicit supervision before one potentially moving grasp action."""

    label = _BOOK_GRASP_STEP_LABELS.get(step_name, step_name)
    if not sys.stdin.isatty():
        print(
            "非交互终端无法确认 book_grasp 子阶段 %s；未发送该动作。" % step_name,
            file=sys.stderr,
        )
        return False
    try:
        reply = input("book_grasp 下一步：%s。是否执行？[y/N]: " % label)
    except (EOFError, KeyboardInterrupt):
        print("\n未收到 book_grasp 子阶段确认；未发送该动作。", file=sys.stderr)
        return False
    return reply.strip().lower() in {"y", "yes", "是"}


def report_book_grasp_step(step_name: str, event: str, error: Optional[str] = None) -> None:
    """Print immediate progress and failure details for supervised book grasping."""

    label = _BOOK_GRASP_STEP_LABELS.get(step_name, step_name)
    if event == "started":
        print("[INFO] book_grasp 正在执行：%s (%s)" % (label, step_name), flush=True)
    elif event == "completed":
        print("[OK] book_grasp 已完成：%s (%s)" % (label, step_name), flush=True)
    elif event == "failed":
        print(
            "[FAIL] book_grasp 失败：%s (%s): %s" % (label, step_name, error or "unknown error"),
            file=sys.stderr,
            flush=True,
        )


def stop_stage_uses_arm(stage: TakeBookStage) -> bool:
    return stage in {
        TakeBookStage.ARM_DEBUG_POSE,
        TakeBookStage.BOOK_GRASP,
        TakeBookStage.BOOK_STAND_PLACE,
        TakeBookStage.ARM_STANDBY,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if not args.confirm:
        print("执行失败：此脚本会移动升降台和机械臂；确认现场安全后请加 --confirm。", file=sys.stderr)
        return 2
    for name in (
        "template", "book_stand_slots", "camera_calibration", "take_book_profile",
        "arm_config", "camera_config", "lift_config", "rod_config",
    ):
        path = getattr(args, name)
        if path is not None:
            setattr(args, name, resolve_path(path))
    try:
        validate_args(args)
        camera_config = load_config(args.camera_config, D435BookSpineLocatorConfig)
        if args.grasp_point_from_top is not None:
            camera_config = replace(
                camera_config or D435BookSpineLocatorConfig(),
                grasp_point_from_top_ratio=args.grasp_point_from_top,
            )
        if args.visualize:
            camera_config = replace(
                camera_config or D435BookSpineLocatorConfig(),
                visualize=True,
                visualize_duration_ms=args.visualize_duration_ms,
            )
        runtime = create_bookarm_runtime(
            book_stand_slots_path=args.book_stand_slots,
            camera_calibration_path=args.camera_calibration,
            take_book_profile_path=args.take_book_profile,
            arm_config=load_config(args.arm_config, ArmDeviceConfig),
            camera_config=camera_config,
            lift_config=load_config(args.lift_config, LiftDeviceConfig),
            rod_config=load_config(args.rod_config, RodDeviceConfig),
            take_book_stop_after_stage=TakeBookStage(args.stop_after),
            grasp_step_approval=confirm_book_grasp_step if args.step_book_grasp else None,
            grasp_step_reporter=report_book_grasp_step if args.step_book_grasp else None,
        )
        request = TakeBookRequest(
            book=BookIdentity(args.book_id),
            target_height_mm=args.height_mm,
            spine_template=SpineTemplate(args.template or Path("unused-template.png")),
            destination_slot=BookStandSlot(args.slot or BookStandSlot.SLOT_1.value),
            lift_speed_level=args.speed_level,
            stop_after_stage=TakeBookStage(args.stop_after),
        )
    except (FileNotFoundError, ValueError, TypeError) as error:
        print("配置错误：%s" % error, file=sys.stderr)
        return 2

    print("开始取书测试，执行至 %s 阶段后结束。请保持外部急停可用，并在运动期间监控设备。" % args.stop_after)
    runtime_started = False
    result: Optional[WorkflowResult] = None
    exit_code: Optional[int] = None
    try:
        runtime.start()
        runtime_started = True
        task = runtime.sdk.take_book(request)
        result = runtime.sdk.wait(task.id)
        # Print the terminal result before any optional arm-disable interaction.
        print_result(result)
        exit_code = 0 if result.state is TaskState.SUCCEEDED else 1
        if result.state is TaskState.RECOVERY_REQUIRED:
            print("任务需要人工恢复；请检查机械臂、夹爪和书籍状态。", file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        print("\n测试已由用户中断；运行时将仅关闭设备通信。", file=sys.stderr)
        exit_code = 130
    except Exception as error:
        print("取书测试异常：%s" % error, file=sys.stderr, flush=True)
        exit_code = 1
    finally:
        if runtime_started:
            if stop_stage_uses_arm(TakeBookStage(args.stop_after)):
                try:
                    confirm_arm_disable(runtime)
                except Exception as error:
                    print("机械臂失能请求失败：%s" % error, file=sys.stderr)
            runtime.stop()

    return 1 if exit_code is None else exit_code


if __name__ == "__main__":
    raise SystemExit(main())
