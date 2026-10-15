#!/usr/bin/env python3
"""通过公开 SDK 对实机推书杆电机进行连通和运动测试。

从仓库根目录运行示例：
    # 仅验证串口可以连接和断开，不会驱动电机。
    python3 scripts/rod/rod_move.py --port /dev/rodmotor

    # 依次转到安全角度 0 度、90 度和 0 度，并验证最终角度反馈。
    python3 scripts/rod/rod_move.py --port /dev/rodmotor --angles 0 90 0

``--angles`` 后可传入一个或多个目标角度，脚本会按给定顺序逐个执行。
仅传入已确认对当前机械结构安全的角度。每个目标位置均会等待实际反馈
落入 ``--tolerance-deg`` 允许范围，否则在超时后报错。
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import sys
from typing import Sequence
import time


# 支持在未安装 BookArm 包的源码仓库中直接运行。
SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from bookarm.devices.rod.service import RodDeviceConfig, RodMotorService


def _finite_number(value: str) -> float:
    try:
        result = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(result):
        raise argparse.ArgumentTypeError("must be finite")
    return result


def _positive_number(value: str) -> float:
    result = _finite_number(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def _positive_integer(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if result <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Connect to a rod motor and optionally move it through target angles."
    )
    parser.add_argument("--port", default="/dev/rodmotor", help="serial device path")
    parser.add_argument("--baudrate", type=_positive_integer, default=921600)
    parser.add_argument(
        "--serial-timeout-s",
        type=_positive_number,
        default=0.2,
        help="read/write timeout for each serial request (default: %(default)s)",
    )
    parser.add_argument(
        "--poll-interval-s",
        type=_positive_number,
        default=0.05,
        help="delay between feedback polls (default: %(default)s)",
    )
    parser.add_argument(
        "--angles",
        type=_finite_number,
        nargs="+",
        metavar="DEG",
        help="target angles to visit in order; omit for a connect/disconnect test only",
    )
    parser.add_argument("--speed", type=_positive_integer, default=1000)
    parser.add_argument("--acceleration", type=_positive_integer, default=50)
    parser.add_argument(
        "--tolerance-deg",
        type=_positive_number,
        default=1.5,
        help="accepted difference between target and feedback (default: %(default)s)",
    )
    parser.add_argument(
        "--move-timeout-s",
        type=_positive_number,
        default=12.0,
        help="maximum wait for each target angle (default: %(default)s)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    service = RodMotorService(
        RodDeviceConfig(
            port=args.port,
            baudrate=args.baudrate,
            timeout_s=args.serial_timeout_s,
            poll_interval_s=args.poll_interval_s,
        )
    )

    try:
        print("Connecting to rod motor on %s at %d baud..." % (args.port, args.baudrate))
        service.start()
        print("Connected.")

        if not args.angles:
            print("Connection test passed; no movement requested.")
            return 0

        for index, target_deg in enumerate(args.angles, start=1):
            print("[%d/%d] Moving rod to %.3f deg..." % (index, len(args.angles), target_deg))
            service.move_to_deg(
                target_deg=target_deg,
                speed=args.speed,
                acceleration=args.acceleration,
                tolerance_deg=args.tolerance_deg,
                timeout_s=args.move_timeout_s,
            )
            print("[%d/%d] Target reached." % (index, len(args.angles)))
            time.sleep(2)
        print("Rod motion and feedback tests passed.")
        return 0
    except KeyboardInterrupt:
        print("Interrupted; the active motor motion may continue. Use the physical emergency stop if needed.", file=sys.stderr)
        return 130
    except Exception as error:
        print("Rod test failed: %s" % error, file=sys.stderr)
        return 1
    finally:
        service.stop()
        print("Disconnected.")


if __name__ == "__main__":
    raise SystemExit(main())
