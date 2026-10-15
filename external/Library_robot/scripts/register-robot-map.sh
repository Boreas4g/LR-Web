#!/usr/bin/env bash
set -euo pipefail

# Copy a reviewed map into the vehicle-owned store. The Gateway later accepts
# only map IDs from this registry, never paths received over MQTT.
if [[ "$(id -u)" -ne 0 ]]; then
  echo '请使用 sudo 执行。' >&2
  exit 1
fi

WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec env PYTHONDONTWRITEBYTECODE=1 python3 "$WORKSPACE/scripts/map-registry.py" stage "$@"
