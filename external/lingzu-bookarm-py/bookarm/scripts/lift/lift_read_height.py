#!/usr/bin/env python3
"""读取 YZ-AIM 升降台当前标定高度，不发送任何运动或使能命令。

从 BookArm 仓库根目录运行：

    python3 scripts/lift/lift_read_height.py --port /dev/lift_port

也可以加载部署配置：

    python3 scripts/lift/lift_read_height.py --config config/devices/lift.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Optional, Sequence


BOOKARM_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = BOOKARM_ROOT / "src"
AIM_SDK_SOURCE_ROOT = BOOKARM_ROOT.parent.parent / "aim_sdk"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))
if str(AIM_SDK_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(AIM_SDK_SOURCE_ROOT))

from bookarm.devices.lift import AimLiftService, LiftDeviceConfig


def positive_integer(value: str) -> int:
    try:
        result = int(value, 0)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if result <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def positive_float(value: str) -> float:
    try:
        result = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if result <= 0.0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="通过 AimLiftService.read_lift_height_mm() 读取升降台当前高度；不会使能或移动升降台。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", type=Path, help="可选 LiftDeviceConfig JSON；命令行参数优先。")
    parser.add_argument("--port", help="RS485 串口，例如 /dev/lift_port。")
    parser.add_argument("--address", type=positive_integer, help="Modbus 驱动地址。")
    parser.add_argument("--baudrate", type=positive_integer, help="RS485 波特率。")
    parser.add_argument("--timeout-s", type=positive_float, help="单次 Modbus 请求超时，单位秒。")
    parser.add_argument("--retries", type=int, help="Modbus 重试次数。")
    return parser.parse_args(argv)


def resolve_path(path: Path) -> Path:
    return path if path.is_absolute() else BOOKARM_ROOT / path


def load_config(path: Optional[Path]) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError("cannot read config file %s: %s" % (path, error)) from error
    except json.JSONDecodeError as error:
        raise ValueError("invalid JSON config %s: %s" % (path, error)) from error
    if not isinstance(payload, dict):
        raise ValueError("lift config must be a JSON object")
    unknown = sorted(set(payload) - set(LiftDeviceConfig.__dataclass_fields__))
    if unknown:
        raise ValueError("unsupported lift config fields: %s" % ", ".join(unknown))
    return dict(payload)


def build_config(args: argparse.Namespace) -> LiftDeviceConfig:
    values = load_config(args.config)
    for option, field in (
        ("port", "port"),
        ("address", "address"),
        ("baudrate", "baudrate"),
        ("timeout_s", "timeout_s"),
        ("retries", "retries"),
    ):
        value = getattr(args, option)
        if value is not None:
            values[field] = value
    try:
        config = LiftDeviceConfig(**values)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid lift config: %s" % error) from error
    if not 1 <= int(config.address) <= 247:
        raise ValueError("lift address must be in 1..247")
    if int(config.retries) < 0:
        raise ValueError("lift retries must not be negative")
    return config


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.config is not None:
        args.config = resolve_path(args.config)
    try:
        config = build_config(args)
    except ValueError as error:
        print("配置错误：%s" % error, file=sys.stderr)
        return 2

    lift = AimLiftService(config)
    try:
        lift.start()
        height_mm = lift.read_lift_height_mm()
    except KeyboardInterrupt:
        print("读取已由用户中断。", file=sys.stderr)
        return 130
    except Exception as error:
        print("读取升降台高度失败：%s" % error, file=sys.stderr)
        return 1
    finally:
        try:
            lift.stop()
        except Exception as error:
            print("关闭升降台连接失败：%s" % error, file=sys.stderr)

    print("升降台当前高度：%.3f mm" % height_mm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
