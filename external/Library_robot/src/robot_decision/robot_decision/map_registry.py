"""Trusted local map registry shared by the runtime launcher and Edge Gateway.

The control station sends only a map ID.  This module resolves that ID through
an administrator-maintained local registry, so an MQTT command can never pick
an arbitrary filesystem path or upload map data to the vehicle.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml


_MAP_ID_PATTERN = re.compile(r"^MAP-[A-Za-z0-9_-]{1,80}$")


@dataclass(frozen=True)
class RegisteredMap:
    """One ROS map that has already been staged and approved on this vehicle."""

    map_id: str
    yaml_path: Path
    min_x: float
    max_x: float
    min_y: float
    max_y: float


def load_map_registry(path: Path) -> dict[str, RegisteredMap]:
    """Read a small, root-managed registry without accepting loose YAML shapes."""

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"cannot read map registry: {error}") from error
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError("map registry must contain version: 1")
    raw_maps = document.get("maps")
    if not isinstance(raw_maps, dict) or not raw_maps:
        raise ValueError("map registry must contain at least one map")

    maps: dict[str, RegisteredMap] = {}
    for map_id, raw_definition in raw_maps.items():
        if not isinstance(map_id, str) or not _MAP_ID_PATTERN.fullmatch(map_id):
            raise ValueError("map registry contains an invalid map ID")
        if not isinstance(raw_definition, dict):
            raise ValueError(f"map registry entry {map_id} must be an object")
        raw_path = raw_definition.get("yamlPath")
        raw_bounds = raw_definition.get("bounds")
        if not isinstance(raw_path, str) or not raw_path.startswith("/"):
            raise ValueError(f"map registry entry {map_id} needs an absolute yamlPath")
        if not isinstance(raw_bounds, dict):
            raise ValueError(f"map registry entry {map_id} needs bounds")
        try:
            values = tuple(float(raw_bounds[key]) for key in ("minX", "maxX", "minY", "maxY"))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"map registry entry {map_id} has invalid bounds") from error
        if not all(math.isfinite(value) for value in values) or values[0] >= values[1] or values[2] >= values[3]:
            raise ValueError(f"map registry entry {map_id} has invalid bounds")
        yaml_path = Path(raw_path)
        if not yaml_path.is_file() or yaml_path.is_symlink():
            raise ValueError(f"staged map YAML is unavailable for {map_id}")
        maps[map_id] = RegisteredMap(map_id, yaml_path, *values)
    return maps


def active_map_id(state_path: Path, fallback_map_id: str) -> str:
    """Read only a committed map selection; interrupted switches remain pending."""

    if not state_path.is_file():
        return fallback_map_id
    try:
        document = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read active map state: {error}") from error
    selected = document.get("activeMapId") if isinstance(document, dict) else None
    if not isinstance(selected, str) or not _MAP_ID_PATTERN.fullmatch(selected):
        raise ValueError("active map state has no valid activeMapId")
    return selected


def stage_pending_switch(state_path: Path, active_id: str, target_id: str) -> None:
    _write_state(state_path, active_id, target_id)


def commit_map_switch(state_path: Path, target_id: str) -> None:
    _write_state(state_path, target_id, None)


def cancel_pending_switch(state_path: Path, active_id: str) -> None:
    _write_state(state_path, active_id, None)


def _write_state(state_path: Path, active_id: str, pending_id: str | None) -> None:
    if not _MAP_ID_PATTERN.fullmatch(active_id) or (
        pending_id is not None and not _MAP_ID_PATTERN.fullmatch(pending_id)
    ):
        raise ValueError("map state contains an invalid map ID")
    document: dict[str, object] = {
        "activeMapId": active_id,
        "updatedAt": datetime.now(UTC).isoformat(),
    }
    if pending_id:
        document["pendingMapId"] = pending_id
    try:
        state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, state_path)
    except OSError as error:
        raise ValueError(f"cannot persist active map state: {error}") from error


def registry_document(maps: dict[str, RegisteredMap]) -> dict[str, Any]:
    """Produce the canonical, minimal registry representation for the staging CLI."""

    return {
        "version": 1,
        "maps": {
            map_id: {
                "yamlPath": str(item.yaml_path),
                "bounds": {
                    "minX": item.min_x,
                    "maxX": item.max_x,
                    "minY": item.min_y,
                    "maxY": item.max_y,
                },
            }
            for map_id, item in sorted(maps.items())
        },
    }
