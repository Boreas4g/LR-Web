"""识别后置校验：书脊倾斜判定 + 车辆居中定长脉冲对齐。

取书/还书/中位还书三种作业在每次视觉识别之后插入本步骤：
1. 倾斜判定——书脊四边形长边与画面竖直方向的夹角超过阈值（默认 60°）
   判定为不可执行，直接报错中止任务（机械臂尚未动作）；
2. 居中对齐——目标书中心不在画面正中指定带宽（默认正中 40%）时，
   通过车端缓速脉冲服务前后移动车辆（此时升降杆在高位，缓启动缓停由
   车端节点与底盘驱动的双斜坡保证），每次脉冲后重新识别，直至居中。

倾斜角算法：四边形 4 角点按环绕序排列后取两条最长边，方向与竖直方向
夹角（atan2(|dx|, |dy|)）取平均，单位度（0°=竖直）。
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, Optional, Sequence, Tuple

from bookarm.domain.models.book import BookTarget, SpineTemplate

log = logging.getLogger(__name__)

PolygonUv = Sequence[Sequence[float]]


def compute_spine_tilt_deg(polygon_uv: Optional[PolygonUv]) -> Optional[float]:
    """书脊倾斜角（与竖直方向夹角，0=竖直，90=水平）；无有效四边形返回 None。"""

    if polygon_uv is None:
        return None
    pts = [tuple(float(c) for c in point[:2]) for point in polygon_uv]
    if len(pts) != 4:
        return None
    cx = sum(p[0] for p in pts) / 4.0
    cy = sum(p[1] for p in pts) / 4.0
    # 按相对质心的极角排序得到环绕序（凸四边形）
    ordered = sorted(pts, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))
    edges = [
        (ordered[(i + 1) % 4][0] - ordered[i][0], ordered[(i + 1) % 4][1] - ordered[i][1])
        for i in range(4)
    ]
    lengths = [math.hypot(*e) for e in edges]
    if min(lengths) <= 0:
        return None
    longest = sorted(range(4), key=lambda i: lengths[i])[-2:]
    tilts = []
    for i in longest:
        dx, dy = edges[i]
        tilts.append(math.degrees(math.atan2(abs(dx), abs(dy))))
    return sum(tilts) / len(tilts)


def center_x_ratio(target: BookTarget) -> Optional[float]:
    """目标书脊中心的水平归一化位置（0..1）；缺数据返回 None。"""

    if target.polygon_uv is None or not target.frame_width_px:
        return None
    cx = sum(float(p[0]) for p in target.polygon_uv) / len(target.polygon_uv)
    return cx / float(target.frame_width_px)


def check_spine_tilt(target: BookTarget, plan: Any) -> Optional[float]:
    """倾斜判定：超过 plan.spine_tilt_max_deg 抛 RuntimeError（不可执行）。

    返回倾斜角（°）或 None（无倾斜信息——兜底轴对齐识别）。"""

    max_deg = float(plan.spine_tilt_max_deg)
    tilt = compute_spine_tilt_deg(target.polygon_uv)
    if tilt is None:
        log.warning("书脊倾斜角不可用（识别兜底分支无倾斜信息）")
        if bool(plan.spine_tilt_fail_on_unknown):
            raise RuntimeError(
                "书脊倾斜角无法计算（识别结果无几何信息），按策略判定为不可执行；"
                "请检查目标书图像与书脊库模板"
            )
        return None
    if tilt > max_deg + 1e-6:  # 恰好等于阈值属可执行（浮点噪声容差）
        raise RuntimeError(
            "书脊倾斜 %.1f°，超过 %.1f° 上限，无法执行：书与书架倾斜过大，"
            "请人工检查书籍是否倒伏" % (tilt, max_deg)
        )
    return tilt


def align_vehicle_to_center(
    vision: Any,
    template: SpineTemplate,
    target: BookTarget,
    vehicle: Any,
    plan: Any,
    *,
    engine: Any = None,
    task: Any = None,
) -> Tuple[BookTarget, Dict[str, Any]]:
    """定长脉冲闭环：目标书移出正中带宽则缓速脉冲动车，直至居中或超限。

    每次动车后重新拍照识别；返回（最终识别结果, 对齐详情）。
    """

    band = float(plan.align_center_band_ratio)
    if not 0.05 <= band <= 0.9:
        raise RuntimeError("align_center_band_ratio %.3f 非法（应 0.05~0.9）" % band)
    lo, hi = 0.5 - band / 2.0, 0.5 + band / 2.0
    speed = float(plan.align_pulse_speed_mps)
    if not 0.005 <= speed <= 0.08:
        raise RuntimeError("align_pulse_speed_mps %.3f 非法（应 0.005~0.08 m/s）" % speed)
    max_moves = int(plan.align_max_moves)
    max_travel_m = float(plan.align_max_travel_m)
    settle_s = float(plan.align_settle_ms) / 1000.0
    min_ms = int(plan.align_pulse_min_ms)
    max_ms = int(plan.align_pulse_max_ms)
    sign = 1 if int(plan.align_direction_sign) >= 0 else -1
    px_to_mm = float(plan.align_px_to_mm)

    moves = 0
    travel_m = 0.0
    pulses = []
    while True:
        ratio = center_x_ratio(target)
        if ratio is None:
            log.warning("目标书居中位置不可用（缺多边形/帧宽），跳过车辆对齐")
            return target, {"aligned": False, "align_skipped": "no_geometry"}
        if lo <= ratio <= hi:
            return target, {
                "aligned": moves > 0,
                "align_moves": moves,
                "align_travel_m": round(travel_m, 4),
                "align_center_x_final": round(ratio, 4),
                "align_pulses": pulses,
            }
        if moves >= max_moves:
            raise RuntimeError(
                "车辆对齐失败：%d 次脉冲后目标书仍未进入画面正中 %.0f%% 区域（当前 %.1f%%）"
                % (moves, band * 100.0, ratio * 100.0)
            )
        # 书在画面右侧（ratio>0.5）时相机需相对书架右移：方向由安装符号决定
        want_forward = (ratio > 0.5) if sign > 0 else (ratio < 0.5)
        direction = "forward" if want_forward else "backward"
        # 偏差像素 → 距离（经验增益） → 脉冲时长，钳制在 [min_ms, max_ms]
        offset_px = abs(ratio - 0.5) * float(target.frame_width_px or 0)
        distance_m = offset_px * px_to_mm / 1000.0
        duration_ms = int(round(distance_m / speed * 1000.0))
        duration_ms = max(min_ms, min(max_ms, duration_ms))
        step_m = speed * duration_ms / 1000.0
        if travel_m + step_m > max_travel_m:
            raise RuntimeError(
                "车辆对齐失败：累计行程将达 %.2f m 上限（已 %.3f m），请检查车位"
                % (max_travel_m, travel_m)
            )
        if engine is not None and task is not None:
            engine.raise_if_cancelled(task)
        vehicle.pulse(direction, duration_ms)
        pulses.append({"direction": direction, "duration_ms": duration_ms})
        travel_m += step_m
        moves += 1
        time.sleep(duration_ms / 1000.0 + settle_s)
        if engine is not None and task is not None:
            engine.raise_if_cancelled(task)
        # 动车后重新识别：位姿与画面都变了
        target = vision.locate_book(template)
        tilt = compute_spine_tilt_deg(target.polygon_uv)
        if tilt is not None and tilt > float(plan.spine_tilt_max_deg) + 1e-6:
            raise RuntimeError(
                "对齐后重新识别发现书脊倾斜 %.1f°，超过 %.1f° 上限，无法执行："
                "请人工检查书籍是否倒伏" % (tilt, float(plan.spine_tilt_max_deg))
            )


def post_vision_check_align(
    vision: Any,
    template: SpineTemplate,
    context: Dict[str, Any],
    context_key: str,
    vehicle: Any,
    plan: Any,
    *,
    engine: Any = None,
    task: Any = None,
) -> Dict[str, Any]:
    """阶段动作入口：对 context[context_key] 中的识别结果做倾斜判定与居中对齐。

    对齐动过车时，context[context_key] 会被替换为对齐后的最新识别结果，
    下游阶段（可达性预检/夹取/插入）始终使用新位姿。
    """

    target = context.get(context_key)
    if not isinstance(target, BookTarget):
        raise RuntimeError("post-vision check requires a BookTarget in context[%r]" % context_key)
    tilt = check_spine_tilt(target, plan)
    details: Dict[str, Any] = {
        "spine_tilt_deg": None if tilt is None else round(tilt, 2),
        "spine_tilt_max_deg": float(plan.spine_tilt_max_deg),
    }
    if vehicle is None:
        # 未接车端对齐服务时仅做倾斜判定（测试/降级环境），不尝试动车
        log.warning("车辆对齐客户端未配置，仅执行书脊倾斜判定")
        details["aligned"] = False
        details["align_skipped"] = "no_vehicle_client"
        return details
    final_target, align_details = align_vehicle_to_center(
        vision, template, target, vehicle, plan, engine=engine, task=task
    )
    context[context_key] = final_target
    details.update(align_details)
    details["final_capture_id"] = final_target.capture_id
    return details
