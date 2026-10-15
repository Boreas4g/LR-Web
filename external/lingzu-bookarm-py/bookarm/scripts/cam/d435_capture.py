#!/usr/bin/env python3
"""使用 Intel RealSense D435 拍摄一张彩色照片。

在 BookArm 仓库根目录运行：

    python3 scripts/cam/d435_capture.py --width 1280 --height 720

当前 D435 的彩色相机使用本脚本的 BGR8 输出时，支持以下 ``--width`` 和
``--height`` 分辨率组合；每个分辨率后列出了可用的 ``--fps`` 帧率：

    1920x1080：6、15、30 FPS
    1280x720： 6、15、30 FPS
    960x540：  6、15、30、60 FPS
    848x480：  6、15、30、60 FPS
    640x480：  6、15、30、60 FPS
    640x360：  6、15、30、60 FPS
    424x240：  6、15、30、60 FPS
    320x240：  6、30、60 FPS
    320x180：  6、30、60 FPS

通过 ``--width`` 和 ``--height`` 指定彩色流分辨率。若 D435 不支持所选帧率下
的该分辨率，RealSense 会在保存照片前提示配置错误。本脚本只会访问相机，不会
与机械臂通信或移动机械臂。
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys
from typing import Sequence

import cv2
import numpy as np


BOOKARM_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = BOOKARM_ROOT / "recordings" / "cam"


def positive_integer(value: str) -> int:
    """Parse a strictly positive command-line integer."""

    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if result <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def nonnegative_integer(value: str) -> int:
    """Parse a nonnegative command-line integer."""

    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if result < 0:
        raise argparse.ArgumentTypeError("must be greater than or equal to zero")
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="拍摄一张 Intel RealSense D435 彩色照片；不会控制机械臂。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--serial",
        default=None,
        help="D435 序列号；省略时使用系统检测到的第一台 RealSense 相机。",
    )
    parser.add_argument("--width", type=positive_integer, default=1280, help="拍摄宽度（像素）。")
    parser.add_argument("--height", type=positive_integer, default=720, help="拍摄高度（像素）。")
    parser.add_argument("--fps", type=positive_integer, default=30, help="彩色流帧率。")
    parser.add_argument(
        "--warmup-frames",
        type=nonnegative_integer,
        default=30,
        help="拍照前丢弃的预热帧数，用于稳定自动曝光。",
    )
    parser.add_argument(
        "--timeout-ms",
        type=positive_integer,
        default=5000,
        help="等待每一帧的最长时间（毫秒）。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="照片输出路径；相对路径按 BookArm 根目录解析。省略时保存到 recordings/cam/。",
    )
    return parser.parse_args(argv)


def output_path(args: argparse.Namespace) -> Path:
    """Choose a collision-resistant default filename when no path was supplied."""

    if args.output is not None:
        return args.output if args.output.is_absolute() else BOOKARM_ROOT / args.output
    filename = "d435_%s_%dx%d.png" % (
        datetime.now().strftime("%Y%m%d_%H%M%S_%f"),
        args.width,
        args.height,
    )
    return DEFAULT_OUTPUT_DIR / filename


def capture_photo(args: argparse.Namespace, destination: Path) -> tuple[int, int]:
    """Start a color-only pipeline, discard warmup frames, and save one BGR frame."""

    try:
        import pyrealsense2 as rs
    except ImportError as error:
        raise RuntimeError("拍摄 D435 照片需要安装 pyrealsense2。") from error

    pipeline = rs.pipeline()
    config = rs.config()
    if args.serial:
        config.enable_device(args.serial)
    config.enable_stream(rs.stream.color, args.width, args.height, rs.format.bgr8, args.fps)

    started = False
    try:
        try:
            pipeline.start(config)
            started = True
        except Exception as error:
            device_description = "序列号为 %s 的 D435" % args.serial if args.serial else "D435"
            raise RuntimeError(
                "无法以 %dx%d @ %d FPS 启动%s：%s"
                % (args.width, args.height, args.fps, device_description, error)
            ) from error

        for _ in range(args.warmup_frames):
            pipeline.wait_for_frames(args.timeout_ms)

        frames = pipeline.wait_for_frames(args.timeout_ms)
        color_frame = frames.get_color_frame()
        if not color_frame:
            raise RuntimeError("D435 未返回彩色帧。")
        image_bgr = np.asanyarray(color_frame.get_data())
        if image_bgr.ndim != 3 or image_bgr.shape[2] != 3:
            raise RuntimeError("D435 返回的彩色帧格式无效：%s" % (image_bgr.shape,))

        destination.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(destination), image_bgr):
            raise RuntimeError("无法保存照片：%s" % destination)
        height, width = image_bgr.shape[:2]
        return width, height
    finally:
        if started:
            pipeline.stop()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    destination = output_path(args)
    try:
        width, height = capture_photo(args, destination)
    except KeyboardInterrupt:
        print("拍照已由用户中断。", file=sys.stderr)
        return 130
    except Exception as error:
        print("拍照失败：%s" % error, file=sys.stderr)
        return 1

    print("已保存 D435 照片：%s" % destination)
    print("实际分辨率：%dx%d" % (width, height))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
