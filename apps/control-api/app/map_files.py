from __future__ import annotations

import ast
import base64
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RosMapMetadata:
    image: str
    resolution: float
    origin: tuple[float, float, float]
    mode: str
    negate: bool
    occupied_threshold: float
    free_threshold: float


@dataclass(frozen=True)
class PgmImage:
    width: int
    height: int
    max_value: int
    pixels: bytes


@dataclass(frozen=True)
class CruisePoint:
    """A cruise destination expressed in the ROS ``map`` frame."""

    name: str
    x: float
    y: float
    yaw: float


def decode_base64(content: str, label: str) -> bytes:
    try:
        data = base64.b64decode(content, validate=True)
    except ValueError as error:
        raise ValueError(f"{label} 内容不是有效的 Base64 数据") from error
    if not data:
        raise ValueError(f"{label} 文件为空")
    if len(data) > 20 * 1024 * 1024:
        raise ValueError(f"{label} 文件不能超过 20 MB")
    return data


def parse_ros_map_yaml(content: bytes, pgm_name: str) -> RosMapMetadata:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("YAML 必须使用 UTF-8 编码") from error

    # ROS map YAML files appear in both flow and block styles; prefer a real
    # YAML parser and fall back to the legacy line parser when PyYAML is absent.
    values: dict[str, object] = {}
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml is not None:
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise ValueError("YAML 解析失败: %s" % error) from error
        if isinstance(document, dict):
            values = {str(key): value for key, value in document.items()}
    if not values:
        legacy: dict[str, str] = {}
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            legacy[key.strip()] = value.strip().strip("\"'")
        values = legacy

    required = {"image", "resolution", "origin"}
    missing = required - values.keys()
    if missing:
        raise ValueError(f"YAML 缺少字段：{', '.join(sorted(missing))}")
    image = str(values["image"])
    if Path(image).name != image or image != pgm_name:
        raise ValueError("YAML 的 image 必须与上传的 PGM 文件同名，且不能包含路径")
    if not image.lower().endswith(".pgm"):
        raise ValueError("YAML 的 image 必须引用 .pgm 文件")
    try:
        resolution = float(values["resolution"])
        origin_raw = values["origin"]
        if isinstance(origin_raw, str):
            origin_raw = ast.literal_eval(origin_raw)
        origin = tuple(float(value) for value in origin_raw)
        negate = bool(int(values.get("negate", 0)))
        occupied_threshold = float(values.get("occupied_thresh", 0.65))
        free_threshold = float(values.get("free_thresh", 0.196))
    except (SyntaxError, TypeError, ValueError) as error:
        raise ValueError("YAML 的 resolution、origin 或阈值格式不正确") from error
    if resolution <= 0 or len(origin) != 3:
        raise ValueError("YAML 的 resolution 必须大于 0，origin 必须包含 x、y、yaw 三个值")
    if not 0 <= free_threshold < occupied_threshold <= 1:
        raise ValueError("YAML 的 free_thresh 与 occupied_thresh 范围不正确")
    mode = values.get("mode", "trinary")
    if mode not in {"trinary", "scale", "raw"}:
        raise ValueError("YAML 的 mode 仅支持 trinary、scale 或 raw")
    return RosMapMetadata(
        image=image,
        resolution=resolution,
        origin=(origin[0], origin[1], origin[2]),
        mode=mode,
        negate=negate,
        occupied_threshold=occupied_threshold,
        free_threshold=free_threshold,
    )


def parse_cruise_points_yaml(content: bytes) -> list[CruisePoint]:
    """Read the limited ``points`` structure used by robot_decision/points.yaml.

    This deliberately avoids adding a general YAML dependency to the API. Only direct
    point entries and their map-frame x/y/yaw fields are accepted.
    """
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("巡航点配置必须使用 UTF-8 编码") from error

    points_indent: int | None = None
    current_name: str | None = None
    current_indent: int | None = None
    raw_points: list[tuple[str, dict[str, str]]] = []
    fields: dict[str, str] = {}

    def finish_current() -> None:
        nonlocal current_name, current_indent, fields
        if current_name is not None:
            raw_points.append((current_name, fields))
        current_name = None
        current_indent = None
        fields = {}

    for raw_line in text.splitlines():
        uncommented = raw_line.split("#", 1)[0].rstrip()
        if not uncommented.strip():
            continue
        indent = len(uncommented) - len(uncommented.lstrip(" "))
        line = uncommented.strip()

        if points_indent is None:
            if line == "points:":
                points_indent = indent
            continue
        if indent <= points_indent:
            finish_current()
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if indent == points_indent + 2:
            finish_current()
            if not key or value:
                raise ValueError("巡航点配置中的 points 条目格式不正确")
            current_name = key
            current_indent = indent
        elif current_name is not None and indent > (current_indent or 0):
            fields[key] = value

    finish_current()
    if points_indent is None:
        raise ValueError("巡航点配置缺少 points 字段")

    points: list[CruisePoint] = []
    for name, values in raw_points:
        frame_id = values.get("frame_id", "map")
        if frame_id != "map":
            continue
        try:
            points.append(
                CruisePoint(
                    name=name,
                    x=float(values["x"]),
                    y=float(values["y"]),
                    yaw=float(values["yaw"]),
                )
            )
        except (KeyError, ValueError) as error:
            raise ValueError(f"巡航点 {name} 缺少或包含无效的 x、y、yaw") from error
    return points


def _read_token(data: bytes, position: int) -> tuple[bytes, int]:
    while position < len(data):
        if data[position] == ord("#"):
            newline = data.find(b"\n", position)
            position = len(data) if newline < 0 else newline + 1
        elif data[position] in b" \t\r\n":
            position += 1
        else:
            break
    start = position
    while position < len(data) and data[position] not in b" \t\r\n#":
        position += 1
    if start == position:
        raise ValueError("PGM 文件头不完整")
    return data[start:position], position


def parse_pgm(data: bytes) -> PgmImage:
    magic, position = _read_token(data, 0)
    if magic not in {b"P2", b"P5"}:
        raise ValueError("仅支持 P2 或 P5 格式的 PGM 地图")
    width_raw, position = _read_token(data, position)
    height_raw, position = _read_token(data, position)
    max_raw, position = _read_token(data, position)
    try:
        width, height, max_value = int(width_raw), int(height_raw), int(max_raw)
    except ValueError as error:
        raise ValueError("PGM 的尺寸或灰度范围无效") from error
    if width <= 0 or height <= 0 or width * height > 16_000_000 or not 0 < max_value <= 255:
        raise ValueError("PGM 图像尺寸或灰度范围超出允许值")

    if magic == b"P2":
        values: list[int] = []
        while position < len(data):
            try:
                token, position = _read_token(data, position)
            except ValueError:
                break
            try:
                values.append(int(token))
            except ValueError as error:
                raise ValueError("P2 PGM 包含无效的像素值") from error
        if len(values) != width * height or any(value < 0 or value > max_value for value in values):
            raise ValueError("P2 PGM 像素数据无效")
        pixels = bytes(round(value * 255 / max_value) for value in values)
        return PgmImage(width=width, height=height, max_value=255, pixels=pixels)

    # P5 uses one whitespace separator after the max value; do not consume image bytes.
    if position >= len(data) or data[position] not in b" \t\r\n":
        raise ValueError("P5 PGM 文件头缺少像素数据分隔符")
    if data[position:position + 2] == b"\r\n":
        position += 2
    else:
        position += 1
    pixels = data[position:]
    if len(pixels) != width * height:
        raise ValueError("P5 PGM 像素数量与图像尺寸不匹配")
    if max_value != 255:
        pixels = bytes(round(value * 255 / max_value) for value in pixels)
    return PgmImage(width=width, height=height, max_value=255, pixels=pixels)


def pgm_preview_png(image: PgmImage, metadata: RosMapMetadata) -> bytes:
    """Render ROS occupancy values into an ordinary PNG for browser display."""
    rows = bytearray()
    for row in range(image.height):
        rows.append(0)  # PNG filter: none
        for value in image.pixels[row * image.width : (row + 1) * image.width]:
            occupancy = value / 255 if metadata.negate else (255 - value) / 255
            if occupancy >= metadata.occupied_threshold:
                rows.extend((35, 43, 54))  # wall
            elif occupancy <= metadata.free_threshold:
                rows.extend((248, 250, 252))  # traversable floor
            else:
                rows.extend((203, 213, 225))  # unknown space

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", image.width, image.height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )
