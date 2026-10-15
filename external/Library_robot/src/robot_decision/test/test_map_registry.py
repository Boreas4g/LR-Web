import json

import pytest

from robot_decision.map_registry import (
    RegisteredMap,
    active_map_id,
    commit_map_switch,
    load_map_registry,
    registry_document,
    stage_pending_switch,
)


def test_registered_map_registry_requires_regular_staged_yaml(tmp_path) -> None:
    yaml_path = tmp_path / "map.yaml"
    yaml_path.write_text("image: map.pgm\n", encoding="utf-8")
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(
        """version: 1
maps:
  MAP-01:
    yamlPath: %s
    bounds: {minX: -1, maxX: 2, minY: -3, maxY: 4}
""" % yaml_path,
        encoding="utf-8",
    )

    loaded = load_map_registry(registry_path)

    assert loaded["MAP-01"] == RegisteredMap("MAP-01", yaml_path, -1, 2, -3, 4)


def test_active_map_state_is_atomic_and_uses_the_committed_target(tmp_path) -> None:
    state_path = tmp_path / "active-map.json"
    stage_pending_switch(state_path, "MAP-02", "MAP-02")

    assert active_map_id(state_path, "MAP-01") == "MAP-02"
    assert json.loads(state_path.read_text(encoding="utf-8"))["pendingMapId"] == "MAP-02"

    commit_map_switch(state_path, "MAP-02")
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["activeMapId"] == "MAP-02"
    assert "pendingMapId" not in saved


def test_registry_rejects_a_path_that_is_not_staged(tmp_path) -> None:
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(
        """version: 1
maps:
  MAP-01:
    yamlPath: /does/not/exist.yaml
    bounds: {minX: -1, maxX: 2, minY: -3, maxY: 4}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unavailable"):
        load_map_registry(registry_path)


def test_registry_document_only_contains_canonical_registered_fields(tmp_path) -> None:
    document = registry_document(
        {"MAP-01": RegisteredMap("MAP-01", tmp_path / "map.yaml", -1, 2, -3, 4)}
    )

    assert document == {
        "version": 1,
        "maps": {
            "MAP-01": {
                "yamlPath": str(tmp_path / "map.yaml"),
                "bounds": {"minX": -1, "maxX": 2, "minY": -3, "maxY": 4},
            }
        },
    }
