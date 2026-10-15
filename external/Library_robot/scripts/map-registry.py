#!/usr/bin/env python3

"""Stage local ROS maps and manage the robot's trusted map registry."""

from __future__ import annotations

import argparse
import grp
import math
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

WORKSPACE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE / "src" / "robot_decision"))

from robot_decision.map_registry import (  # noqa: E402
    RegisteredMap,
    active_map_id,
    load_map_registry,
    registry_document,
)


MAP_ID_PATTERN = re.compile(r"^MAP-[A-Za-z0-9_-]{1,80}$")


def _read_pgm_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    position = 0

    def token() -> bytes:
        nonlocal position
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
            raise ValueError("PGM header is incomplete")
        return data[start:position]

    magic = token()
    if magic not in {b"P2", b"P5"}:
        raise ValueError("only P2/P5 PGM maps are supported")
    try:
        width, height, max_value = int(token()), int(token()), int(token())
    except ValueError as error:
        raise ValueError("PGM dimensions are invalid") from error
    if width <= 0 or height <= 0 or width * height > 16_000_000 or not 0 < max_value <= 255:
        raise ValueError("PGM dimensions are outside the permitted range")
    return width, height


def _read_source_map(path: Path) -> tuple[dict, Path, tuple[float, float, float], float, int, int]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("map YAML must be a regular file")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"cannot read map YAML: {error}") from error
    if not isinstance(document, dict):
        raise ValueError("map YAML must be an object")
    image_name = document.get("image")
    if not isinstance(image_name, str) or Path(image_name).name != image_name or not image_name.endswith(".pgm"):
        raise ValueError("map YAML image must be a sibling .pgm filename")
    image_path = path.parent / image_name
    if not image_path.is_file() or image_path.is_symlink():
        raise ValueError("map YAML image is unavailable")
    try:
        resolution = float(document["resolution"])
        origin_values = document["origin"]
        origin = tuple(float(value) for value in origin_values)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("map YAML resolution or origin is invalid") from error
    if len(origin) != 3 or not math.isfinite(resolution) or resolution <= 0 or not all(
        math.isfinite(value) for value in origin
    ):
        raise ValueError("map YAML resolution or origin is invalid")
    width, height = _read_pgm_size(image_path)
    return document, image_path, (origin[0], origin[1], origin[2]), resolution, width, height


def _atomic_write_registry(path: Path, maps: dict[str, RegisteredMap]) -> None:
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".map-registry.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            yaml.safe_dump(registry_document(maps), output, sort_keys=False, allow_unicode=False)
        runtime_group = grp.getgrnam("boreas").gr_gid
        os.chown(temporary_name, 0, runtime_group)
        os.chmod(temporary_name, 0o640)
        os.replace(temporary_name, path)
    except (KeyError, OSError):
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _set_vehicle_read_permissions(path: Path) -> None:
    """Keep root ownership while granting only the runtime group map access."""

    try:
        runtime_group = grp.getgrnam("boreas").gr_gid
        os.chown(path, 0, runtime_group)
        os.chmod(path, 0o750)
        for child in path.iterdir():
            os.chown(child, 0, runtime_group)
            os.chmod(child, 0o640)
    except (KeyError, OSError) as error:
        raise ValueError(f"cannot set secure map permissions: {error}") from error


def stage_map(arguments: argparse.Namespace) -> None:
    if not MAP_ID_PATTERN.fullmatch(arguments.map_id):
        raise ValueError("map ID must match MAP-xx")
    source_yaml = Path(arguments.map_yaml).resolve()
    document, source_pgm, origin, resolution, width, height = _read_source_map(source_yaml)
    storage_root = Path(arguments.storage_root)
    if not storage_root.is_absolute():
        raise ValueError("storage root must be absolute")
    target_dir = storage_root / arguments.map_id
    if target_dir.exists() and not arguments.replace:
        raise ValueError(f"map {arguments.map_id} is already staged; use --replace after review")
    storage_root.mkdir(mode=0o750, parents=True, exist_ok=True)
    # Harden only the storage root itself.  Applying
    # _set_vehicle_read_permissions here also chmods every existing map
    # directory to 0640, which locks the boreas runtime out of all previously
    # staged maps (observed on LR-01, 2026-09-04: navigation failed to start).
    try:
        os.chown(storage_root, 0, grp.getgrnam("boreas").gr_gid)
        os.chmod(storage_root, 0o750)
    except (KeyError, OSError) as error:
        raise ValueError(f"cannot set secure map permissions: {error}") from error
    temporary_dir = Path(tempfile.mkdtemp(prefix=f".{arguments.map_id}.", dir=storage_root))
    try:
        target_yaml = temporary_dir / "map.yaml"
        target_pgm = temporary_dir / "map.pgm"
        document["image"] = target_pgm.name
        target_yaml.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=False), encoding="utf-8")
        shutil.copyfile(source_pgm, target_pgm)
        if target_dir.exists():
            retired = storage_root / f".{arguments.map_id}.replaced"
            if retired.exists():
                shutil.rmtree(retired)
            os.replace(target_dir, retired)
            try:
                os.replace(temporary_dir, target_dir)
            except Exception:
                os.replace(retired, target_dir)
                raise
            shutil.rmtree(retired)
        else:
            os.replace(temporary_dir, target_dir)
        _set_vehicle_read_permissions(target_dir)
    except Exception:
        if temporary_dir.exists():
            shutil.rmtree(temporary_dir)
        raise

    registry_path = Path(arguments.registry)
    try:
        maps = load_map_registry(registry_path) if registry_path.exists() else {}
        maps[arguments.map_id] = RegisteredMap(
            map_id=arguments.map_id,
            yaml_path=target_dir / "map.yaml",
            min_x=origin[0],
            max_x=origin[0] + width * resolution,
            min_y=origin[1],
            max_y=origin[1] + height * resolution,
        )
        _atomic_write_registry(registry_path, maps)
    except Exception:
        # Do not leave an unregistered map that appears selectable on disk.
        if target_dir.exists():
            shutil.rmtree(target_dir)
        raise
    print(f"Staged {arguments.map_id}: {target_dir / 'map.yaml'}")


def show_active_map(arguments: argparse.Namespace) -> None:
    maps = load_map_registry(Path(arguments.registry))
    selected_id = active_map_id(Path(arguments.state), arguments.fallback_map_id)
    selected = maps.get(selected_id)
    if selected is None:
        raise ValueError(f"active map {selected_id} is not registered on this vehicle")
    print(selected.map_id)
    print(selected.yaml_path)


def validate_registry(arguments: argparse.Namespace) -> None:
    maps = load_map_registry(Path(arguments.registry))
    selected_id = active_map_id(Path(arguments.state), arguments.fallback_map_id)
    if selected_id not in maps:
        raise ValueError(f"active map {selected_id} is not registered on this vehicle")
    print(f"Registry valid: {len(maps)} map(s); active {selected_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("stage", help="copy and register one local ROS map")
    stage.add_argument("--map-id", required=True)
    stage.add_argument("--map-yaml", required=True)
    stage.add_argument("--registry", default="/etc/lr-robot/map-registry.yaml")
    stage.add_argument("--storage-root", default="/var/lib/lr-robot/maps")
    stage.add_argument("--replace", action="store_true")
    stage.set_defaults(handler=stage_map)
    for name, handler in (("active", show_active_map), ("validate", validate_registry)):
        command = commands.add_parser(name)
        command.add_argument("--registry", default="/etc/lr-robot/map-registry.yaml")
        command.add_argument("--state", default="/home/boreas/.local/state/lr-robot/active_map.json")
        command.add_argument("--fallback-map-id", required=True)
        command.set_defaults(handler=handler)
    arguments = parser.parse_args()
    try:
        arguments.handler(arguments)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
