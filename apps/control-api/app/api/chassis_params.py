"""Debug-center chassis parameters.

Exposes nav2 / chassis / static-TF tuning parameters as a read/write API and
writes them back with comment-preserving surgical text edits (``yaml.safe_load``
would drop every comment).  The "apply" endpoint restarts the on-robot runtime
so the new parameters take effect; that action is triggered manually by an
operator on the real machine.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..dependencies import get_current_user
from ..schemas import User

router = APIRouter(prefix="/api/v1/debug", tags=["debug"])

_ROBOT_ROOT = Path(os.environ.get("LR_ROBOT_ROOT", "/home/boreas/LR/Library_robot"))
NAV2_PARAMS_FILE = Path(
    os.environ.get(
        "LR_NAV2_PARAMS_FILE",
        _ROBOT_ROOT / "src" / "nav2_minimal_bringup" / "config" / "nav2_params.yaml",
    )
)
DDSM_PARAMS_FILE = Path(
    os.environ.get(
        "LR_DDSM_PARAMS_FILE",
        _ROBOT_ROOT / "src" / "car_ctrl" / "config" / "ddsm_hat_diff_drive.yaml",
    )
)
NAVIGATION_LAUNCH_FILE = Path(
    os.environ.get(
        "LR_NAVIGATION_LAUNCH_FILE",
        _ROBOT_ROOT / "src" / "nav2_minimal_bringup" / "launch" / "full_navigation.launch.py",
    )
)
FINE_TUNE_LAUNCH_FILE = Path(
    os.environ.get(
        "LR_FINE_TUNE_LAUNCH_FILE",
        _ROBOT_ROOT / "src" / "robot_decision" / "launch" / "book_shelf_fine_tune.launch.py",
    )
)

# Runtime launches from the colcon install tree, so a write that only lands in
# src/ never takes effect.  Each source file gets an install-side counterpart
# (the bringup package is renamed nav2_minimal_bringup -> mobile_robot_nav_bringup
# there, so the mapping is explicit).  Counterparts are written only when they
# already exist; tests patch these paths with temporary copies.
INSTALL_FILES: dict[str, Path] = {
    "nav2": _ROBOT_ROOT
    / "install"
    / "mobile_robot_nav_bringup"
    / "share"
    / "mobile_robot_nav_bringup"
    / "config"
    / "nav2_params.yaml",
    "ddsm": _ROBOT_ROOT
    / "install"
    / "car_ctrl"
    / "share"
    / "car_ctrl"
    / "config"
    / "ddsm_hat_diff_drive.yaml",
    "launch": _ROBOT_ROOT
    / "install"
    / "mobile_robot_nav_bringup"
    / "share"
    / "mobile_robot_nav_bringup"
    / "launch"
    / "full_navigation.launch.py",
    "finetune": _ROBOT_ROOT
    / "install"
    / "robot_decision"
    / "share"
    / "robot_decision"
    / "launch"
    / "book_shelf_fine_tune.launch.py",
}

APPLY_COMMAND = ["sudo", "-n", "systemctl", "restart", "lr-robot-runtime.service"]
APPLY_TIMEOUT_S = 60.0


# ---------------------------------------------------------------------------
# Comment-preserving YAML scalar editor
# ---------------------------------------------------------------------------


class YamlPathNotFoundError(KeyError):
    """A registry path does not exist in the target YAML document."""


_YAML_KEY_RE = re.compile(r"^(?P<indent>[ ]*)(?P<key>[A-Za-z0-9_.\-]+):(?P<rest>.*)$")


def _split_inline_comment(text: str) -> tuple[str, str]:
    """Split a YAML scalar tail from its inline comment.

    Only plain scalars (numbers / booleans / flow lists of numbers) are edited,
    so a simple ``#`` search is safe — no quoted strings can contain one.
    The returned scalar part keeps its trailing whitespace so the original gap
    before the comment survives a rewrite.
    """
    index = text.find("#")
    if index > 0 and text[index - 1] in " \t":
        return text[:index], text[index:]
    return text, ""


def _locate_key(lines: list[str], start: int, end: int, min_indent: int, key: str) -> int:
    """Find the line index of ``key:`` inside the block starting after ``start``.

    The block level is fixed by the first key line at or below ``min_indent``;
    the scan stops when indentation leaves that block.
    """
    block_indent: int | None = None
    for index in range(start, end):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _YAML_KEY_RE.match(line)
        if not match:
            continue
        indent = len(match.group("indent"))
        if block_indent is None:
            if indent < min_indent:
                break
            block_indent = indent
        if indent < block_indent:
            break
        if indent > block_indent:
            continue
        if match.group("key") == key:
            return index
    raise YamlPathNotFoundError(key)


def _descend(lines: list[str], start: int, end: int, min_indent: int, path: tuple[Any, ...]) -> int:
    index = _locate_key(lines, start, end, min_indent, str(path[0]))
    if len(path) == 1 or isinstance(path[1], int):
        return index
    match = _YAML_KEY_RE.match(lines[index])
    indent = len(match.group("indent")) if match else 0
    return _descend(lines, index + 1, end, indent + 1, path[1:])


def format_yaml_scalar(value: bool | int | float | str, value_type: str) -> str:
    if value_type == "bool":
        return "true" if value else "false"
    if value_type == "int":
        return str(int(value))
    if value_type == "string":
        # Quoted YAML string (e.g. footprint). Generated values never contain
        # quotes or backslashes, so plain wrapping is safe.
        return f'"{value}"'
    return repr(float(value))


def _replace_flow_list_element(scalar_part: str, index: int, formatted: str) -> str:
    open_index = scalar_part.find("[")
    close_index = scalar_part.find("]", open_index)
    if open_index == -1 or close_index == -1:
        raise YamlPathNotFoundError(f"flow list element [{index}]")
    inner = scalar_part[open_index + 1 : close_index]
    elements = inner.split(",")
    if index >= len(elements):
        raise YamlPathNotFoundError(f"flow list element [{index}]")
    original = elements[index]
    leading = original[: len(original) - len(original.lstrip())]
    trailing = original[len(original.rstrip()) :]
    elements[index] = leading + formatted + trailing
    return scalar_part[: open_index + 1] + ",".join(elements) + scalar_part[close_index:]


def set_yaml_value(
    text: str,
    path: tuple[Any, ...],
    value: bool | int | float | str,
    value_type: str,
) -> str:
    """Return ``text`` with the scalar at ``path`` replaced, comments intact.

    ``path`` elements are mapping keys; a trailing ``int`` addresses one element
    of an inline flow list such as ``max_velocity: [0.3, 0.0, 0.8]``.
    ``value_type`` "string" writes a quoted YAML string (used for footprint).
    """
    lines = text.splitlines(keepends=True)
    index = _descend(lines, 0, len(lines), 0, path)
    line = lines[index]
    newline = "\n" if line.endswith("\n") else ""
    body = line[: -len(newline)] if newline else line
    match = _YAML_KEY_RE.match(body)
    if not match:
        raise YamlPathNotFoundError(str(path))
    scalar_part, comment = _split_inline_comment(match.group("rest"))
    formatted = format_yaml_scalar(value, value_type)
    if isinstance(path[-1], int):
        new_scalar = _replace_flow_list_element(scalar_part, path[-1], formatted)
        rest = new_scalar + comment
    else:
        gap = scalar_part[len(scalar_part.rstrip()) :]
        rest = " " + formatted + (gap + comment if comment else "")
    lines[index] = body[: match.start("rest")] + rest + newline
    return "".join(lines)


def get_yaml_value(text: str, path: tuple[Any, ...]) -> Any:
    node: Any = yaml.safe_load(text)
    for part in path:
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and isinstance(part, int) and 0 <= part < len(node):
            node = node[part]
        else:
            raise YamlPathNotFoundError(str(path))
    return node


# ---------------------------------------------------------------------------
# Launch-file static-TF editor
# ---------------------------------------------------------------------------


class LaunchTfNotFoundError(KeyError):
    """The requested static TF node or flag does not exist in the launch file."""


_TF_VALUE_RE = re.compile(r'^(?P<indent>\s*)"(?P<value>[^"]*)"(?P<comma>,?)\s*$')


def _locate_tf_flag(lines: list[str], node_name: str, flag: str) -> int:
    """Find the value line following ``--flag`` inside one TF node block."""
    name_token = f'name="{node_name}"'
    start = None
    for index, line in enumerate(lines):
        if name_token in line:
            start = index
            break
    if start is None:
        raise LaunchTfNotFoundError(node_name)
    flag_token = f'"--{flag}"'
    for index in range(start + 1, len(lines)):
        line = lines[index]
        # Leaving the node block: another node's name or the next assignment.
        if ('name="' in line and name_token not in line) or "= Node(" in line:
            break
        if line.strip().rstrip(",") == flag_token:
            for value_index in range(index + 1, len(lines)):
                if lines[value_index].strip():
                    return value_index
            break
    raise LaunchTfNotFoundError(f"{node_name} --{flag}")


def get_launch_tf_value(text: str, node_name: str, flag: str) -> float:
    lines = text.splitlines(keepends=True)
    index = _locate_tf_flag(lines, node_name, flag)
    match = _TF_VALUE_RE.match(lines[index].rstrip("\n"))
    if not match:
        raise LaunchTfNotFoundError(f"{node_name} --{flag} value")
    try:
        return float(match.group("value"))
    except ValueError as error:
        raise LaunchTfNotFoundError(f"{node_name} --{flag} value") from error


def set_launch_tf_value(text: str, node_name: str, flag: str, value: float) -> str:
    """Replace the string literal after ``--flag`` in one static TF node block."""
    lines = text.splitlines(keepends=True)
    index = _locate_tf_flag(lines, node_name, flag)
    line = lines[index]
    newline = "\n" if line.endswith("\n") else ""
    match = _TF_VALUE_RE.match(line[: -len(newline)] if newline else line)
    if not match:
        raise LaunchTfNotFoundError(f"{node_name} --{flag} value")
    try:
        float(match.group("value"))
    except ValueError as error:
        raise LaunchTfNotFoundError(f"{node_name} --{flag} value") from error
    formatted = repr(float(value))
    lines[index] = f'{match.group("indent")}"{formatted}"{match.group("comma")}{newline}'
    return "".join(lines)


# ---------------------------------------------------------------------------
# Python-dict launch parameter editor ("key": value, lines)
# ---------------------------------------------------------------------------


class PyDictKeyNotFoundError(KeyError):
    """The requested key does not exist in the Python dict parameter block."""


_PY_DICT_LINE_RE = re.compile(
    r'^(?P<indent>\s*)"(?P<key>[^"]+)":\s*(?P<value>[^,#]+?)(?P<comma>,)(?P<tail>.*)$'
)


def _locate_py_dict_line(lines: list[str], key: str) -> int:
    for index, line in enumerate(lines):
        match = _PY_DICT_LINE_RE.match(line.rstrip("\n"))
        if match and match.group("key") == key:
            return index
    raise PyDictKeyNotFoundError(key)


def get_py_dict_value(text: str, key: str) -> Any:
    """Parse the literal value of a ``"key": value,`` line (numbers/bools)."""
    lines = text.splitlines(keepends=True)
    index = _locate_py_dict_line(lines, key)
    token = _PY_DICT_LINE_RE.match(lines[index].rstrip("\n")).group("value")
    try:
        return ast.literal_eval(token)
    except (ValueError, SyntaxError) as error:
        raise PyDictKeyNotFoundError(f"{key} value") from error


def set_py_dict_value(text: str, key: str, value: bool | int | float, value_type: str) -> str:
    """Replace the literal on a ``"key": value,`` line, keeping comma/comment."""
    lines = text.splitlines(keepends=True)
    index = _locate_py_dict_line(lines, key)
    line = lines[index]
    newline = "\n" if line.endswith("\n") else ""
    match = _PY_DICT_LINE_RE.match(line[: -len(newline)] if newline else line)
    if not match:
        raise PyDictKeyNotFoundError(key)
    # Sanity: the existing token must stay a numeric/bool literal.
    get_py_dict_value(text, key)
    if value_type == "bool":
        formatted = "True" if value else "False"
    elif value_type == "int":
        formatted = str(int(value))
    else:
        formatted = repr(float(value))
    lines[index] = (
        f'{match.group("indent")}"{match.group("key")}": {formatted}'
        f'{match.group("comma")}{match.group("tail")}{newline}'
    )
    return "".join(lines)


# ---------------------------------------------------------------------------
# Costmap footprint (vehicle body size) helpers
# ---------------------------------------------------------------------------

FOOTPRINT_PATHS: tuple[tuple[Any, ...], ...] = (
    ("global_costmap", "global_costmap", "ros__parameters", "footprint"),
    ("local_costmap", "local_costmap", "ros__parameters", "footprint"),
)


def parse_footprint_size(raw: str) -> tuple[float, float]:
    """Return (length, width) in metres from a footprint string; raises ValueError."""
    points = ast.literal_eval(raw)
    if not isinstance(points, list) or len(points) != 4:
        raise ValueError("footprint 需要 4 个角点")
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return max(xs) - min(xs), max(ys) - min(ys)


def format_footprint(length: float, width: float) -> str:
    """Render a centred rectangle footprint string, coordinates rounded to 4 decimals."""
    half_x = round(length / 2, 4)
    half_y = round(width / 2, 4)
    return (
        f"[[{half_x}, {half_y}], [{half_x}, {-half_y}], "
        f"[{-half_x}, {-half_y}], [{-half_x}, {half_y}]]"
    )


def read_footprint_size(nav2_text: str) -> tuple[float, float] | None:
    """Consistent (length, width) across both costmaps, else None.

    Returns None when a footprint line is unparsable or the global and local
    levels disagree; GET then reports the body size values as empty.
    """
    sizes: list[tuple[float, float]] = []
    for path in FOOTPRINT_PATHS:
        raw = get_yaml_value(nav2_text, path)  # may raise YamlPathNotFoundError
        if not isinstance(raw, str):
            return None
        try:
            sizes.append(parse_footprint_size(raw))
        except ValueError:
            return None
    rounded = {(round(length, 4), round(width, 4)) for length, width in sizes}
    if len(rounded) != 1:
        return None
    return sizes[0]


# ---------------------------------------------------------------------------
# Parameter registry
# ---------------------------------------------------------------------------

ParamType = Literal["float", "int", "bool"]

# group labels
GROUP_CHASSIS = "车辆与轮系"
GROUP_TF = "雷达与坐标变换"
GROUP_VELOCITY = "导航速度链"
GROUP_GOAL = "到位判定"
GROUP_SMOOTHER = "速度平滑"
GROUP_COLLISION = "碰撞监控"
GROUP_AMCL = "AMCL"
GROUP_COSTMAP = "代价地图"
GROUP_BODY = "车体尺寸"
GROUP_FINETUNE = "到位精调"


@dataclass(frozen=True)
class ParamDef:
    key: str
    group: str
    label: str
    unit: str
    type: ParamType
    min: float
    max: float
    step: float
    source: str  # "nav2" | "ddsm" | "launch" | "finetune"
    # YAML params: one or more key paths that must stay consistent.
    yaml_paths: tuple[tuple[Any, ...], ...] = ()
    # Launch TF params: (node name, flag without "--").
    tf_target: tuple[str, str] | None = None
    # Python-dict launch params: the quoted dict key.
    py_key: str | None = None
    # Specially handled params: "footprint_length" | "footprint_width".
    special: str | None = None


def _ddsm(section: str, key: str) -> tuple[Any, ...]:
    return (section, "ros__parameters", key)


def _nav2(*keys: Any) -> tuple[Any, ...]:
    return keys


_DDSM = "ddsm"
_NAV2 = "nav2"
_LAUNCH = "launch"
_FINETUNE = "finetune"
_FRONT_TF_NODE = "base_to_front_laser_tf"
_BACK_TF_NODE = "base_to_back_laser_tf"


def _finetune_params() -> list[ParamDef]:
    specs = [
        # (key, label, unit, min, max, step)
        ("x_tolerance", "X 到位容差", "m", 0.005, 0.2, 0.005),
        ("y_tolerance", "Y 到位容差", "m", 0.005, 0.2, 0.005),
        ("yaw_tolerance", "角度到位容差", "rad", 0.005, 0.2, 0.005),
        ("max_linear_speed", "最大线速度", "m/s", 0.01, 0.5, 0.01),
        ("max_angular_speed", "最大角速度", "rad/s", 0.01, 0.5, 0.01),
        ("min_linear_speed", "最小线速度", "m/s", 0.01, 0.5, 0.01),
        ("min_angular_speed", "最小角速度", "rad/s", 0.01, 0.5, 0.01),
        ("k_x", "X 方向 P 增益", "", 0.1, 3.0, 0.05),
        ("k_y", "Y 方向 P 增益", "", 0.1, 3.0, 0.05),
        ("k_yaw", "角度 P 增益", "", 0.1, 3.0, 0.05),
        ("stage_timeout_sec", "单阶段超时", "s", 5.0, 300.0, 1.0),
        ("timeout_sec", "总超时", "s", 5.0, 300.0, 1.0),
        ("max_initial_translation_error_m", "准入平移误差上限", "m", 0.1, 1.0, 0.01),
        ("max_initial_yaw_error_rad", "准入角度误差上限", "rad", 0.05, 1.57, 0.01),
        ("error_ema_alpha", "误差平滑系数 α", "", 0.05, 1.0, 0.05),
    ]
    return [
        ParamDef(
            key=f"finetune.{key}",
            group=GROUP_FINETUNE,
            label=label,
            unit=unit,
            type="float",
            min=minimum,
            max=maximum,
            step=step,
            source=_FINETUNE,
            py_key=key,
        )
        for key, label, unit, minimum, maximum, step in specs
    ]


def _tf_params() -> list[ParamDef]:
    specs = [
        ("x", "{side}雷达 X", "m", -1.0, 1.0, 0.005),
        ("y", "{side}雷达 Y", "m", -1.0, 1.0, 0.005),
        ("z", "{side}雷达 Z", "m", 0.0, 1.0, 0.01),
        ("yaw", "{side}雷达偏航角", "rad", -3.1416, 3.1416, 0.01),
    ]
    params: list[ParamDef] = []
    for side, node in (("front", _FRONT_TF_NODE), ("back", _BACK_TF_NODE)):
        side_label = "前" if side == "front" else "后"
        for flag, label, unit, minimum, maximum, step in specs:
            params.append(
                ParamDef(
                    key=f"tf.{side}.{flag}",
                    group=GROUP_TF,
                    label=label.format(side=side_label),
                    unit=unit,
                    type="float",
                    min=minimum,
                    max=maximum,
                    step=step,
                    source=_LAUNCH,
                    tf_target=(node, flag),
                )
            )
    return params


PARAMS: tuple[ParamDef, ...] = (
    # 车辆与轮系（ddsm_hat_diff_drive.yaml；轮径/轮距需同步两个节点段）
    ParamDef(
        key="chassis.wheel_radius",
        group=GROUP_CHASSIS,
        label="车轮半径",
        unit="m",
        type="float",
        min=0.01,
        max=0.2,
        step=0.0005,
        source=_DDSM,
        yaml_paths=(_ddsm("ddsm_hat_diff_drive", "wheel_radius"), _ddsm("car_odometry", "wheel_radius")),
    ),
    ParamDef(
        key="chassis.track_width",
        group=GROUP_CHASSIS,
        label="轮距",
        unit="m",
        type="float",
        min=0.2,
        max=1.0,
        step=0.005,
        source=_DDSM,
        yaml_paths=(_ddsm("ddsm_hat_diff_drive", "wheel_track"), _ddsm("car_odometry", "track_width")),
    ),
    ParamDef(
        key="chassis.max_rpm",
        group=GROUP_CHASSIS,
        label="电机最大转速",
        unit="rpm",
        type="int",
        min=10,
        max=600,
        step=10,
        source=_DDSM,
        yaml_paths=(_ddsm("ddsm_hat_diff_drive", "max_rpm"),),
    ),
    ParamDef(
        key="chassis.accel_rpm_per_sec",
        group=GROUP_CHASSIS,
        label="电机加速度",
        unit="rpm/s",
        type="float",
        min=10.0,
        max=2000.0,
        step=10.0,
        source=_DDSM,
        yaml_paths=(_ddsm("ddsm_hat_diff_drive", "accel_rpm_per_sec"),),
    ),
    *_tf_params(),
    # 导航速度链（controller_server FollowPath / DWB）
    ParamDef(
        key="nav.max_vel_x",
        group=GROUP_VELOCITY,
        label="最大线速度",
        unit="m/s",
        type="float",
        min=0.05,
        max=1.0,
        step=0.01,
        source=_NAV2,
        yaml_paths=(_nav2("controller_server", "ros__parameters", "FollowPath", "max_vel_x"),),
    ),
    ParamDef(
        key="nav.min_vel_x",
        group=GROUP_VELOCITY,
        label="最小线速度（倒车）",
        unit="m/s",
        type="float",
        min=-0.5,
        max=0.0,
        step=0.01,
        source=_NAV2,
        yaml_paths=(_nav2("controller_server", "ros__parameters", "FollowPath", "min_vel_x"),),
    ),
    ParamDef(
        key="nav.max_vel_theta",
        group=GROUP_VELOCITY,
        label="最大角速度",
        unit="rad/s",
        type="float",
        min=0.1,
        max=2.0,
        step=0.05,
        source=_NAV2,
        yaml_paths=(_nav2("controller_server", "ros__parameters", "FollowPath", "max_vel_theta"),),
    ),
    ParamDef(
        key="nav.acc_lim_x",
        group=GROUP_VELOCITY,
        label="线加速度",
        unit="m/s²",
        type="float",
        min=0.05,
        max=2.0,
        step=0.05,
        source=_NAV2,
        yaml_paths=(_nav2("controller_server", "ros__parameters", "FollowPath", "acc_lim_x"),),
    ),
    ParamDef(
        key="nav.decel_lim_x",
        group=GROUP_VELOCITY,
        label="线减速度",
        unit="m/s²",
        type="float",
        min=-2.0,
        max=-0.05,
        step=0.05,
        source=_NAV2,
        yaml_paths=(_nav2("controller_server", "ros__parameters", "FollowPath", "decel_lim_x"),),
    ),
    ParamDef(
        key="nav.acc_lim_theta",
        group=GROUP_VELOCITY,
        label="角加速度",
        unit="rad/s²",
        type="float",
        min=0.1,
        max=4.0,
        step=0.05,
        source=_NAV2,
        yaml_paths=(_nav2("controller_server", "ros__parameters", "FollowPath", "acc_lim_theta"),),
    ),
    ParamDef(
        key="nav.decel_lim_theta",
        group=GROUP_VELOCITY,
        label="角减速度",
        unit="rad/s²",
        type="float",
        min=-4.0,
        max=-0.1,
        step=0.05,
        source=_NAV2,
        yaml_paths=(_nav2("controller_server", "ros__parameters", "FollowPath", "decel_lim_theta"),),
    ),
    # 到位判定（general_goal_checker）
    ParamDef(
        key="goal.xy_goal_tolerance",
        group=GROUP_GOAL,
        label="到位距离容差",
        unit="m",
        type="float",
        min=0.01,
        max=0.5,
        step=0.01,
        source=_NAV2,
        yaml_paths=(_nav2("controller_server", "ros__parameters", "general_goal_checker", "xy_goal_tolerance"),),
    ),
    ParamDef(
        key="goal.yaw_goal_tolerance",
        group=GROUP_GOAL,
        label="到位角度容差",
        unit="rad",
        type="float",
        min=0.05,
        # 上限放到 ~2π：到位姿态由书架精调保证，导航栈只做粗定位，允许任意角度到位。
        max=6.28,
        step=0.05,
        source=_NAV2,
        yaml_paths=(_nav2("controller_server", "ros__parameters", "general_goal_checker", "yaw_goal_tolerance"),),
    ),
    ParamDef(
        key="goal.trans_stopped_velocity",
        group=GROUP_GOAL,
        label="停止判定线速度",
        unit="m/s",
        type="float",
        min=0.01,
        max=0.3,
        step=0.01,
        source=_NAV2,
        yaml_paths=(_nav2("controller_server", "ros__parameters", "general_goal_checker", "trans_stopped_velocity"),),
    ),
    # 速度平滑（velocity_smoother 流量表 [x, y, theta]）
    ParamDef(
        key="smoother.max_velocity.x",
        group=GROUP_SMOOTHER,
        label="平滑最大线速度",
        unit="m/s",
        type="float",
        min=0.05,
        max=1.0,
        step=0.01,
        source=_NAV2,
        yaml_paths=(_nav2("velocity_smoother", "ros__parameters", "max_velocity", 0),),
    ),
    ParamDef(
        key="smoother.max_velocity.theta",
        group=GROUP_SMOOTHER,
        label="平滑最大角速度",
        unit="rad/s",
        type="float",
        min=0.1,
        max=2.0,
        step=0.05,
        source=_NAV2,
        yaml_paths=(_nav2("velocity_smoother", "ros__parameters", "max_velocity", 2),),
    ),
    ParamDef(
        key="smoother.max_accel.x",
        group=GROUP_SMOOTHER,
        label="平滑最大线加速度",
        unit="m/s²",
        type="float",
        min=0.05,
        max=2.0,
        step=0.05,
        source=_NAV2,
        yaml_paths=(_nav2("velocity_smoother", "ros__parameters", "max_accel", 0),),
    ),
    ParamDef(
        key="smoother.max_accel.theta",
        group=GROUP_SMOOTHER,
        label="平滑最大角加速度",
        unit="rad/s²",
        type="float",
        min=0.1,
        max=4.0,
        step=0.05,
        source=_NAV2,
        yaml_paths=(_nav2("velocity_smoother", "ros__parameters", "max_accel", 2),),
    ),
    ParamDef(
        key="smoother.max_decel.x",
        group=GROUP_SMOOTHER,
        label="平滑最大线减速度",
        unit="m/s²",
        type="float",
        min=-2.0,
        max=-0.05,
        step=0.05,
        source=_NAV2,
        yaml_paths=(_nav2("velocity_smoother", "ros__parameters", "max_decel", 0),),
    ),
    ParamDef(
        key="smoother.max_decel.theta",
        group=GROUP_SMOOTHER,
        label="平滑最大角减速度",
        unit="rad/s²",
        type="float",
        min=-4.0,
        max=-0.1,
        step=0.05,
        source=_NAV2,
        yaml_paths=(_nav2("velocity_smoother", "ros__parameters", "max_decel", 2),),
    ),
    # 碰撞监控（collision_monitor）
    ParamDef(
        key="collision.time_before_collision",
        group=GROUP_COLLISION,
        label="碰撞前时间阈值",
        unit="s",
        type="float",
        min=0.5,
        max=5.0,
        step=0.1,
        source=_NAV2,
        yaml_paths=(_nav2("collision_monitor", "ros__parameters", "FootprintApproach", "time_before_collision"),),
    ),
    ParamDef(
        key="collision.min_points",
        group=GROUP_COLLISION,
        label="触发最小点数",
        unit="个",
        type="int",
        min=1,
        max=20,
        step=1,
        source=_NAV2,
        yaml_paths=(_nav2("collision_monitor", "ros__parameters", "FootprintApproach", "min_points"),),
    ),
    ParamDef(
        key="collision.source_timeout",
        group=GROUP_COLLISION,
        label="数据源超时",
        unit="s",
        type="float",
        min=0.2,
        max=5.0,
        step=0.1,
        source=_NAV2,
        yaml_paths=(_nav2("collision_monitor", "ros__parameters", "source_timeout"),),
    ),
    ParamDef(
        key="collision.stop_pub_timeout",
        group=GROUP_COLLISION,
        label="停止发布超时",
        unit="s",
        type="float",
        min=0.5,
        max=10.0,
        step=0.1,
        source=_NAV2,
        yaml_paths=(_nav2("collision_monitor", "ros__parameters", "stop_pub_timeout"),),
    ),
    # AMCL
    ParamDef(
        key="amcl.min_particles",
        group=GROUP_AMCL,
        label="最小粒子数",
        unit="个",
        type="int",
        min=100,
        max=5000,
        step=50,
        source=_NAV2,
        yaml_paths=(_nav2("amcl", "ros__parameters", "min_particles"),),
    ),
    ParamDef(
        key="amcl.max_particles",
        group=GROUP_AMCL,
        label="最大粒子数",
        unit="个",
        type="int",
        min=100,
        max=5000,
        step=50,
        source=_NAV2,
        yaml_paths=(_nav2("amcl", "ros__parameters", "max_particles"),),
    ),
    ParamDef(
        key="amcl.sigma_hit",
        group=GROUP_AMCL,
        label="激光模型 σ",
        unit="m",
        type="float",
        min=0.05,
        max=1.0,
        step=0.01,
        source=_NAV2,
        yaml_paths=(_nav2("amcl", "ros__parameters", "sigma_hit"),),
    ),
    ParamDef(
        key="amcl.z_hit",
        group=GROUP_AMCL,
        label="命中权重 z_hit",
        unit="",
        type="float",
        min=0.0,
        max=1.0,
        step=0.01,
        source=_NAV2,
        yaml_paths=(_nav2("amcl", "ros__parameters", "z_hit"),),
    ),
    ParamDef(
        key="amcl.z_rand",
        group=GROUP_AMCL,
        label="随机权重 z_rand",
        unit="",
        type="float",
        min=0.0,
        max=1.0,
        step=0.01,
        source=_NAV2,
        yaml_paths=(_nav2("amcl", "ros__parameters", "z_rand"),),
    ),
    ParamDef(
        key="amcl.update_min_d",
        group=GROUP_AMCL,
        label="更新平移阈值",
        unit="m",
        type="float",
        min=0.01,
        max=0.5,
        step=0.01,
        source=_NAV2,
        yaml_paths=(_nav2("amcl", "ros__parameters", "update_min_d"),),
    ),
    ParamDef(
        key="amcl.update_min_a",
        group=GROUP_AMCL,
        label="更新旋转阈值",
        unit="rad",
        type="float",
        min=0.01,
        max=0.5,
        step=0.01,
        source=_NAV2,
        yaml_paths=(_nav2("amcl", "ros__parameters", "update_min_a"),),
    ),
    ParamDef(
        key="amcl.resample_interval",
        group=GROUP_AMCL,
        label="重采样间隔",
        unit="次",
        type="int",
        min=1,
        max=10,
        step=1,
        source=_NAV2,
        yaml_paths=(_nav2("amcl", "ros__parameters", "resample_interval"),),
    ),
    ParamDef(
        key="amcl.max_beams",
        group=GROUP_AMCL,
        label="最大光束数",
        unit="束",
        type="int",
        min=10,
        max=500,
        step=10,
        source=_NAV2,
        yaml_paths=(_nav2("amcl", "ros__parameters", "max_beams"),),
    ),
    # 代价地图（本配置使用 footprint 多边形，无 robot_radius 键，故不接入）
    ParamDef(
        key="costmap.local_inflation_radius",
        group=GROUP_COSTMAP,
        label="局部膨胀半径",
        unit="m",
        type="float",
        min=0.1,
        max=2.0,
        step=0.05,
        source=_NAV2,
        yaml_paths=(
            _nav2("local_costmap", "local_costmap", "ros__parameters", "inflation_layer", "inflation_radius"),
        ),
    ),
    ParamDef(
        key="costmap.global_inflation_radius",
        group=GROUP_COSTMAP,
        label="全局膨胀半径",
        unit="m",
        type="float",
        min=0.1,
        max=2.0,
        step=0.05,
        source=_NAV2,
        yaml_paths=(
            _nav2("global_costmap", "global_costmap", "ros__parameters", "inflation_layer", "inflation_radius"),
        ),
    ),
    ParamDef(
        key="costmap.local_resolution",
        group=GROUP_COSTMAP,
        label="局部地图分辨率",
        unit="m/格",
        type="float",
        min=0.01,
        max=0.2,
        step=0.01,
        source=_NAV2,
        yaml_paths=(_nav2("local_costmap", "local_costmap", "ros__parameters", "resolution"),),
    ),
    ParamDef(
        key="costmap.global_resolution",
        group=GROUP_COSTMAP,
        label="全局地图分辨率",
        unit="m/格",
        type="float",
        min=0.01,
        max=0.2,
        step=0.01,
        source=_NAV2,
        yaml_paths=(_nav2("global_costmap", "global_costmap", "ros__parameters", "resolution"),),
    ),
    # 车体尺寸（两级 costmap 的 footprint / footprint_padding）
    ParamDef(
        key="body.length_m",
        group=GROUP_BODY,
        label="车长",
        unit="m",
        type="float",
        min=0.2,
        max=1.2,
        step=0.01,
        source=_NAV2,
        special="footprint_length",
    ),
    ParamDef(
        key="body.width_m",
        group=GROUP_BODY,
        label="车宽",
        unit="m",
        type="float",
        min=0.2,
        max=1.2,
        step=0.01,
        source=_NAV2,
        special="footprint_width",
    ),
    ParamDef(
        key="body.footprint_padding_global",
        group=GROUP_BODY,
        label="全局 footprint 内边距",
        unit="m",
        type="float",
        min=0.0,
        max=0.2,
        step=0.005,
        source=_NAV2,
        yaml_paths=(_nav2("global_costmap", "global_costmap", "ros__parameters", "footprint_padding"),),
    ),
    ParamDef(
        key="body.footprint_padding_local",
        group=GROUP_BODY,
        label="局部 footprint 内边距",
        unit="m",
        type="float",
        min=0.0,
        max=0.2,
        step=0.005,
        source=_NAV2,
        yaml_paths=(_nav2("local_costmap", "local_costmap", "ros__parameters", "footprint_padding"),),
    ),
    # 到位精调（book_shelf_fine_tune.launch.py 的 Python dict 参数块）
    *_finetune_params(),
)

PARAMS_BY_KEY: dict[str, ParamDef] = {param.key: param for param in PARAMS}


# ---------------------------------------------------------------------------
# API schemas and handlers
# ---------------------------------------------------------------------------


class ChassisParamView(BaseModel):
    key: str
    group: str
    label: str
    unit: str
    type: ParamType
    min: float
    max: float
    step: float
    # None when the current value cannot be derived (e.g. inconsistent
    # global/local footprints); the page then shows an empty input.
    value: bool | int | float | None = None
    # False when the parameter is registered but absent from the current
    # on-robot files (vehicle runtime predates it); the page disables editing.
    available: bool = True
    unavailable_reason: str | None = None


class ChassisParamsView(BaseModel):
    params: list[ChassisParamView] = Field(default_factory=list)


class ChassisParamsUpdate(BaseModel):
    # Values are validated per parameter so type errors carry Chinese messages.
    values: dict[str, Any]


class ApplyResultView(BaseModel):
    ok: bool


def _source_file(source: str) -> Path:
    if source == _NAV2:
        return NAV2_PARAMS_FILE
    if source == _DDSM:
        return DDSM_PARAMS_FILE
    if source == _FINETUNE:
        return FINE_TUNE_LAUNCH_FILE
    return NAVIGATION_LAUNCH_FILE


def _write_targets(source: str) -> list[Path]:
    """All files a write must land in: the src file plus its install-tree copy
    when that copy exists (the runtime loads the install tree)."""
    targets = [_source_file(source)]
    install_copy = INSTALL_FILES.get(source)
    if install_copy is not None and install_copy != targets[0] and install_copy.exists():
        targets.append(install_copy)
    return targets


def _read_source_text(source: str) -> str:
    path = _source_file(source)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"无法读取配置文件 {path}：{error}") from error


def _read_param_value(param: ParamDef, texts: dict[str, str]) -> bool | int | float | None:
    if param.tf_target is not None:
        return get_launch_tf_value(texts[param.source], param.tf_target[0], param.tf_target[1])
    if param.py_key is not None:
        raw = get_py_dict_value(texts[param.source], param.py_key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise PyDictKeyNotFoundError(param.key)
        return int(raw) if param.type == "int" else float(raw)
    if param.special in ("footprint_length", "footprint_width"):
        size = read_footprint_size(texts[_NAV2])
        if size is None:
            # Inconsistent or unparsable footprints: report an empty value
            # instead of a misleading one.
            return None
        length, width = size
        return round(length, 4) if param.special == "footprint_length" else round(width, 4)
    raw = get_yaml_value(texts[param.source], param.yaml_paths[0])
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return int(raw) if param.type == "int" else float(raw)
    raise YamlPathNotFoundError(param.key)


UNAVAILABLE_REASON = "当前车端版本不支持"


def _load_params() -> ChassisParamsView:
    texts: dict[str, str] = {}
    view = ChassisParamsView()
    for param in PARAMS:
        if param.source not in texts:
            texts[param.source] = _read_source_text(param.source)
        available = True
        unavailable_reason: str | None = None
        try:
            value = _read_param_value(param, texts)
        except (YamlPathNotFoundError, LaunchTfNotFoundError, PyDictKeyNotFoundError):
            # Registered but absent from the current on-robot files (the
            # vehicle runtime predates it): report it as unavailable instead
            # of dropping it, so the page can disable the input with a reason.
            value = None
            available = False
            unavailable_reason = UNAVAILABLE_REASON
        view.params.append(
            ChassisParamView(
                key=param.key,
                group=param.group,
                label=param.label,
                unit=param.unit,
                type=param.type,
                min=param.min,
                max=param.max,
                step=param.step,
                value=value,
                available=available,
                unavailable_reason=unavailable_reason,
            )
        )
    return view


def _validate_update(param: ParamDef, raw: Any) -> bool | int | float:
    if param.type == "bool":
        if not isinstance(raw, bool):
            raise HTTPException(status_code=422, detail=f"参数「{param.label}」需要布尔值")
        return raw
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise HTTPException(status_code=422, detail=f"参数「{param.label}」需要数值")
    if param.type == "int":
        if isinstance(raw, float) and not raw.is_integer():
            raise HTTPException(status_code=422, detail=f"参数「{param.label}」需要整数")
        value: bool | int | float = int(raw)
    else:
        value = float(raw)
    if not param.min <= value <= param.max:
        raise HTTPException(
            status_code=422,
            detail=f"参数「{param.label}」超出允许范围 [{param.min}, {param.max}]（收到 {value}）",
        )
    return value


@router.get("/chassis-params", response_model=ChassisParamsView)
def get_chassis_params(user: User = Depends(get_current_user)) -> ChassisParamsView:
    return _load_params()


@router.put("/chassis-params", response_model=ChassisParamsView)
def update_chassis_params(update: ChassisParamsUpdate, user: User = Depends(get_current_user)) -> ChassisParamsView:
    edits: list[tuple[ParamDef, bool | int | float]] = []
    for key, raw in update.values.items():
        param = PARAMS_BY_KEY.get(key)
        if param is None:
            raise HTTPException(status_code=422, detail=f"未知参数：{key}")
        edits.append((param, _validate_update(param, raw)))

    # Group edits per file; each file is rewritten once after all edits apply.
    texts: dict[str, str] = {}
    footprint_dims: dict[str, float] = {}
    for param, value in edits:
        text = texts.get(param.source) or _read_source_text(param.source)
        try:
            if param.tf_target is not None:
                text = set_launch_tf_value(text, param.tf_target[0], param.tf_target[1], float(value))
            elif param.py_key is not None:
                text = set_py_dict_value(text, param.py_key, value, param.type)
            elif param.special in ("footprint_length", "footprint_width"):
                dimension = "length" if param.special == "footprint_length" else "width"
                footprint_dims[dimension] = float(value)
            else:
                for path in param.yaml_paths:
                    text = set_yaml_value(text, path, value, param.type)
        except (YamlPathNotFoundError, LaunchTfNotFoundError, PyDictKeyNotFoundError) as error:
            raise HTTPException(
                status_code=422,
                detail=f"参数「{param.label}」在当前配置文件中不存在，无法写入",
            ) from error
        texts[param.source] = text

    if footprint_dims:
        # A footprint rewrite always targets both costmap levels at once.
        text = texts.get(_NAV2) or _read_source_text(_NAV2)
        length = footprint_dims.get("length")
        width = footprint_dims.get("width")
        if length is None or width is None:
            current = read_footprint_size(text)
            if current is None:
                raise HTTPException(
                    status_code=422,
                    detail="当前两级 footprint 不一致或无法解析，请同时提交车长与车宽",
                )
            length = length if length is not None else current[0]
            width = width if width is not None else current[1]
        footprint = format_footprint(length, width)
        for path in FOOTPRINT_PATHS:
            text = set_yaml_value(text, path, footprint, "string")
        texts[_NAV2] = text

    for source, text in texts.items():
        for path in _write_targets(source):
            try:
                path.write_text(text, encoding="utf-8")
            except OSError as error:
                raise HTTPException(status_code=500, detail=f"无法写入配置文件 {path}：{error}") from error

    return _load_params()


def _restart_robot_runtime() -> subprocess.CompletedProcess[str]:
    return subprocess.run(APPLY_COMMAND, capture_output=True, text=True, timeout=APPLY_TIMEOUT_S)


@router.post("/chassis-params/apply", response_model=ApplyResultView)
def apply_chassis_params(user: User = Depends(get_current_user)) -> ApplyResultView:
    """Restart the robot navigation runtime so saved parameters take effect."""
    try:
        result = _restart_robot_runtime()
    except subprocess.TimeoutExpired as error:
        raise HTTPException(status_code=502, detail="重启导航运行时超时（60 秒）") from error
    except OSError as error:
        raise HTTPException(status_code=502, detail=f"无法执行重启命令：{error}") from error
    if result.returncode != 0:
        summary = (result.stderr or result.stdout or "").strip()[:300] or "无错误输出"
        raise HTTPException(
            status_code=502,
            detail=f"重启导航运行时失败（退出码 {result.returncode}）：{summary}",
        )
    return ApplyResultView(ok=True)
