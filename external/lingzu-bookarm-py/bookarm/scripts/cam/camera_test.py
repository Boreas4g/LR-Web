#!/usr/bin/env python3
"""D435 相机 识别目标书脊

从仓库根目录运行：

启动方式（在 BookArm 仓库根目录执行）：
   python3 scripts/cam/camera_test.py

指定书脊模板与彩色/深度分辨率：
    python3 scripts/cam/camera_test.py \
        --template assets/pic/book_spine/book1.jpeg \
        --width 1280 --height 720 --depth-width 1280 --depth-height 720

无图形界面环境不显示采集画面和模板；若只检查相机采集，可同时跳过端到端定位：
   python3 scripts/cam/camera_test.py --no-display --skip-service

脚本测试 D435Device/D435Frame、BookSpineMatcher、CameraToBaseTransform 和
D435BookSpineLocator。它只会打开 RealSense 相机，不会使能或移动机械臂。
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any, Callable, Optional, TypeVar

import cv2
import numpy as np


BOOKARM_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = BOOKARM_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from bookarm.devices.camera.d435_device import D435Device, D435Frame
from bookarm.devices.camera.matcher import BookSpineMatcher, SpineMatch
from bookarm.devices.camera.service import D435BookSpineLocator, D435BookSpineLocatorConfig
from bookarm.devices.camera.transform import CameraToBaseTransform
from bookarm.domain.models.book import SpineTemplate


DEFAULT_TEMPLATE = BOOKARM_ROOT / "assets" / "pic" / "book_spine" / "book1.jpeg"
DEFAULT_CALIBRATION = BOOKARM_ROOT / "config" / "calibration" / "camera_to_base.example.json"
DEFAULT_OUTPUT_DIR = BOOKARM_ROOT / "recordings" / "camera_test"
T = TypeVar("T")


def top_down_ratio(value: str) -> float:
    """Parse one of the supported book-spine positions measured from its top."""

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


def resolve_path(path: Path) -> Path:
    """将相对路径按 BookArm 根目录解析，保证从任意目录启动时结果一致。"""

    return path if path.is_absolute() else BOOKARM_ROOT / path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="测试 BookArm D435 相机 SDK；不会向机械臂发送运动或使能指令。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    camera = parser.add_argument_group("相机")
    camera.add_argument("--serial", default="262322073961", help="D435 序列号。")
    camera.add_argument("--width", type=int, default=1280, help="彩色流宽度。")
    camera.add_argument("--height", type=int, default=720, help="彩色流高度。")
    camera.add_argument("--depth-width", type=int, default=1280, help="深度流宽度。")
    camera.add_argument("--depth-height", type=int, default=720, help="深度流高度。")
    camera.add_argument("--fps", type=int, default=30, help="RGB-D 帧率。")
    camera.add_argument("--warmup", type=int, default=30, help="自动曝光稳定前丢弃的帧数。")
    camera.add_argument("--timeout-ms", type=int, default=5000, help="单帧采集超时，单位毫秒。")
    camera.add_argument("--no-align", action="store_true", help="不将深度流对齐到彩色流。")

    check = parser.add_argument_group("检查")
    check.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE, help="现场可见的书脊模板图片。")
    check.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION, help="相机到基座的 4x4 外参 JSON。")
    check.add_argument("--pixel", type=int, nargs=2, metavar=("U", "V"), default=None, help="反投影像素；默认为图像中心。")
    check.add_argument("--search-radius", type=int, default=10, help="无效深度的邻域搜索半径，单位像素。")
    check.add_argument("--max-depth-m", type=float, default=2.0, help="点云和定位接受的最大深度，单位米。")
    check.add_argument("--stride", type=int, default=1, help="点云像素采样步长。")
    check.add_argument("--max-attempts", type=int, default=10, help="服务层书脊定位的最大采集次数。")
    check.add_argument(
        "--grasp-point-from-top",
        type=top_down_ratio,
        choices=(
            BookSpineMatcher.TOP_DOWN_ONE_SIXTH,
            BookSpineMatcher.TOP_DOWN_ONE_FIFTH,
            BookSpineMatcher.TOP_DOWN_TWO_FIFTHS,
            BookSpineMatcher.TOP_DOWN_ONE_QUARTER,
            BookSpineMatcher.TOP_DOWN_ONE_THIRD,
            BookSpineMatcher.TOP_DOWN_ONE_HALF,
            BookSpineMatcher.TOP_DOWN_TWO_THIRDS,
        ),
        default=BookSpineMatcher.TOP_DOWN_ONE_SIXTH,
        metavar="{1/6,1/5,2/5,1/4,1/3,1/2,2/3}",
        help="书脊抓取点距顶部的比例。",
    )
    check.add_argument("--skip-service", action="store_true", help="跳过 D435BookSpineLocator.locate_book() 端到端检查。")
    check.add_argument(
        "--no-display",
        action="store_true",
        help="不显示采集画面和目标书脊模板，适用于无图形界面环境。",
    )

    output = parser.add_argument_group("输出")
    output.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="诊断文件与 JSON 报告的根目录。")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    for name in ("width", "height", "depth_width", "depth_height", "fps", "timeout_ms"):
        if getattr(args, name) <= 0:
            raise ValueError("--%s 必须为正数。" % name.replace("_", "-"))
    if args.warmup < 0:
        raise ValueError("--warmup 必须大于等于 0。")
    if args.search_radius < 0:
        raise ValueError("--search-radius 必须大于等于 0。")
    if args.max_depth_m <= 0.0:
        raise ValueError("--max-depth-m 必须大于 0。")
    if args.stride < 1:
        raise ValueError("--stride 必须大于等于 1。")
    if args.max_attempts < 1:
        raise ValueError("--max-attempts 必须大于等于 1。")
    if not args.template.is_file():
        raise FileNotFoundError("书脊模板不存在：%s" % args.template)
    if not args.calibration.is_file():
        raise FileNotFoundError("相机外参不存在：%s" % args.calibration)


def to_json(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): to_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json(item) for item in value]
    return value


def run_check(
    report: dict[str, Any],
    name: str,
    action: Callable[[], T],
    report_value: Optional[Callable[[T], Any]] = None,
) -> Optional[T]:
    try:
        value = action()
    except Exception as error:
        report["checks"][name] = {
            "ok": False,
            "error_type": type(error).__name__,
            "message": str(error),
        }
        report["failures"].append(name)
        print("[FAIL] %s: %s" % (name, error), file=sys.stderr)
        return None
    result = value if report_value is None else report_value(value)
    report["checks"][name] = {"ok": True, "result": to_json(result)}
    print("[OK] %s" % name)
    return value


def capture_frame(args: argparse.Namespace) -> tuple[D435Frame, dict[str, Any]]:
    camera = D435Device(
        serial=args.serial,
        width=args.width,
        height=args.height,
        depth_width=args.depth_width,
        depth_height=args.depth_height,
        fps=args.fps,
        align_depth_to_color=not args.no_align,
    )
    details: dict[str, Any] = {"started_before": camera.started}
    try:
        camera.start()
        if not camera.started:
            raise RuntimeError("D435Device.start() 返回后相机仍未启动。")
        details["started_after_start"] = camera.started
        camera.warmup(args.warmup, args.timeout_ms)
        frame = camera.capture(args.timeout_ms)
        expected_color_shape = (args.height, args.width, 3)
        if frame.color_bgr.shape != expected_color_shape:
            raise RuntimeError("彩色帧尺寸错误：%s，期望 %s" % (frame.color_bgr.shape, expected_color_shape))
        expected_depth_shape = (
            (args.height, args.width)
            if not args.no_align
            else (args.depth_height, args.depth_width)
        )
        if frame.depth_m.shape != expected_depth_shape:
            raise RuntimeError("深度帧尺寸错误：%s，期望 %s" % (frame.depth_m.shape, expected_depth_shape))
        if not all(np.isfinite(value) and value > 0.0 for value in (frame.fx, frame.fy)):
            raise RuntimeError("相机内参 fx/fy 无效。")
        details.update(
            color_shape=frame.color_bgr.shape,
            depth_shape=frame.depth_m.shape,
            frame_number=frame.frame_number,
            timestamp_ms=frame.timestamp_ms,
            intrinsics={"fx": frame.fx, "fy": frame.fy, "ppx": frame.ppx, "ppy": frame.ppy},
        )
        return frame, details
    finally:
        camera.stop()
        details["stopped_after_stop"] = not camera.started


def resize_for_display(image: np.ndarray, max_width: int, max_height: int) -> np.ndarray:
    """等比例缩小图像，确保采集画面和模板可同时显示。"""

    height, width = image.shape[:2]
    scale = min(1.0, max_width / width, max_height / height)
    if scale == 1.0:
        return image.copy()
    return cv2.resize(image, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)


def labeled_display_image(image: np.ndarray, label: str, width: int, height: int) -> np.ndarray:
    """将图像置于统一大小的带标题画布中，便于横向比对。"""

    canvas = np.full((height + 40, width, 3), 245, dtype=np.uint8)
    resized = resize_for_display(image, width, height)
    image_height, image_width = resized.shape[:2]
    x = (width - image_width) // 2
    y = 40 + (height - image_height) // 2
    canvas[y : y + image_height, x : x + image_width] = resized
    cv2.putText(canvas, label, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (20, 20, 20), 2, cv2.LINE_AA)
    return canvas


def display_captured_and_template(
    frame: D435Frame,
    template_path: Path,
    grasp_point_from_top_ratio: float,
) -> dict[str, Any]:
    """先比对采集画面和模板，再在原图上显示书脊匹配结果。"""

    template = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
    if template is None:
        raise RuntimeError("无法读取书脊模板图片：%s" % template_path)

    panel_width, panel_height = 800, 600
    comparison = np.hstack(
        (
            labeled_display_image(frame.color_bgr, "D435 采集画面", panel_width, panel_height),
            labeled_display_image(template, "目标书脊模板", panel_width, panel_height),
        )
    )
    window_name = "D435 采集画面与目标书脊模板"
    window_created = False
    try:
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        window_created = True
        cv2.imshow(window_name, comparison)
        print("已显示采集画面（左）和目标书脊模板（右）；按任意键显示原图中的书脊匹配框。")
        comparison_key = cv2.waitKey(0)

        matcher = BookSpineMatcher(template_path)
        match = matcher.match(frame.color_bgr)
        overlay = frame.color_bgr.copy()
        details: dict[str, Any] = {
            "template_shape": template.shape,
            "comparison_shape": comparison.shape,
            "key_code": int(comparison_key),
            "comparison_key_code": int(comparison_key),
        }
        if match is None:
            cv2.putText(
                overlay,
                "No matching book spine found",
                (24, 48),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.0,
                (0, 0, 255),
                2,
                cv2.LINE_AA,
            )
            details["matched"] = False
            print("未匹配到书脊模板；已显示原始大图。按任意键继续后续检查。")
        else:
            polygon = np.asarray(match.polygon_uv, dtype=np.int32).reshape(-1, 1, 2)
            grasp_pixel = matcher.pick_point(match.polygon_uv, grasp_point_from_top_ratio)
            cv2.polylines(overlay, [polygon], True, (0, 255, 0), 4, cv2.LINE_AA)
            cv2.circle(overlay, grasp_pixel, 8, (0, 0, 255), -1, cv2.LINE_AA)
            details.update(
                matched=True,
                confidence=match.confidence,
                polygon_uv=match.polygon_uv,
                grasp_pixel_uv=grasp_pixel,
            )
            print("已在原始大图中用绿色框标出匹配书脊，红点为取书点；按任意键继续后续检查。")

        cv2.imshow(window_name, overlay)
        details["result_key_code"] = int(cv2.waitKey(0))
        return details
    finally:
        if window_created:
            try:
                cv2.destroyWindow(window_name)
            except cv2.error:
                # 无图形界面或窗口已被用户关闭时，不应覆盖原始的显示错误。
                pass


def point_at_pixel(frame: D435Frame, args: argparse.Namespace) -> dict[str, Any]:
    height, width = frame.depth_m.shape
    u, v = args.pixel if args.pixel is not None else (width // 2, height // 2)
    point_m = frame.point_at_pixel(u, v, search_radius=args.search_radius)
    return {
        "requested_pixel_uv": [u, v],
        "camera_point_m": point_m,
        "camera_point_mm": [coordinate * 1000.0 for coordinate in point_m],
    }


def create_point_cloud(frame: D435Frame, args: argparse.Namespace) -> tuple[np.ndarray, dict[str, Any]]:
    cloud = frame.point_cloud(max_depth_m=args.max_depth_m, stride=args.stride)
    if cloud.size == 0:
        raise RuntimeError("点云为空，请调整相机视角或 --max-depth-m。")
    return cloud, {
        "point_count": int(cloud.shape[0]),
        "stride": args.stride,
        "max_depth_m": args.max_depth_m,
        "min_xyz_m": np.min(cloud, axis=0),
        "max_xyz_m": np.max(cloud, axis=0),
    }


def match_book_spine(
    frame: D435Frame,
    template: Path,
    grasp_point_from_top_ratio: float,
) -> tuple[SpineMatch, dict[str, Any]]:
    matcher = BookSpineMatcher(template)
    match = matcher.match(frame.color_bgr)
    if match is None:
        raise RuntimeError("当前相机画面未匹配到书脊模板；请将目标书脊置于视野内。")
    grasp_pixel = matcher.pick_point(match.polygon_uv, grasp_point_from_top_ratio)
    return match, {
        "confidence": match.confidence,
        "polygon_uv": match.polygon_uv,
        "grasp_pixel_uv": grasp_pixel,
    }


def save_artifacts(
    run_dir: Path,
    frame: D435Frame,
    point_cloud: Optional[np.ndarray],
    match: Optional[SpineMatch],
    max_depth_m: float,
    grasp_point_from_top_ratio: float,
) -> dict[str, Path]:
    paths = {
        "color_bgr": run_dir / "color_bgr.png",
        "depth_m": run_dir / "depth_m.npy",
        "depth_visualization": run_dir / "depth_visualization.png",
    }
    if not cv2.imwrite(str(paths["color_bgr"]), frame.color_bgr):
        raise RuntimeError("保存彩色图失败：%s" % paths["color_bgr"])
    np.save(paths["depth_m"], frame.depth_m)

    valid = np.isfinite(frame.depth_m) & (frame.depth_m > 0.0)
    depth_image = cv2.applyColorMap(
        (np.clip(frame.depth_m / max_depth_m, 0.0, 1.0) * 255.0).astype(np.uint8),
        cv2.COLORMAP_JET,
    )
    depth_image[~valid] = (0, 0, 0)
    if not cv2.imwrite(str(paths["depth_visualization"]), depth_image):
        raise RuntimeError("保存深度伪彩图失败：%s" % paths["depth_visualization"])

    if point_cloud is not None:
        paths["point_cloud"] = run_dir / "point_cloud_m.npz"
        np.savez_compressed(paths["point_cloud"], points_xyz_m=point_cloud)

    if match is not None:
        overlay = frame.color_bgr.copy()
        polygon = np.asarray(match.polygon_uv, dtype=np.int32).reshape(-1, 1, 2)
        pixel = BookSpineMatcher.pick_point(match.polygon_uv, grasp_point_from_top_ratio)
        cv2.polylines(overlay, [polygon], True, (0, 255, 0), 2, cv2.LINE_AA)
        cv2.circle(overlay, pixel, 6, (0, 0, 255), -1, cv2.LINE_AA)
        paths["book_spine_match"] = run_dir / "book_spine_match.png"
        if not cv2.imwrite(str(paths["book_spine_match"]), overlay):
            raise RuntimeError("保存书脊匹配图失败：%s" % paths["book_spine_match"])
    return paths


def service_locate_book(args: argparse.Namespace) -> dict[str, Any]:
    config = D435BookSpineLocatorConfig(
        serial=args.serial,
        calibration_path=args.calibration,
        width=args.width,
        height=args.height,
        depth_width=args.depth_width,
        depth_height=args.depth_height,
        fps=args.fps,
        warmup_frames=args.warmup,
        frame_timeout_ms=args.timeout_ms,
        max_attempts=args.max_attempts,
        max_depth_m=args.max_depth_m,
        point_cloud_stride=args.stride,
        grasp_point_from_top_ratio=args.grasp_point_from_top,
    )
    service = D435BookSpineLocator(config)
    try:
        service.start()
        target = service.locate_book(SpineTemplate(args.template))
    finally:
        service.stop()
    result = {
        "pixel_uv": target.pixel_uv,
        "confidence": target.confidence,
        "capture_id": target.capture_id,
        "base_pose_m": [target.raw_pose.x_m, target.raw_pose.y_m, target.raw_pose.z_m],
    }
    base_x_m, base_y_m, base_z_m = result["base_pose_m"]
    print("D435BookSpineLocator 定位结果：")
    print("  目标像素 (u, v)：%s" % (result["pixel_uv"],))
    print("  基座坐标（米）：x=%.6f, y=%.6f, z=%.6f" % (base_x_m, base_y_m, base_z_m))
    print("  基座坐标（毫米）：x=%.1f, y=%.1f, z=%.1f" % (base_x_m * 1000.0, base_y_m * 1000.0, base_z_m * 1000.0))
    print("  匹配置信度：%.3f；采集 ID：%s" % (result["confidence"], result["capture_id"]))
    return result


def write_report(run_dir: Path, report: dict[str, Any]) -> Path:
    path = run_dir / "report.json"
    path.write_text(json.dumps(to_json(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    args = parse_args()
    args.template = resolve_path(args.template)
    args.calibration = resolve_path(args.calibration)
    args.output_dir = resolve_path(args.output_dir)
    try:
        validate_args(args)
    except (ValueError, FileNotFoundError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 2

    run_dir = args.output_dir / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "run_directory": run_dir,
        "camera": {
            "serial": args.serial,
            "color_size": [args.width, args.height],
            "depth_size": [args.depth_width, args.depth_height],
            "fps": args.fps,
            "depth_aligned_to_color": not args.no_align,
        },
        "template": args.template,
        "grasp_point_from_top_ratio": args.grasp_point_from_top,
        "calibration": args.calibration,
        "checks": {},
        "failures": [],
    }

    print("开始 BookArm D435 SDK 自检；本脚本不会移动或使能机械臂。")
    print("输出目录：%s" % run_dir)
    try:
        captured = run_check(
            report,
            "D435Device.start_warmup_capture_stop",
            lambda: capture_frame(args),
            lambda value: value[1],
        )
        frame: Optional[D435Frame] = None
        if captured is not None:
            frame, _capture_details = captured

        if frame is not None:
            if args.no_display:
                report["checks"]["display_captured_and_template"] = {
                    "ok": True,
                    "skipped": True,
                    "message": "已按 --no-display 跳过画面显示。",
                }
            else:
                run_check(
                    report,
                    "display_captured_and_template",
                    lambda: display_captured_and_template(
                        frame,
                        args.template,
                        args.grasp_point_from_top,
                    ),
                )

        transform = run_check(
            report,
            "CameraToBaseTransform.from_json",
            lambda: CameraToBaseTransform.from_json(args.calibration),
            lambda value: {"base_origin_m": value.transform_point((0.0, 0.0, 0.0))},
        )

        selected_point: Optional[dict[str, Any]] = None
        point_cloud: Optional[np.ndarray] = None
        spine_match: Optional[SpineMatch] = None
        if frame is not None:
            selected_point = run_check(
                report,
                "D435Frame.point_at_pixel",
                lambda: point_at_pixel(frame, args),
            )
            cloud = run_check(
                report,
                "D435Frame.point_cloud",
                lambda: create_point_cloud(frame, args),
                lambda value: value[1],
            )
            if cloud is not None:
                point_cloud, _cloud_details = cloud
            matched = run_check(
                report,
                "BookSpineMatcher.match_and_pick_point",
                lambda: match_book_spine(frame, args.template, args.grasp_point_from_top),
                lambda value: value[1],
            )
            if matched is not None:
                spine_match, match_details = matched
                run_check(
                    report,
                    "D435Frame.point_at_matched_book_spine",
                    lambda: {
                        "grasp_pixel_uv": match_details["grasp_pixel_uv"],
                        "camera_point_m": frame.point_at_pixel(*match_details["grasp_pixel_uv"], search_radius=args.search_radius),
                    },
                )

        if transform is not None and selected_point is not None:
            run_check(
                report,
                "CameraToBaseTransform.transform_point",
                lambda: {
                    "camera_point_m": selected_point["camera_point_m"],
                    "base_point_m": transform.transform_point(selected_point["camera_point_m"]),
                },
            )

        if frame is not None:
            run_check(
                report,
                "save_diagnostics",
                lambda: save_artifacts(
                    run_dir,
                    frame,
                    point_cloud,
                    spine_match,
                    args.max_depth_m,
                    args.grasp_point_from_top,
                ),
            )

        if args.skip_service:
            report["checks"]["D435BookSpineLocator.locate_book"] = {
                "ok": True,
                "skipped": True,
                "message": "已按 --skip-service 跳过。",
            }
        else:
            run_check(report, "D435BookSpineLocator.locate_book", lambda: service_locate_book(args))
    except KeyboardInterrupt:
        report["failures"].append("interrupted")
        print("\n测试已由用户中断。", file=sys.stderr)
    finally:
        report["finished_at"] = datetime.now().isoformat(timespec="seconds")
        report["passed"] = not report["failures"]
        report_path = write_report(run_dir, report)

    if report["passed"]:
        print("全部 BookArm 相机 SDK 检查通过。报告：%s" % report_path)
        return 0
    print("相机 SDK 检查失败：%s。报告：%s" % (", ".join(report["failures"]), report_path), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
