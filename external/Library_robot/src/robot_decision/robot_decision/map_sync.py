"""Download approved ROS maps from a control station without changing the active map.

The synchronizer is deliberately independent from ROS.  It runs as a small
root-owned network job, verifies the station with the robot's mTLS identity,
then hands files to the existing local map registry staging tool.  A successful
sync only makes a new ``MAP-...`` selectable; the Gateway still performs the
separate stationary, idle, Nav2-checked map switch.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

import yaml

from robot_decision.map_registry import RegisteredMap, load_map_registry


_MAP_ID_PATTERN = re.compile(r"^MAP-[A-Za-z0-9_-]{1,80}$")
_ROBOT_ID_PATTERN = re.compile(r"^LR-[A-Za-z0-9_-]{1,80}$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
_MAX_MAPS = 64


class MapSyncError(ValueError):
    """A non-destructive map synchronization failure."""


@dataclass(frozen=True)
class MapArtifact:
    name: str
    sha256: str
    size: int


@dataclass(frozen=True)
class MapPackage:
    map_id: str
    yaml: MapArtifact
    pgm: MapArtifact


@dataclass(frozen=True)
class MapSyncConfig:
    station_url: str
    robot_id: str
    ca_file: Path
    certificate_file: Path
    key_file: Path
    registry_file: Path
    storage_root: Path
    state_file: Path


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):  # noqa: N802
        return None


def load_map_sync_config(path: Path) -> MapSyncConfig:
    """Parse the root-managed configuration without accepting loose fields."""

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise MapSyncError(f"cannot read map sync configuration: {error}") from error
    if not isinstance(raw, dict) or set(raw) != {
        "version",
        "stationUrl",
        "robotId",
        "caFile",
        "certificateFile",
        "keyFile",
        "registryFile",
        "storageRoot",
        "stateFile",
    }:
        raise MapSyncError("map sync configuration has invalid fields")
    if raw.get("version") != 1:
        raise MapSyncError("map sync configuration must contain version: 1")
    robot_id = raw.get("robotId")
    if not isinstance(robot_id, str) or not _ROBOT_ID_PATTERN.fullmatch(robot_id):
        raise MapSyncError("map sync robotId is invalid")
    station_url = _normalized_station_url(raw.get("stationUrl"))
    paths: dict[str, Path] = {}
    for key in ("caFile", "certificateFile", "keyFile", "registryFile", "storageRoot", "stateFile"):
        value = raw.get(key)
        if not isinstance(value, str) or not value.startswith("/"):
            raise MapSyncError(f"map sync {key} must be an absolute path")
        paths[key] = Path(value)
    for key in ("caFile", "certificateFile", "keyFile"):
        if not paths[key].is_file() or paths[key].is_symlink():
            raise MapSyncError(f"map sync {key} is unavailable")
    return MapSyncConfig(
        station_url=station_url,
        robot_id=robot_id,
        ca_file=paths["caFile"],
        certificate_file=paths["certificateFile"],
        key_file=paths["keyFile"],
        registry_file=paths["registryFile"],
        storage_root=paths["storageRoot"],
        state_file=paths["stateFile"],
    )


def synchronize_maps(config: MapSyncConfig, registry_script: Path) -> list[str]:
    """Synchronize newly published packages and return newly staged map IDs."""

    packages = _read_manifest(_make_opener(config), config.station_url)
    registry = load_map_registry(config.registry_file) if config.registry_file.exists() else {}
    state = _load_sync_state(config.state_file)
    staged: list[str] = []
    for package in packages:
        previous = state.get(package.map_id)
        if _is_already_synced(package, previous, registry):
            continue
        if package.map_id in registry:
            if _registered_map_matches(package, registry[package.map_id]):
                # A power loss after staging but before saving state is safe:
                # reconstruct the tiny state record from verified local files.
                state[package.map_id] = {
                    "yamlSha256": package.yaml.sha256,
                    "pgmSha256": package.pgm.sha256,
                }
                _write_sync_state(config.state_file, state)
                continue
            raise MapSyncError(f"map {package.map_id} is already locally registered with different content")
        _stage_downloaded_map(config, package, registry_script)
        registry = load_map_registry(config.registry_file)
        state[package.map_id] = {
            "yamlSha256": package.yaml.sha256,
            "pgmSha256": package.pgm.sha256,
        }
        _write_sync_state(config.state_file, state)
        staged.append(package.map_id)
    return staged


def _normalized_station_url(value: object) -> str:
    if not isinstance(value, str):
        raise MapSyncError("map sync stationUrl is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise MapSyncError("map sync stationUrl must be a root HTTPS URL")
    return f"https://{parsed.netloc}"


def _make_opener(config: MapSyncConfig):
    try:
        context = ssl.create_default_context(cafile=config.ca_file)
        context.load_cert_chain(config.certificate_file, config.key_file)
    except (OSError, ssl.SSLError) as error:
        raise MapSyncError(f"cannot prepare map sync mTLS context: {error}") from error
    return build_opener(HTTPSHandler(context=context), _NoRedirect())


def _read_manifest(opener, station_url: str) -> list[MapPackage]:
    payload = _read_response(opener, f"{station_url}/api/v1/vehicle/maps/manifest", _MAX_MANIFEST_BYTES)
    try:
        document = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as error:
        raise MapSyncError("vehicle map manifest is not valid JSON") from error
    if not isinstance(document, dict) or set(document) != {"schemaVersion", "maps"}:
        raise MapSyncError("vehicle map manifest has invalid fields")
    if document.get("schemaVersion") != "lr-vehicle-map-manifest-v1" or not isinstance(document.get("maps"), list):
        raise MapSyncError("vehicle map manifest has invalid schema")
    raw_maps = document["maps"]
    if len(raw_maps) > _MAX_MAPS:
        raise MapSyncError("vehicle map manifest contains too many maps")
    packages = [_parse_package(item) for item in raw_maps]
    if len({item.map_id for item in packages}) != len(packages):
        raise MapSyncError("vehicle map manifest has duplicate map IDs")
    return packages


def _parse_package(raw: object) -> MapPackage:
    if not isinstance(raw, dict) or set(raw) != {"mapId", "version", "createdAt", "yaml", "pgm"}:
        raise MapSyncError("vehicle map package has invalid fields")
    map_id = raw.get("mapId")
    if not isinstance(map_id, str) or not _MAP_ID_PATTERN.fullmatch(map_id):
        raise MapSyncError("vehicle map package has invalid mapId")
    if not isinstance(raw.get("version"), str) or not isinstance(raw.get("createdAt"), str):
        raise MapSyncError("vehicle map package has invalid version metadata")
    yaml_artifact = _parse_artifact(raw.get("yaml"), ".yaml")
    pgm_artifact = _parse_artifact(raw.get("pgm"), ".pgm")
    return MapPackage(map_id=map_id, yaml=yaml_artifact, pgm=pgm_artifact)


def _parse_artifact(raw: object, suffix: str) -> MapArtifact:
    if not isinstance(raw, dict) or set(raw) != {"name", "sha256", "bytes"}:
        raise MapSyncError("vehicle map artifact has invalid fields")
    name, digest, size = raw.get("name"), raw.get("sha256"), raw.get("bytes")
    if (
        not isinstance(name, str)
        or Path(name).name != name
        or not name.lower().endswith(suffix)
        or not isinstance(digest, str)
        or not _SHA256_PATTERN.fullmatch(digest)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or not 0 < size <= _MAX_ARTIFACT_BYTES
    ):
        raise MapSyncError("vehicle map artifact metadata is invalid")
    return MapArtifact(name=name, sha256=digest, size=size)


def _read_response(opener, url: str, maximum_bytes: int) -> bytes:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with opener.open(request, timeout=20) as response:
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > maximum_bytes:
                        raise MapSyncError("vehicle map response exceeds the permitted size")
                except ValueError as error:
                    raise MapSyncError("vehicle map response has invalid Content-Length") from error
            body = response.read(maximum_bytes + 1)
    except HTTPError as error:
        raise MapSyncError(f"vehicle map server rejected request: HTTP {error.code}") from error
    except (OSError, URLError, ssl.SSLError) as error:
        raise MapSyncError(f"vehicle map download failed: {error}") from error
    if len(body) > maximum_bytes:
        raise MapSyncError("vehicle map response exceeds the permitted size")
    return body


def _stage_downloaded_map(config: MapSyncConfig, package: MapPackage, registry_script: Path) -> None:
    if not registry_script.is_file():
        raise MapSyncError("local map registry staging script is unavailable")
    config.storage_root.mkdir(mode=0o750, parents=True, exist_ok=True)
    opener = _make_opener(config)
    with tempfile.TemporaryDirectory(prefix=f".map-sync-{package.map_id}.", dir=config.storage_root) as temporary:
        temporary_dir = Path(temporary)
        yaml_path = temporary_dir / package.yaml.name
        pgm_path = temporary_dir / package.pgm.name
        _download_artifact(opener, config.station_url, package.map_id, "yaml", package.yaml, yaml_path)
        _download_artifact(opener, config.station_url, package.map_id, "pgm", package.pgm, pgm_path)
        command = [
            os.environ.get("PYTHON", "python3"),
            str(registry_script),
            "stage",
            "--map-id",
            package.map_id,
            "--map-yaml",
            str(yaml_path),
            "--registry",
            str(config.registry_file),
            "--storage-root",
            str(config.storage_root),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=45)
        except (OSError, subprocess.SubprocessError) as error:
            raise MapSyncError(f"local map staging failed: {error}") from error


def _download_artifact(opener, station_url: str, map_id: str, kind: str, artifact: MapArtifact, path: Path) -> None:
    content = _read_response(opener, f"{station_url}/api/v1/vehicle/maps/{map_id}/files/{kind}", artifact.size)
    if len(content) != artifact.size or hashlib.sha256(content).hexdigest() != artifact.sha256:
        raise MapSyncError(f"vehicle map {map_id} {kind} checksum verification failed")
    path.write_bytes(content)
    os.chmod(path, 0o600)


def _is_already_synced(
    package: MapPackage, previous: object, registry: dict[str, RegisteredMap]
) -> bool:
    if not isinstance(previous, dict) or package.map_id not in registry:
        return False
    # map-registry.py rewrites the local YAML image name to ``map.pgm``. The
    # state holds source artifact digests, so compare those only to the source
    # manifest here rather than incorrectly hashing the normalized local YAML.
    return (
        previous.get("yamlSha256") == package.yaml.sha256
        and previous.get("pgmSha256") == package.pgm.sha256
        and _registered_map_matches(package, registry[package.map_id])
    )


def _registered_map_matches(package: MapPackage, registered: RegisteredMap) -> bool:
    """Recognize a complete locally staged map after an interrupted state write."""

    yaml_path = registered.yaml_path
    try:
        document = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        image_name = document.get("image") if isinstance(document, dict) else None
        if not isinstance(image_name, str) or Path(image_name).name != image_name:
            return False
        pgm_path = yaml_path.parent / image_name
        return (
            image_name == "map.pgm"
            and yaml_path.is_file()
            and pgm_path.is_file()
            and not yaml_path.is_symlink()
            and not pgm_path.is_symlink()
            and pgm_path.stat().st_size == package.pgm.size
            and _sha256_file(pgm_path) == package.pgm.sha256
        )
    except (OSError, yaml.YAMLError):
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_sync_state(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise MapSyncError(f"cannot read map sync state: {error}") from error
    if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("maps"), dict):
        raise MapSyncError("map sync state is invalid")
    maps: dict[str, dict[str, str]] = {}
    for map_id, details in document["maps"].items():
        if (
            not isinstance(map_id, str)
            or not _MAP_ID_PATTERN.fullmatch(map_id)
            or not isinstance(details, dict)
            or not _SHA256_PATTERN.fullmatch(str(details.get("yamlSha256", "")))
            or not _SHA256_PATTERN.fullmatch(str(details.get("pgmSha256", "")))
        ):
            raise MapSyncError("map sync state contains invalid map metadata")
        maps[map_id] = {"yamlSha256": details["yamlSha256"], "pgmSha256": details["pgmSha256"]}
    return maps


def _write_sync_state(path: Path, maps: dict[str, dict[str, str]]) -> None:
    document = {"version": 1, "maps": dict(sorted(maps.items()))}
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError as error:
        raise MapSyncError(f"cannot save map sync state: {error}") from error
