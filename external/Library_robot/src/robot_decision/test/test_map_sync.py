import hashlib
import json
from pathlib import Path

import pytest
import yaml

from robot_decision.map_sync import (
    MapArtifact,
    MapPackage,
    MapSyncConfig,
    MapSyncError,
    _load_sync_state,
    _parse_package,
    _write_sync_state,
    synchronize_maps,
)


def _pgm() -> bytes:
    return b"P5\n2 2\n255\n\x00\xfe\xcd\xfe"


def test_map_sync_rejects_invalid_vehicle_artifact_metadata() -> None:
    with pytest.raises(MapSyncError, match="artifact"):
        _parse_package(
            {
                "mapId": "MAP-01",
                "version": "v1",
                "createdAt": "2026-01-01T00:00:00Z",
                "yaml": {"name": "../../map.yaml", "sha256": "0" * 64, "bytes": 3},
                "pgm": {"name": "map.pgm", "sha256": "0" * 64, "bytes": 3},
            }
        )


def test_map_sync_state_is_atomic_and_validated(tmp_path) -> None:
    state = tmp_path / "sync-state.json"
    _write_sync_state(state, {"MAP-01": {"yamlSha256": "a" * 64, "pgmSha256": "b" * 64}})

    assert _load_sync_state(state) == {"MAP-01": {"yamlSha256": "a" * 64, "pgmSha256": "b" * 64}}
    assert json.loads(state.read_text(encoding="utf-8"))["version"] == 1


def test_map_sync_does_not_replace_existing_map_id(tmp_path, monkeypatch) -> None:
    storage = tmp_path / "maps"
    existing = storage / "MAP-01"
    existing.mkdir(parents=True)
    map_yaml = existing / "map.yaml"
    map_yaml.write_text("image: map.pgm\n", encoding="utf-8")
    (existing / "map.pgm").write_bytes(b"different")
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "maps": {
                    "MAP-01": {
                        "yamlPath": str(map_yaml),
                        "bounds": {"minX": 0, "maxX": 1, "minY": 0, "maxY": 1},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    cert = tmp_path / "robot.crt"
    key = tmp_path / "robot.key"
    ca = tmp_path / "ca.crt"
    for path in (cert, key, ca):
        path.write_text("placeholder", encoding="utf-8")
    package = MapPackage(
        "MAP-01",
        MapArtifact("map.yaml", hashlib.sha256(b"yaml").hexdigest(), 4),
        MapArtifact("map.pgm", hashlib.sha256(_pgm()).hexdigest(), len(_pgm())),
    )
    config = MapSyncConfig("https://lr-control.local", "LR-01", ca, cert, key, registry, storage, tmp_path / "state")
    monkeypatch.setattr("robot_decision.map_sync._make_opener", lambda _config: object())
    monkeypatch.setattr("robot_decision.map_sync._read_manifest", lambda _opener, _url: [package])

    with pytest.raises(MapSyncError, match="different content"):
        synchronize_maps(config, tmp_path / "map-registry.py")


def test_map_sync_recovers_state_after_a_verified_completed_stage(tmp_path, monkeypatch) -> None:
    storage = tmp_path / "maps"
    registered_dir = storage / "MAP-01"
    registered_dir.mkdir(parents=True)
    yaml_content = b"image: map.pgm\nresolution: 0.05\norigin: [0, 0, 0]\n"
    pgm_content = _pgm()
    map_yaml = registered_dir / "map.yaml"
    map_yaml.write_bytes(yaml_content)
    (registered_dir / "map.pgm").write_bytes(pgm_content)
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "maps": {
                    "MAP-01": {
                        "yamlPath": str(map_yaml),
                        "bounds": {"minX": 0, "maxX": 1, "minY": 0, "maxY": 1},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    cert = tmp_path / "robot.crt"
    key = tmp_path / "robot.key"
    ca = tmp_path / "ca.crt"
    for path in (cert, key, ca):
        path.write_text("placeholder", encoding="utf-8")
    package = MapPackage(
        "MAP-01",
        MapArtifact("map.yaml", hashlib.sha256(yaml_content).hexdigest(), len(yaml_content)),
        MapArtifact("map.pgm", hashlib.sha256(pgm_content).hexdigest(), len(pgm_content)),
    )
    state = tmp_path / "sync-state.json"
    config = MapSyncConfig("https://lr-control.local", "LR-01", ca, cert, key, registry, storage, state)
    monkeypatch.setattr("robot_decision.map_sync._make_opener", lambda _config: object())
    monkeypatch.setattr("robot_decision.map_sync._read_manifest", lambda _opener, _url: [package])

    assert synchronize_maps(config, tmp_path / "map-registry.py") == []
    assert _load_sync_state(state)["MAP-01"]["yamlSha256"] == package.yaml.sha256
