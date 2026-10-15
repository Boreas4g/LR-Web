"""Translate raw bookarm/navigation failure strings into operator Chinese.

The bookarm daemon and the edge gateway report failures as English technical
strings (IK solver output, device-driver errors, status words). Operators
watching the takeout/return flows should see what actually went wrong and
what to do next, so every known pattern is mapped here; anything unknown
falls through verbatim (still wrapped by the caller's Chinese label).
"""

from __future__ import annotations

import math
import re

_CJK_RE = re.compile(r"[一-鿿]")

_BOOKARM_STATE_LABELS = {
    "failed": "执行失败",
    "recovery_required": "需要人工接管恢复（机械臂可能夹着书停在半路）",
    "cancelled": "已取消",
    "unsupported": "当前服务不支持该操作",
}

_TASK_STATUS_LABELS = {
    "failed": "执行失败",
    "paused": "被暂停（可能触发急停或人工干预）",
    "cancelled": "已取消",
}

_TAKE_BOOK_STEP_LABELS = {
    "rod_home": "拨杆回零",
    "arm_pregrasp": "机械臂预抓运动",
    "rod_hook": "拨杆勾书",
    "arm_fine_grasp": "机械臂精细夹取",
    "arm_post_grasp": "夹取后回撤",
    "gripper_close": "夹爪闭合",
}

# Reachability-precheck pose labels, as emitted by bookarm's
# ``arm pose unreachable (<label>)`` errors, grouped by the pose chain they
# belong to so the advice points at the right knob (book height vs stand-slot
# calibration vs shelf target).
_REACHABILITY_STEP_LABELS = {
    "arm_pregrasp": "取书·预抓位",
    "arm_approach": "取书·接近位",
    "arm_fine_grasp": "取书·精夹位",
    "arm_post_grasp": "取书·夹后回撤位",
    "stand_pre_place": "放入书立·预放位",
    "stand_place": "放入书立·放置位",
    "stand_lower_place": "放入书立·下放位",
    "shelf_pre_place": "还书上架·预放位",
    "shelf_place": "还书上架·放置位",
    "pre_insert": "中位还书·预插入位",
    "insert": "中位还书·插入位",
    "push": "中位还书·推入位",
    "retract": "中位还书·回撤位",
}

_REACHABILITY_STEP_ADVICE = (
    (
        {"stand_pre_place", "stand_place", "stand_lower_place"},
        "该位姿来自书立槽位标定，与书籍位置/层高无关，"
        "请到调参页重新标定该书立槽位，或改用其他槽位后重试",
    ),
    (
        {"arm_pregrasp", "arm_approach", "arm_fine_grasp", "arm_post_grasp"},
        "请检查书籍位置/层高是否超出机械臂工作范围",
    ),
    (
        {"shelf_pre_place", "shelf_place"},
        "请检查还书目标书架位置/层高是否超出机械臂工作范围",
    ),
    (
        {"pre_insert", "insert", "push", "retract"},
        "请检查还书插入目标位置/层高是否超出机械臂工作范围",
    ),
)


def _has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text))


def _describe_unreachable_pose(label: str, inner: str) -> str:
    """Spell out one reachability-precheck rejection: which pose, which values.

    bookarm raises ``arm pose unreachable (<label>): <reason>`` before sending
    any motion.  The reason is either an IK non-convergence carrying the target
    pose tuple (metres/radians), or a joint-limit violation naming the joint.
    Surface both in operator units (mm/degrees) so the failing number is
    visible on the page instead of only in the daemon log.
    """
    step = _REACHABILITY_STEP_LABELS.get(label, label)
    parts = [f"机械臂可达性预检未通过：「{step}」（{label}）不可达"]
    joint_match = re.search(
        r"arm joint (\d+) target ([-\d.]+) rad is outside \[([-\d.]+), ([-\d.]+)\]", inner
    )
    if joint_match:
        value, minimum, maximum = (math.degrees(float(item)) for item in joint_match.groups()[1:])
        parts.append(
            f"解算结果第 {joint_match.group(1)} 轴需要 {value:.1f}°，"
            f"超出允许范围 [{minimum:.1f}°, {maximum:.1f}°]"
        )
    else:
        target_match = re.search(r"for target \(([^)]+)\)", inner)
        values = []
        if target_match:
            try:
                values = [float(item) for item in target_match.group(1).split(",")]
            except ValueError:
                values = []
        if len(values) == 6:
            x_m, y_m, z_m, rx, ry, rz = values
            parts.append(
                f"目标位姿 x={x_m * 1000:.0f}mm y={y_m * 1000:.0f}mm z={z_m * 1000:.0f}mm "
                f"rx={math.degrees(rx):.1f}° ry={math.degrees(ry):.1f}° rz={math.degrees(rz):.1f}°，"
                "运动学解算不收敛，该位置/姿态组合超出机械臂工作空间"
            )
        else:
            parts.append("运动学解算不收敛，目标位姿超出机械臂工作空间")
    advice = next(
        (text for labels, text in _REACHABILITY_STEP_ADVICE if label in labels),
        "请检查目标位置/层高是否超出机械臂工作范围",
    )
    parts.append(advice)
    parts.append("未执行任何动作")
    return "；".join(parts)


def describe_task_status(status: str) -> str:
    """Human label for a navigation / fine-tune task status word."""
    return _TASK_STATUS_LABELS.get(status, status)


def describe_bookarm_state(state: str) -> str:
    """Human label for a bookarm terminal state (no detail available)."""
    return _BOOKARM_STATE_LABELS.get(state, state)


def describe_bookarm_detail(detail: str) -> str:
    """Translate one raw bookarm error string into actionable Chinese."""
    text = detail.strip()

    if "unknown template_id" in text:
        return "书脊模板不存在，请重新录入该书后再试"
    if "gripper closed without contact" in text:
        return "未夹到书：槽位可能无书或位置偏移，请确认槽位有书且书的位置正确后重试"
    if "already running" in text:
        return "机械臂正在执行上一个任务，请稍候重试"
    match = re.search(r"arm pose unreachable \(([^)]+)\)(?::\s*(.*))?", text, re.DOTALL)
    if match:
        return _describe_unreachable_pose(match.group(1), (match.group(2) or "").strip())
    if "inverse kinematics" in text or "did not converge" in text:
        return (
            "机械臂无法到达目标位置：运动学解算失败，目标点超出工作空间或姿态受关节限位限制。"
            "请调整目标位置、书架高度或插入 Y 偏移后重试"
        )
    match = re.search(r"final joint error ([\d.]+) rad exceeds ([\d.]+) rad", text)
    if match:
        return (
            f"机械臂没有精确停到位（关节误差 {match.group(1)} rad，超过允许值 "
            f"{match.group(2)} rad），可能是运动途中受到阻挡，请检查机械臂周边后重试"
        )
    if "夹爪空夹" in text:
        return (
            "夹爪没有夹到书：夹爪已闭合到极限仍未检测到书本，"
            "请确认槽位或书架上确实有书、书的位置没有明显偏移"
        )
    if "no valid D435 depth" in text:
        return (
            "深度相机在目标位置取不到有效深度数据，"
            "请确认相机视野无遮挡、目标书在识别范围内后重试"
        )
    if (
        "Device disconnected" in text
        or "Failed to reconnect" in text
        or "xioctl" in text
        or "UVCIOC_CTRL_QUERY" in text
    ):
        return (
            "深度相机 USB 连接中断且重连失败：请检查相机 USB 线是否插紧、"
            "尽量直连主板 USB3 口（避免多级 Hub 和延长线）、确认供电充足；"
            "相机掉线后需要重新插拔，等系统重新识别出相机后再重试任务"
        )
    match = re.search(r"book spine was not detected within (\d+) D435 captures", text)
    if match:
        return (
            f"连续 {match.group(1)} 次拍照都没有识别到目标书脊，"
            "请确认书脊模板与实际图书一致、相机视野清晰"
        )
    match = re.search(r"book-grasp step (\w+) failed: (.*)", text, re.DOTALL)
    if match:
        step = _TAKE_BOOK_STEP_LABELS.get(match.group(1), match.group(1))
        inner = describe_bookarm_detail(match.group(2))
        return f"取书动作在「{step}」步骤失败：{inner}"
    if re.search(r"\brod\b.*failed", text) or "rod command failed" in text:
        return "拨杆（勾书杆）动作失败：拨杆电机无响应，请检查拨杆是否被卡住或接线松动"
    if "书脊倾斜" in text:
        return (
            "书与书架倾斜超过允许上限（60°），判定为不可执行，本次未执行任何动作；"
            "请人工检查该书是否倒伏或斜插，扶正后再试"
        )
    if "车辆对齐失败" in text:
        return (
            "车辆多次缓移后目标书仍未到画面正中区域，已中止；"
            "请检查车辆位置是否偏差过大、目标书是否在相机视野内"
        )
    if "车辆对齐服务不可达" in text:
        return "车辆对齐服务未就绪：请确认车端运行服务（lr-robot-runtime）已启动后重试"
    if "reserved by another controller" in text:
        return "机械臂正被另一个控制端占用，请先结束占用方的操作再重试"
    if "AIM lift motion failed" in text:
        return "升降台动作执行失败，请检查升降台是否被阻挡、控制器是否报警"
    if "AIM lift communication failed" in text:
        return "升降台通信失败，请检查升降台控制器的串口/接线"
    if "AIM lift" in text and "not connected" in text:
        return "升降台未连接，请检查升降台控制器的电源与接线"
    match = re.search(r"arm joint (\d+) reports fault code (\d+)", text)
    if match:
        return (
            f"机械臂第 {match.group(1)} 轴上报故障（故障码 {match.group(2)}），"
            "请排除故障并复位机械臂后重试"
        )
    match = re.search(r"arm feedback for joint (\d+) is stale", text)
    if match:
        return (
            f"机械臂第 {match.group(1)} 轴反馈信号中断，"
            "请检查 CAN 总线连接，必要时重启机械臂服务"
        )
    if "arm feedback" in text and "stale" in text:
        return "机械臂关节反馈信号中断，请检查 CAN 总线连接，必要时重启机械臂服务"
    if "arm enable failed" in text:
        return "机械臂使能失败：各轴未全部就绪，请检查机械臂电源与 CAN 总线连接"
    match = re.search(r"book-stand (?:pickup )?slot (\d+) is not calibrated", text)
    if match:
        return f"书立槽位 {match.group(1)} 尚未标定，请先在调参页完成槽位标定"
    if "camera calibration is not loaded" in text:
        return "相机标定未加载，视觉定位不可用，请联系维护人员检查相机标定配置"
    if "failed to start D435" in text or "D435 is not started" in text:
        return "深度相机未启动，请检查相机 USB 连接，必要时重启机械臂服务"
    if "D435 did not provide synchronized depth and color frames" in text:
        return "深度相机没有输出同步的彩色/深度图像，请检查相机连接后重试"
    if "gripper feedback has not refreshed" in text or "gripper joint" in text:
        return "夹爪反馈异常，请检查夹爪接线并重新使能机械臂"
    if "arm transport command failed" in text:
        return "机械臂总线通信失败，请检查 CAN 接口与接线"
    if "thermal" in text or "温度" in text:
        return "机械臂温度保护触发，请等待各轴冷却后再操作"
    if "not connected" in text:
        return "设备未连接，请检查相关硬件的电源与接线后重试"

    # Already-Chinese details (e.g. 夹取失败 variants) pass through verbatim;
    # anything else stays as-is so engineers keep the original diagnostic.
    return text


def describe_navigation_failure(reason: str) -> str:
    """Translate one raw vehicle navigation / fine-tune reason into Chinese."""
    text = reason.strip()

    if "not the active vehicle map" in text or "not the active map" in text:
        return "目标地图不是车端当前的活动地图，请先在地图页把机器人切换到该地图后重试"
    if "base wheel feedback is not fresh" in text:
        return "底盘轮速反馈数据超时，导航被车端拒绝：请检查底盘连接与驱动状态后重试"
    if re.search(r"(?i)fine.?tun", text) and re.search(r"(?i)time.?out|timed out", text):
        return "低速精调超时：底盘在限定时间内未能精确对位，请检查定位精度后重试"
    if re.search(r"(?i)nav2", text) and re.search(r"(?i)reject", text):
        return "Nav2 拒绝了导航目标：目标点可能在障碍物内或地图范围外，请调整目标点后重试"
    if re.search(
        r"(?i)unreachable|no valid path|failed to (find|compute).*path|path.*blocked", text
    ):
        return "目标不可达：导航规划找不到可行路径，请确认目标点附近可以通行后重试"
    if re.search(r"(?i)locali[sz]ation", text) and re.search(
        r"(?i)unstable|lost|stale|not fresh", text
    ):
        return "定位不稳定：车端定位质量不足，无法可靠导航，请重新确认机器人位置后重试"
    if re.search(r"(?i)time.?out|timed out", text):
        return "执行超时：车端在限定时间内未完成，请检查机器人状态后重试"
    # Already-Chinese or unknown reasons pass through verbatim so engineers
    # keep the original diagnostic.
    return text


def describe_map_operation_failure(reason: str) -> str:
    """Translate raw map-switch / mapping-agent rejection reasons into Chinese.

    The vehicle gateway and the on-robot mapping agent report map-related
    rejections as English strings; anything unknown falls back to the generic
    navigation translator and finally to the verbatim reason.
    """
    text = reason.strip()

    if "is not enabled on this robot" in text:
        return "该地图未注册到车端，请先在车端注册该地图后重试"
    if "is not the active vehicle map" in text:
        return "该地图不是车端当前活动地图"
    if "not the active map" in text:
        return "目标地图不是车端当前的活动地图，请先在地图页把机器人切换到该地图后重试"
    return describe_navigation_failure(text)


def describe_bookarm_failure(state: str, detail: str) -> str:
    """Full operator-facing failure line for one bookarm sub-task."""
    friendly = describe_bookarm_detail(detail)
    state_label = _BOOKARM_STATE_LABELS.get(state, state)
    if friendly == detail.strip() and _has_cjk(friendly):
        # Already readable; only append the state when it adds information.
        if state == "recovery_required":
            return f"{friendly}（{_BOOKARM_STATE_LABELS[state]}）"
        return friendly
    return f"{friendly}（{state_label}）"
