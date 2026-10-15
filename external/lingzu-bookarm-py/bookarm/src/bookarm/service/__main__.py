"""Command-line entry point for the bookarm take-out/return HTTP service.

From the BookArm repository root:

    # Hardware-free simulation
    python3 -m bookarm.service --fake --port 8090

    # Real hardware (CAN, rod, lift and RealSense must be reachable)
    python3 -m bookarm.service \
        --slots-path config/calibration/book_stand_slots.json \
        --pickup-slots-path config/calibration/book_stand_pickup_slots.json \
        --camera-calibration config/calibration/camera_to_base.example.json \
        --token secret
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import sys
from typing import Any, Callable, Optional, Sequence


BOOKARM_ROOT = Path(os.environ.get("LR_BOOKARM_ROOT") or Path(__file__).resolve().parents[3])
INFERENCE_ROOT = Path(
    os.environ.get("LR_BOOKARM_INFERENCE_ROOT")
    or Path(__file__).resolve().parents[4] / "book_spine_inference_light"
)
DEFAULT_UPLOAD_DIR = BOOKARM_ROOT / "recordings" / "takeout_templates"
# The spine studio holds production library data, so it lives outside the
# code repository: a redeploy or a wiped checkout must never drop templates.
DEFAULT_STUDIO_DIR = Path("/var/lib/lr-bookarm/spine_studio")
DEFAULT_TUNING_DIR = BOOKARM_ROOT / "recordings" / "tuning"
DEFAULT_RUNTIME_DIR = BOOKARM_ROOT / "recordings" / "runtime"
DEFAULT_TASK_STORE = DEFAULT_RUNTIME_DIR / "bookarm-tasks.json"
DEFAULT_HARDWARE_LOCK = DEFAULT_RUNTIME_DIR / "bookarm-hardware.lock"
DEFAULT_SLOTS = BOOKARM_ROOT / "config" / "calibration" / "book_stand_slots.json"
DEFAULT_PICKUP_SLOTS = BOOKARM_ROOT / "config" / "calibration" / "book_stand_pickup_slots.json"
DEFAULT_CAMERA_CALIBRATION = BOOKARM_ROOT / "config" / "calibration" / "camera_to_base.example.json"
# User-refined extrinsic written by the studio camera-transform API; when it
# exists it wins over the packaged example at startup.
DEFAULT_CAMERA_CALIBRATION_OVERRIDE = BOOKARM_ROOT / "config" / "calibration" / "camera_to_base.json"
DEFAULT_YOLO_WEIGHTS = INFERENCE_ROOT / "weights" / "best.pt"
_GUI_INERTIA = Path(__file__).resolve().parents[4] / "resources" / "config" / "inertia_params.yaml"
DEFAULT_ARM_INERTIA = (
    _GUI_INERTIA
    if _GUI_INERTIA.is_file()
    else BOOKARM_ROOT / "config" / "calibration" / "arm_inertia.example.yaml"
)

HARDWARE_CHECKLIST = """hardware startup failed; please check:
  - CAN interface can0 is up and the arm is reachable
  - /dev/rodmotor (book-pushing rod serial port) exists and is accessible
  - /dev/lift_port (lift serial port) exists and is accessible
  - the Intel RealSense D435 camera is connected
"""


def resolve_path(path: Path) -> Path:
    """Resolve all relative paths from the BookArm root, not the caller's CWD."""

    return path if path.is_absolute() else BOOKARM_ROOT / path


def take_book_plan_getter(sdk: Any) -> Callable[[], Any]:
    """Studio 倾斜/居中阈值的 plan 读取入口：取书用例对象挂在 sdk._take_book
    （sdk.take_book 是同名的公开工作流方法，没有 plan 属性，勿直接取）。
    调参热更新替换用例的 plan 属性后，下一次读取即生效。"""

    return lambda: sdk._take_book.plan


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="通过 HTTP 提供取书/还书服务；硬件独占，任务串行执行。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP 监听地址。")
    parser.add_argument("--port", type=int, default=8090, help="HTTP 监听端口。")
    parser.add_argument(
        "--unix-socket",
        type=Path,
        help="改用 Unix socket 监听（生产 Docker 反代使用）；不能与 TCP 端口同时监听。",
    )
    parser.add_argument("--fake", action="store_true", help="不接触硬件，模拟取书/还书流程。")
    parser.add_argument("--token", help="可选的 Bearer 访问令牌；设置后所有接口强制鉴权。")
    parser.add_argument(
        "--task-store-path",
        type=Path,
        default=None,
        help="任务持久化 JSON；重启时未完成任务会标记为需人工恢复。",
    )
    parser.add_argument(
        "--hardware-lock-path",
        type=Path,
        default=DEFAULT_HARDWARE_LOCK,
        help="真实硬件进程互斥锁；--fake 模式不获取。",
    )
    parser.add_argument(
        "--upload-dir",
        type=Path,
        default=DEFAULT_UPLOAD_DIR,
        help="上传书脊模板图片的保存目录。",
    )
    parser.add_argument(
        "--studio-dir",
        type=Path,
        default=DEFAULT_STUDIO_DIR,
        help="书脊分割工作台的拍照/分割/书脊库存储目录。",
    )
    parser.add_argument(
        "--yolo-weights",
        type=Path,
        default=DEFAULT_YOLO_WEIGHTS,
        help="书脊分割 YOLO 权重 best.pt；缺失时分割接口返回 503。",
    )
    parser.add_argument(
        "--yolo-infer",
        type=Path,
        help="书脊分割 infer.py 路径；默认取权重同级目录下的 infer.py。",
    )
    parser.add_argument(
        "--yolo-threads",
        type=int,
        default=2,
        choices=[1, 2, 3, 4],
        help="YOLO 分割时 torch 使用的 CPU 线程数，避免占满全部核心。",
    )
    parser.add_argument(
        "--yolo-idle-unload-s",
        type=float,
        default=300.0,
        help="YOLO 模型闲置多少秒后从内存卸载（0 = 永不卸载）。",
    )
    parser.add_argument("--slots-path", type=Path, default=DEFAULT_SLOTS, help="书立槽位标定 JSON（取书）。")
    parser.add_argument(
        "--pickup-slots-path",
        type=Path,
        default=DEFAULT_PICKUP_SLOTS,
        help="书立取书槽位标定 JSON（还书）。",
    )
    parser.add_argument(
        "--camera-calibration",
        type=Path,
        default=None,
        help=(
            "相机到基座的外参标定 JSON；缺省时优先使用调参接口写入的 "
            "config/calibration/camera_to_base.json，不存在则回退 camera_to_base.example.json。"
        ),
    )
    parser.add_argument(
        "--take-profile",
        type=Path,
        help="可选的取书动作 profile JSON；缺省时优先使用调参接口持久化的 recordings/tuning/take_profile.json。",
    )
    parser.add_argument(
        "--return-profile",
        type=Path,
        help="可选的还书动作 profile JSON；缺省时优先使用调参接口持久化的 recordings/tuning/return_profile.json。",
    )
    parser.add_argument(
        "--midreturn-profile",
        type=Path,
        help="可选的中位还书动作 profile JSON；缺省时优先使用调参接口持久化的 recordings/tuning/midreturn_profile.json。",
    )
    parser.add_argument(
        "--arm-feedback-max-age-s",
        type=float,
        default=0.6,
        help="机械臂反馈保鲜上限（秒）；小主机调度抖动较大，默认放宽到 0.6，仍远快于人工反应。",
    )
    parser.add_argument(
        "--arm-inertia-config",
        type=Path,
        default=DEFAULT_ARM_INERTIA,
        help="标定惯量参数 YAML（重力前馈用）；传 NONE 可禁用。",
    )
    parser.add_argument(
        "--arm-thermal-limit-c",
        type=float,
        default=90.0,
        help="关节温度保护阈值（摄氏度），超过即告警并失能锁定。",
    )
    parser.add_argument(
        "--arm-thermal-check-interval-s",
        type=float,
        default=60.0,
        help="温度巡检间隔（秒），0 关闭温度保护。",
    )
    parser.add_argument(
        "--arm-telemetry-dir",
        type=Path,
        default=BOOKARM_ROOT / "recordings" / "arm_telemetry",
        help="关节遥测 CSV 保存目录；传 NONE 关闭记录。",
    )
    parser.add_argument(
        "--arm-telemetry-interval-s",
        type=float,
        default=1.0,
        help="遥测采样间隔（秒）。",
    )
    parser.add_argument(
        "--telemetry-retention-days",
        type=float,
        default=7.0,
        help="telemetry_*.csv 保留天数，启动新文件时清理超期文件（0 = 不清理）。",
    )
    parser.add_argument(
        "--shutdown-timeout-s",
        type=float,
        default=10.0,
        help="停止服务时等待运行中任务线程的上限（秒）；超时后放弃卡死线程并退出进程。",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    upload_dir = resolve_path(args.upload_dir)
    yolo_weights = resolve_path(args.yolo_weights)
    yolo_infer = (
        resolve_path(args.yolo_infer)
        if args.yolo_infer is not None
        else yolo_weights.parent.parent / "infer.py"
    )

    from bookarm.application.task_manager import JsonTaskStore, TaskManager

    # A fake run must never open the real daemon's journal and mistakenly mark
    # its live task as interrupted.  Tests can still request persistence by
    # passing --task-store-path explicitly.
    if args.fake and args.task_store_path is None:
        task_manager = TaskManager()
    else:
        try:
            task_store_path = args.task_store_path or DEFAULT_TASK_STORE
            task_manager = TaskManager(JsonTaskStore(resolve_path(task_store_path)))
        except Exception as error:
            print("cannot initialize bookarm task journal: %s" % error, file=sys.stderr)
            return 2

    from bookarm.service.studio import StudioStore

    studio_store = StudioStore(resolve_path(args.studio_dir))

    # Profiles persisted by the tuning API become the startup defaults; an
    # explicit --take-profile/--return-profile/--midreturn-profile always wins.
    take_profile = args.take_profile
    if take_profile is None:
        persisted_take = DEFAULT_TUNING_DIR / "take_profile.json"
        if persisted_take.is_file():
            take_profile = persisted_take
    return_profile = args.return_profile
    if return_profile is None:
        persisted_return = DEFAULT_TUNING_DIR / "return_profile.json"
        if persisted_return.is_file():
            return_profile = persisted_return
    mid_return_profile = args.midreturn_profile
    if mid_return_profile is None:
        persisted_mid_return = DEFAULT_TUNING_DIR / "midreturn_profile.json"
        if persisted_mid_return.is_file():
            mid_return_profile = persisted_mid_return
    camera_calibration = args.camera_calibration
    if camera_calibration is None:
        camera_calibration = (
            DEFAULT_CAMERA_CALIBRATION_OVERRIDE
            if DEFAULT_CAMERA_CALIBRATION_OVERRIDE.is_file()
            else DEFAULT_CAMERA_CALIBRATION
        )

    hardware_lock = None
    if args.fake:
        from bookarm.service.runner import FakeWorkflowRunner
        from bookarm.service.safety import SafetyController
        from bookarm.service.studio import FakeStudioBackend
        from bookarm.service.tuning import create_fake_tuning

        runner = FakeWorkflowRunner(task_manager=task_manager)
        studio = FakeStudioBackend(studio_store, busy_lock=runner.busy_lock)
        runtime = None
        tuning = create_fake_tuning(runner, studio, store_dir=DEFAULT_TUNING_DIR)
        safety = SafetyController(runner)
    else:
        # Import hardware-backed modules lazily so --fake and --help never
        # require the device dependencies.
        from bookarm.service.composition import create_combined_runtime
        from bookarm.service.runner import SdkWorkflowRunner
        from bookarm.service.safety import SafetyController
        from bookarm.service.studio import RealStudioBackend
        from bookarm.service.tuning import TuningDevices, create_sdk_tuning
        from bookarm.runtime.reservation import HardwareProcessLock

        try:
            hardware_lock = HardwareProcessLock(resolve_path(args.hardware_lock_path))
            hardware_lock.acquire()
            combined = create_combined_runtime(
                book_stand_slots_path=resolve_path(args.slots_path),
                book_stand_pickup_slots_path=resolve_path(args.pickup_slots_path),
                camera_calibration_path=resolve_path(camera_calibration),
                take_book_profile_path=(
                    resolve_path(take_profile) if take_profile is not None else None
                ),
                return_book_profile_path=(
                    resolve_path(return_profile) if return_profile is not None else None
                ),
                mid_return_profile_path=(
                    resolve_path(mid_return_profile) if mid_return_profile is not None else None
                ),
                task_manager=task_manager,
                arm_feedback_max_age_s=args.arm_feedback_max_age_s,
                camera_debug_capture_dir=resolve_path(args.studio_dir) / "vision_debug",
                arm_inertia_config_path=(
                    None
                    if str(args.arm_inertia_config).upper() == "NONE"
                    else resolve_path(args.arm_inertia_config)
                ),
                arm_thermal_limit_c=args.arm_thermal_limit_c,
                arm_thermal_check_interval_s=args.arm_thermal_check_interval_s,
                arm_telemetry_dir=(
                    None
                    if str(args.arm_telemetry_dir).upper() == "NONE"
                    else resolve_path(args.arm_telemetry_dir)
                ),
                arm_telemetry_interval_s=args.arm_telemetry_interval_s,
                arm_telemetry_retention_days=args.telemetry_retention_days,
            )
            combined.runtime.start()
            degraded = combined.runtime.start_errors
            if degraded:
                print(
                    "hardware degraded startup; unavailable devices: %s"
                    % "; ".join("%s (%s)" % (name, error) for name, error in degraded.items()),
                    file=sys.stderr,
                )
        except Exception as error:
            if hardware_lock is not None:
                hardware_lock.release()
            print("%s: %s" % (HARDWARE_CHECKLIST, error), file=sys.stderr)
            return 2
        runtime = combined.runtime
        try:
            # Leave CPU headroom for the 200 Hz arm control thread while the
            # camera worker runs OpenCV template matching.
            import cv2

            cv2.setNumThreads(2)
        except Exception:
            pass
        runner = SdkWorkflowRunner(runtime, task_manager, shutdown_timeout_s=args.shutdown_timeout_s)
        studio = RealStudioBackend(
            camera=combined.camera,
            lift=combined.lift,
            store=studio_store,
            busy_lock=runner.busy_lock,
            vehicle=combined.vehicle,
            plan_getter=take_book_plan_getter(combined.runtime.sdk),
            weights_path=yolo_weights,
            infer_path=yolo_infer,
            torch_threads=args.yolo_threads,
            model_idle_unload_s=args.yolo_idle_unload_s,
            arm=combined.arm,
            rod=combined.rod,
        )
        tuning = create_sdk_tuning(
            combined.runtime.sdk,
            TuningDevices(arm=combined.arm, rod=combined.rod, vision=combined.camera),
            studio,
            store_dir=DEFAULT_TUNING_DIR,
            busy_lock=runner.busy_lock,
            book_stand_slots_path=resolve_path(args.slots_path),
            book_stand_pickup_slots_path=resolve_path(args.pickup_slots_path),
            preempt=runner.cancel_active,
        )
        safety = SafetyController(runner, arm=combined.arm, lift=combined.lift)

    import uvicorn

    from bookarm.service.app import create_app

    app = create_app(
        runner,
        upload_dir=upload_dir,
        token=args.token,
        studio=studio,
        tuning=tuning,
        safety=safety,
    )
    try:
        if args.unix_socket is None:
            uvicorn.run(app, host=args.host, port=args.port)
        else:
            socket_path = resolve_path(args.unix_socket)
            _prepare_unix_socket(socket_path)
            uvicorn.run(app, uds=str(socket_path))
    finally:
        studio.shutdown()
        runner.shutdown()
        if runtime is not None:
            runtime.stop()
        if hardware_lock is not None:
            hardware_lock.release()
    return 0


def _prepare_unix_socket(path: Path) -> None:
    """Remove only an unowned stale socket before Uvicorn binds its endpoint."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() and not path.is_symlink():
        return
    mode = path.lstat().st_mode
    if not stat.S_ISSOCK(mode):
        raise RuntimeError("refusing to replace non-socket path: %s" % path)
    # HardwareProcessLock prevents two real daemons sharing CAN/serial/camera,
    # but it cannot protect a fake process or a manually started server using
    # a different lock path.  Probing the socket first ensures we never unlink
    # a live HTTP endpoint merely because another launch was attempted.
    import socket

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.2)
    try:
        probe.connect(str(path))
    except OSError:
        pass
    else:
        raise RuntimeError("refusing to replace live Unix socket: %s" % path)
    finally:
        probe.close()
    os.unlink(path)


if __name__ == "__main__":
    raise SystemExit(main())
