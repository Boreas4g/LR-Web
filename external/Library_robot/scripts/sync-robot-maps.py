#!/usr/bin/env python3

"""Fetch newly imported control-station maps into this robot's local registry.

【已弃用】单机交付已弃用本脚本，地图导入走控制站本机直注册
（register-robot-map.sh / 建图代理 /mapping/save 自动登记）。本文件仅保留作
历史参考，不再由 lr-robot-map-sync.timer 调用；不要在新交付中执行。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE / "src" / "robot_decision"))

from robot_decision.map_sync import MapSyncError, load_map_sync_config, synchronize_maps  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/lr-robot/map-sync.yaml")
    arguments = parser.parse_args()
    try:
        staged = synchronize_maps(load_map_sync_config(Path(arguments.config)), WORKSPACE / "scripts" / "map-registry.py")
    except MapSyncError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    if staged:
        print(f"Staged synchronized maps: {', '.join(staged)}")
    else:
        print("Robot map registry is already synchronized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
