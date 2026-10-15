#!/usr/bin/env python3
"""通过公开 BookArm SDK 执行七阶段还书流程（识别目标书 → 书立取书 → 插入书架）。

示例：
    # 升降台测试
    python3 scripts/api/return_book_test.py \
      --height-mm 1160 \
      --speed-level 2 \
      --slot 1 \
      --stop-after lift_move \
      --left-neighbor-template assets/pic/book_spine/book1.jpeg \
      --camera-calibration config/calibration/camera_to_base.example.json \
      --confirm

    # 升降台+调试位
    python3 scripts/api/return_book_test.py \
        --height-mm 1160 \
        --speed-level 2 \
        --slot 1 \
        --stop-after arm_debug_pose \
        --left-neighbor-template assets/pic/book_return/image.png \
        --camera-calibration config/calibration/camera_to_base.example.json \
        --confirm

    # 升降台+调试位+识别目标书
        python3 scripts/api/return_book_test.py \
            --height-mm 1160 \
            --speed-level 2 \
            --slot 1 \
            --stop-after vision_retarget \
            --left-neighbor-template assets/pic/book_return/image.png \
            --visualize\
            --camera-calibration config/calibration/camera_to_base.example.json \
            --confirm

    # 升降台+调试位+识别目标书+书立取书
        python3 scripts/api/return_book_test.py \
            --height-mm 1160 \
            --speed-level 2 \
            --slot 1 \
            --stop-after book_stand_pickup \
            --left-neighbor-template assets/pic/book_return/image.png \
            --camera-calibration config/calibration/camera_to_base.example.json \
            --confirm

    # 升降台+调试位+识别目标书+书立取书+插入书架
        python3 scripts/api/return_book_test.py \
            --height-mm 1100 \
            --speed-level 2 \
            --slot 1 \
            --stop-after shelf_insert \
            --left-neighbor-template assets/pic/book_return/image.png \
            --camera-calibration config/calibration/camera_to_base.example.json \
            --confirm

        python3 scripts/api/return_book_test.py \
            --height-mm 1100 \
            --speed-level 2 \
            --slot 1 \
            --stop-after shelf_insert \
            --left-neighbor-template assets/pic/book_return/image.png \
            --camera-calibration config/calibration/camera_to_base.example.json \
            --visualize\
            --confirm

可以使用 ``--stop-after`` 在指定阶段结束。运行时退出时只关闭设备通信，
不会自动失能机械臂。
"""

from __future__ import annotations

import argparse
from dataclasses import fields
from enum import Enum
import json
import math
from pathlib import Path
import sys
from typing import Any, Optional, Sequence, TypeVar


BOOKARM_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = BOOKARM_ROOT / "src"
AIM_SDK_SOURCE_ROOT = BOOKARM_ROOT.parent.parent / "aim_sdk"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))
if str(AIM_SDK_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(AIM_SDK_SOURCE_ROOT))

from bookarm.api import ReturnBookRequest, ReturnBookStage
from bookarm.api.results import WorkflowResult
from bookarm.devices.arm import ArmDeviceConfig
from bookarm.devices.camera import D435BookSpineLocatorConfig
from bookarm.devices.lift import LiftDeviceConfig
from bookarm.devices.rod import RodDeviceConfig
from bookarm.domain.models.book import BookIdentity, SpineTemplate
from bookarm.domain.models.shelf import BookStandSlot
from bookarm.domain.models.task import TaskState
from bookarm.infrastructure import create_return_book_runtime


DEFAULT_PICKUP_SLOTS = BOOKARM_ROOT / "config" / "calibration" / "book_stand_pickup_slots.json"
DEFAULT_CAMERA_CALIBRATION = (
    BOOKARM_ROOT / "config" / "calibration" / "camera_to_base.example.json"
)
DEFAULT_LEFT_NEIGHBOR_TEMPLATE = BOOKARM_ROOT / "assets" / "pic" / "book_return" / "image.png"
ConfigT = TypeVar("ConfigT")


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
    return path if path.is_absolute() else BOOKARM_ROOT / path


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="通过公开 BookArm SDK 执行升降、车载书架取书、视觉定位和外置书架还书测试。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    request = parser.add_argument_group("还书请求")
    request.add_argument("--book-id", default="return-book-test", help="本次测试的书籍标识。")
    request.add_argument(
        "--height-mm",
        type=finite_float,
        required=True,
        help="AIM 标定目标书架高度，范围 10--670 mm。",
    )
    request.add_argument(
        "--speed-level",
        type=int,
        choices=(1, 2, 3),
        default=1,
        help="AIM 速度档位：1 慢、2 中、3 快。",
    )
    request.add_argument(
        "--slot",
        type=int,
        choices=(1, 2, 3),
        help="车载 Book Stand 来源槽位；book_stand_pickup 阶段及之后必填。",
    )
    request.add_argument(
        "--left-neighbor-template",
        "--template",
        dest="left_neighbor_template",
        type=Path,
        default=DEFAULT_LEFT_NEIGHBOR_TEMPLATE,
        help="目标参考书的书脊照片；还回的书插入到它旁边。",
    )
    request.add_argument(
        "--stop-after",
        choices=tuple(stage.value for stage in ReturnBookStage),
        default=ReturnBookStage.SHELF_INSERT.value,
        help="成功执行到指定阶段后结束。",
    )

    calibration = parser.add_argument_group("标定与动作参数")
    calibration.add_argument(
        "--book-stand-pickup-slots",
        type=Path,
        default=DEFAULT_PICKUP_SLOTS,
        help="三个车载书架槽位的预抓取位和抓取位 JSON。",
    )
    calibration.add_argument(
        "--camera-calibration",
        type=Path,
        default=DEFAULT_CAMERA_CALIBRATION,
        help="D435 到机械臂基座的 4x4 外参 JSON。",
    )
    calibration.add_argument(
        "--return-book-profile",
        type=Path,
        help="可选还书动作 profile；包含基座坐标偏移、目标姿态和运动时长。",
    )

    visualization = parser.add_argument_group("视觉显示")
    visualization.add_argument(
        "--visualize",
        action="store_true",
        help="显示目标书脊模板的识别框；不等待用户按键。",
    )
    visualization.add_argument(
        "--visualize-duration-ms",
        type=positive_int,
        default=2500,
        help="每个视觉画面的自动展示时长。",
    )

    devices = parser.add_argument_group("设备配置")
    devices.add_argument("--arm-config", type=Path, help="可选 ArmDeviceConfig JSON。")
    devices.add_argument("--camera-config", type=Path, help="可选 D435 配置 JSON。")
    devices.add_argument("--lift-config", type=Path, help="可选 LiftDeviceConfig JSON。")
    devices.add_argument("--rod-config", type=Path, help="可选 RodDeviceConfig JSON。")
    parser.add_argument("--confirm", action="store_true", help="确认现场安全，允许控制实机设备。")
    return parser.parse_args(argv)


def load_config(path: Optional[Path], config_type: type[ConfigT]) -> Optional[ConfigT]:
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
    allowed = {field.name for field in fields(config_type)}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError("unsupported config fields in %s: %s" % (path, ", ".join(unknown)))
    values: dict[str, Any] = dict(payload)
    if config_type is ArmDeviceConfig:
        if "urdf_path" in values:
            values["urdf_path"] = Path(values["urdf_path"])
        if "tcp_offset" in values:
            values["tcp_offset"] = tuple(float(value) for value in values["tcp_offset"])
        for name in ("joint_directions", "joint_offsets", "gravity_joint_scale"):
            if name in values:
                values[name] = {int(key): float(value) for key, value in values[name].items()}
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
    stage = ReturnBookStage(args.stop_after)
    if not 10.0 <= args.height_mm <= 670.0:
        raise ValueError("--height-mm must be within the AIM calibrated range 10..670 mm")
    if stage in {
        ReturnBookStage.BOOK_STAND_PICKUP,
        ReturnBookStage.INSERT_TARGET,
        ReturnBookStage.REACHABILITY_CHECK,
        ReturnBookStage.SHELF_INSERT,
    }:
        if args.slot is None:
            raise ValueError("--slot is required for book_stand_pickup or a later stage")
        if not args.book_stand_pickup_slots.is_file():
            raise FileNotFoundError(
                "required file does not exist: %s" % args.book_stand_pickup_slots
            )
    if stage not in {ReturnBookStage.LIFT_MOVE, ReturnBookStage.ARM_DEBUG_POSE}:
        # vision_retarget (and every later stage) needs the template and the
        # camera extrinsics.
        for path in (args.left_neighbor_template, args.camera_calibration):
            if not path.is_file():
                raise FileNotFoundError("required file does not exist: %s" % path)
    if args.visualize and stage in {ReturnBookStage.LIFT_MOVE, ReturnBookStage.ARM_DEBUG_POSE}:
        raise ValueError("--visualize requires --stop-after vision_retarget or a later stage")
    for path in (
        args.return_book_profile,
        args.arm_config,
        args.camera_config,
        args.lift_config,
        args.rod_config,
    ):
        if path is not None and not path.is_file():
            raise FileNotFoundError("config file does not exist: %s" % path)


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
    print("还书任务：%s" % result.task_id.value, flush=True)
    print("最终状态：%s" % result.state.value, flush=True)
    for stage in result.stages:
        print("[%s] %s: %s" % (stage.status.value.upper(), stage.name, stage.message), flush=True)
        if stage.details:
            print(json.dumps(to_json(stage.details), ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    if result.error:
        print("错误：%s" % result.error, file=sys.stderr, flush=True)


def stop_stage_uses_arm(stage: ReturnBookStage) -> bool:
    return stage is not ReturnBookStage.LIFT_MOVE


def confirm_arm_disable(runtime) -> None:
    if not sys.stdin.isatty():
        print("非交互终端未确认失能；机械臂保持当前使能状态。", file=sys.stderr)
        return
    try:
        reply = input("还书测试结束。是否移动到失能位、确认到位后再失能机械臂 1--7 轴？[y/N]: ")
    except (EOFError, KeyboardInterrupt):
        print("\n未收到失能确认；机械臂保持当前使能状态。", file=sys.stderr)
        return
    if reply.strip().lower() not in {"y", "yes", "是"}:
        print("未发送机械臂失能命令；机械臂保持当前使能状态。")
        return
    result = runtime.sdk.disable_arm(confirmed=True)
    print("[OK] 机械臂已到达失能位并发送失能命令：%s" % json.dumps(result, ensure_ascii=False))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if not args.confirm:
        print("执行失败：此脚本会移动升降台和机械臂；确认现场安全后请加 --confirm。", file=sys.stderr)
        return 2
    for name in (
        "left_neighbor_template",
        "book_stand_pickup_slots",
        "camera_calibration",
        "return_book_profile",
        "arm_config",
        "camera_config",
        "lift_config",
        "rod_config",
    ):
        path = getattr(args, name)
        if path is not None:
            setattr(args, name, resolve_path(path))
    try:
        validate_args(args)
        camera_config = load_config(args.camera_config, D435BookSpineLocatorConfig)
        if args.visualize:
            values = dict(vars(camera_config)) if camera_config is not None else {}
            values.update(
                visualize=True,
                visualize_duration_ms=args.visualize_duration_ms,
            )
            camera_config = D435BookSpineLocatorConfig(**values)
        stop_after = ReturnBookStage(args.stop_after)
        runtime = create_return_book_runtime(
            book_stand_pickup_slots_path=args.book_stand_pickup_slots,
            camera_calibration_path=args.camera_calibration,
            return_book_profile_path=args.return_book_profile,
            arm_config=load_config(args.arm_config, ArmDeviceConfig),
            camera_config=camera_config,
            lift_config=load_config(args.lift_config, LiftDeviceConfig),
            rod_config=load_config(args.rod_config, RodDeviceConfig),
            stop_after_stage=stop_after,
        )
        request = ReturnBookRequest(
            book=BookIdentity(args.book_id),
            target_height_mm=args.height_mm,
            left_neighbor_spine_template=SpineTemplate(
                args.left_neighbor_template or Path("unused-template.jpeg")
            ),
            source_slot=BookStandSlot(args.slot or BookStandSlot.SLOT_1.value),
            lift_speed_level=args.speed_level,
            stop_after_stage=stop_after,
        )
    except (FileNotFoundError, ValueError, TypeError) as error:
        print("配置错误：%s" % error, file=sys.stderr)
        return 2

    print(
        "开始还书测试，执行至 %s 阶段后结束。请保持外部急停可用，并在运动期间监控设备。"
        % args.stop_after
    )
    runtime_started = False
    exit_code = 1
    try:
        runtime.start()
        runtime_started = True
        task = runtime.sdk.return_book(request)
        result = runtime.sdk.wait(task.id)
        print_result(result)
        exit_code = 0 if result.state is TaskState.SUCCEEDED else 1
        if result.state is TaskState.RECOVERY_REQUIRED:
            print("任务需要人工恢复；请检查机械臂、夹爪和书籍状态。", file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        print("\n测试已由用户中断；运行时将仅关闭设备通信。", file=sys.stderr)
        exit_code = 130
    except Exception as error:
        print("还书测试异常：%s" % error, file=sys.stderr, flush=True)
        exit_code = 1
    finally:
        if runtime_started:
            if stop_stage_uses_arm(ReturnBookStage(args.stop_after)):
                try:
                    confirm_arm_disable(runtime)
                except Exception as error:
                    print("机械臂失能请求失败：%s" % error, file=sys.stderr)
            runtime.stop()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
