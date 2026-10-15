"""Vehicle-side registration of an imported map (best effort, never blocking)."""

import subprocess
from base64 import b64encode
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app import map_register
from app.main import app
from app.schemas import MapOrigin, MapVersion

client = TestClient(app)

YAML_DATA = b"""image: reg_map.pgm
mode: trinary
resolution: 0.050
origin: [-1.0, -2.0, 0]
negate: 0
occupied_thresh: 0.65
free_thresh: 0.196
"""
PGM_DATA = b"P5\n2 2\n255\n\x00\xfe\xcd\xfe"


def auth_headers() -> dict[str, str]:
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['token']}"}


def make_map_version(map_id: str = "MAP-REG00000001") -> MapVersion:
    return MapVersion(
        id=map_id,
        name="注册测试地图",
        version="v20260929-000000",
        yamlFile="reg_map.yaml",
        pgmFile="reg_map.pgm",
        width=2,
        height=2,
        resolution=0.05,
        origin=MapOrigin(x=-1.0, y=-2.0, yaw=0.0),
        mode="trinary",
        negate=False,
        occupiedThreshold=0.65,
        freeThreshold=0.196,
        status="draft",
        createdAt=datetime.now(UTC),
        createdBy="admin",
    )


def prepare_source(tmp_path, map_id: str):
    source_dir = tmp_path / "maps" / map_id
    source_dir.mkdir(parents=True)
    (source_dir / "reg_map.yaml").write_bytes(YAML_DATA)
    (source_dir / "reg_map.pgm").write_bytes(PGM_DATA)
    return tmp_path / "maps"


@pytest.fixture
def vehicle_env(tmp_path, monkeypatch):
    """Point the register helper at a fake vehicle store + script in tmp_path."""
    vehicle_dir = tmp_path / "vehicle-maps"
    vehicle_dir.mkdir()
    script = tmp_path / "register-robot-map.sh"
    script.write_text("#!/bin/sh\n")
    monkeypatch.setattr(map_register, "VEHICLE_MAP_STORE_DIR", vehicle_dir)
    monkeypatch.setattr(map_register, "REGISTER_SCRIPT", script)
    return vehicle_dir, script


def completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def test_register_success_invokes_sudo_stage(monkeypatch, vehicle_env, tmp_path) -> None:
    map_version = make_map_version()
    storage = prepare_source(tmp_path, map_version.id)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return completed(0)

    monkeypatch.setattr(map_register.subprocess, "run", fake_run)
    registered, note = map_register.register_imported_map(map_version, storage, enabled=True)
    assert registered is True
    assert note is None
    argv = calls[0]
    assert argv[:3] == ["sudo", "-n", str(vehicle_env[1])]
    assert argv[3] == "stage"
    assert "--map-id" in argv and map_version.id in argv
    yaml_arg = argv[argv.index("--map-yaml") + 1]
    assert yaml_arg.startswith("/tmp/")


def test_register_failure_reports_reason_without_raising(monkeypatch, vehicle_env, tmp_path) -> None:
    map_version = make_map_version("MAP-REG00000002")
    storage = prepare_source(tmp_path, map_version.id)

    def fake_run(argv, **kwargs):
        return completed(1, "map MAP-REG00000002 is already staged; use --replace after review")

    monkeypatch.setattr(map_register.subprocess, "run", fake_run)
    registered, note = map_register.register_imported_map(map_version, storage, enabled=True)
    assert registered is False
    assert note is not None and "车端地图注册失败" in note
    assert "already staged" in note


def test_register_timeout_reports_reason(monkeypatch, vehicle_env, tmp_path) -> None:
    map_version = make_map_version("MAP-REG00000003")
    storage = prepare_source(tmp_path, map_version.id)

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    monkeypatch.setattr(map_register.subprocess, "run", fake_run)
    registered, note = map_register.register_imported_map(map_version, storage, enabled=True)
    assert registered is False
    assert note is not None and "超时" in note


def test_register_skipped_when_disabled(tmp_path) -> None:
    map_version = make_map_version("MAP-REG00000004")
    storage = prepare_source(tmp_path, map_version.id)
    registered, note = map_register.register_imported_map(map_version, storage, enabled=False)
    assert registered is False
    assert note is not None and "开关已关闭" in note


def test_register_skipped_without_vehicle_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(map_register, "VEHICLE_MAP_STORE_DIR", tmp_path / "missing-dir")
    monkeypatch.setattr(map_register, "REGISTER_SCRIPT", tmp_path / "missing.sh")
    map_version = make_map_version("MAP-REG00000005")
    storage = prepare_source(tmp_path, map_version.id)
    registered, note = map_register.register_imported_map(map_version, storage, enabled=True)
    assert registered is False
    assert note is not None and "未检测到车端地图注册环境" in note


def import_payload(map_id: str | None = None) -> dict:
    payload = {
        "name": "注册联调地图",
        "yamlName": "reg_map.yaml",
        "yamlContent": b64encode(YAML_DATA).decode(),
        "pgmName": "reg_map.pgm",
        "pgmContent": b64encode(PGM_DATA).decode(),
    }
    if map_id is not None:
        payload["id"] = map_id
    return payload


def test_import_endpoint_registers_to_vehicle(monkeypatch, vehicle_env, tmp_path) -> None:
    from app.api import operations
    from app.store import store

    monkeypatch.setattr(store, "map_storage_dir", tmp_path / "maps")
    monkeypatch.setattr(operations.settings, "vehicle_map_register_enabled", True)
    monkeypatch.setattr(map_register.subprocess, "run", lambda argv, **kwargs: completed(0))
    map_id = "MAP-REG00000006"
    try:
        response = client.post(
            "/api/v1/library/maps/import", headers=auth_headers(), json=import_payload(map_id)
        )
        assert response.status_code == 201
        body = response.json()
        assert body["id"] == map_id
        assert body["vehicleRegistered"] is True
        assert body["vehicleRegisterNote"] is None
    finally:
        store.map_versions = [item for item in store.map_versions if item.id != map_id]
        store.map_annotations.pop(map_id, None)


def test_import_endpoint_survives_registration_failure(monkeypatch, vehicle_env, tmp_path) -> None:
    from app.api import operations
    from app.store import store

    monkeypatch.setattr(store, "map_storage_dir", tmp_path / "maps")
    monkeypatch.setattr(operations.settings, "vehicle_map_register_enabled", True)

    def fake_run(argv, **kwargs):
        return completed(2, "sudo: a password is required")

    monkeypatch.setattr(map_register.subprocess, "run", fake_run)
    map_id = "MAP-REG00000007"
    try:
        response = client.post(
            "/api/v1/library/maps/import", headers=auth_headers(), json=import_payload(map_id)
        )
        assert response.status_code == 201
        body = response.json()
        assert body["vehicleRegistered"] is False
        assert "password is required" in body["vehicleRegisterNote"]
        assert any(item.id == map_id for item in store.map_versions)
    finally:
        store.map_versions = [item for item in store.map_versions if item.id != map_id]
        store.map_annotations.pop(map_id, None)


def test_import_endpoint_skips_registration_when_disabled(monkeypatch, tmp_path) -> None:
    from app.api import operations
    from app.store import store

    monkeypatch.setattr(store, "map_storage_dir", tmp_path / "maps")
    monkeypatch.setattr(operations.settings, "vehicle_map_register_enabled", False)

    def forbidden_run(argv, **kwargs):
        raise AssertionError("subprocess must not run when registration is disabled")

    monkeypatch.setattr(map_register.subprocess, "run", forbidden_run)
    map_id = "MAP-REG00000008"
    try:
        response = client.post(
            "/api/v1/library/maps/import", headers=auth_headers(), json=import_payload(map_id)
        )
        assert response.status_code == 201
        body = response.json()
        assert body["vehicleRegistered"] is False
        assert "开关已关闭" in body["vehicleRegisterNote"]
    finally:
        store.map_versions = [item for item in store.map_versions if item.id != map_id]
        store.map_annotations.pop(map_id, None)
