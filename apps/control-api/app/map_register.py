"""Best-effort registration of an imported map into the vehicle-side map store.

Single-machine delivery runs the control station on the vehicle computer: an
operator-imported map must additionally be staged into the vehicle-owned store
(/var/lib/lr-robot/maps) before the Gateway accepts a switch to that map ID.
Registration never blocks the library import — a failure is reported on the
import response so the operator can register manually later.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .schemas import MapVersion

# Vehicle-side prerequisites; both must exist on this host to attempt staging.
VEHICLE_MAP_STORE_DIR = Path("/var/lib/lr-robot/maps")
REGISTER_SCRIPT = Path("/home/boreas/LR/Library_robot/scripts/register-robot-map.sh")
REGISTER_TIMEOUT_SECONDS = 120


def register_imported_map(
    map_version: MapVersion, map_storage_dir: Path, *, enabled: bool
) -> tuple[bool, str | None]:
    """Stage one freshly imported map into the vehicle registry via sudo.

    Returns (registered, note): note is None on success and carries the
    operator-facing reason otherwise.
    """
    if not enabled:
        return False, "车端地图注册开关已关闭（vehicle_map_register_enabled=false）"
    if not VEHICLE_MAP_STORE_DIR.is_dir() or not REGISTER_SCRIPT.is_file():
        return False, "本机未检测到车端地图注册环境，地图仅入库控制站"
    source_dir = map_storage_dir / map_version.id
    # The registry script resolves the PGM through the YAML's image field, so
    # both files are staged into one /tmp directory under their original names.
    try:
        with tempfile.TemporaryDirectory(prefix="lr-map-register-", dir="/tmp") as temp_dir:
            temp_yaml = Path(temp_dir) / map_version.yamlFile
            shutil.copyfile(source_dir / map_version.yamlFile, temp_yaml)
            shutil.copyfile(source_dir / map_version.pgmFile, Path(temp_dir) / map_version.pgmFile)
            completed = subprocess.run(
                # sudoers 白名单仅放行脚本绝对路径（/etc/sudoers.d/lr-map-register），
                # 因此直接执行脚本本身而不是经 bash 中转。
                [
                    "sudo",
                    "-n",
                    str(REGISTER_SCRIPT),
                    "stage",
                    "--map-id",
                    map_version.id,
                    "--map-yaml",
                    str(temp_yaml),
                ],
                capture_output=True,
                text=True,
                timeout=REGISTER_TIMEOUT_SECONDS,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return False, "车端地图注册脚本执行超时，请稍后在车端手动注册"
    except OSError as error:
        return False, f"车端地图注册脚本无法执行：{error}"
    if completed.returncode != 0:
        lines = (completed.stderr or completed.stdout or "").strip().splitlines()
        tail = lines[-1] if lines else f"退出码 {completed.returncode}"
        return False, f"车端地图注册失败：{tail}"
    return True, None
