import json
import socket
import threading
from types import SimpleNamespace

import pytest

from robot_decision.bookarm_client import BookArmApiError
from robot_decision.bookarm_takeout_client import BookArmTakeoutClient
from robot_decision.edge_gateway_node import EdgeGatewayNode
from robot_decision.hardware_adapter import HardwareAdapter


def test_takeout_client_exposes_conservative_capabilities() -> None:
    client = BookArmTakeoutClient("/run/lr-bookarm/bookarm.sock", 1.0)

    capabilities = client.capabilities()

    assert capabilities["softwareStop"] is True
    assert capabilities["liftRelativeMotion"] is False
    assert capabilities["bookWorkflowModes"] == []


def test_takeout_client_posts_to_safety_routes(monkeypatch) -> None:
    client = BookArmTakeoutClient("/run/lr-bookarm/bookarm.sock", 1.0, token="secret")
    requests = []
    monkeypatch.setattr(
        client,
        "_request",
        lambda method, path, payload=None: requests.append((method, path, payload))
        or {"accepted": True, "armStopped": True, "liftStopped": True, "taskInterrupted": False},
    )

    stop = client.safe_stop()
    emergency = client.emergency_stop()

    assert stop["accepted"] is True
    assert emergency["accepted"] is True
    assert requests == [
        ("POST", "/api/v1/safety/stop", {}),
        ("POST", "/api/v1/safety/emergency-stop", {}),
    ]


def test_takeout_client_rejects_an_unacknowledged_stop(monkeypatch) -> None:
    client = BookArmTakeoutClient("/run/lr-bookarm/bookarm.sock", 1.0)
    monkeypatch.setattr(client, "_request", lambda method, path, payload=None: {"accepted": False})

    with pytest.raises(BookArmApiError, match="未被确认"):
        client.safe_stop()
    with pytest.raises(BookArmApiError, match="未被确认"):
        client.emergency_stop()


def test_takeout_client_refuses_legacy_maintenance_surface() -> None:
    client = BookArmTakeoutClient("/run/lr-bookarm/bookarm.sock", 1.0)

    with pytest.raises(BookArmApiError):
        client.move_lift(3.0)
    with pytest.raises(BookArmApiError):
        client.prepare_arm()
    with pytest.raises(BookArmApiError):
        client.start_workflow("takeout")
    with pytest.raises(BookArmApiError):
        client.workflow("BA-1")
    with pytest.raises(BookArmApiError):
        client.continue_workflow("BA-1")
    with pytest.raises(BookArmApiError):
        client.cancel_workflow("BA-1")


class _UnixSocketServer:
    """One-request-at-a-time HTTP server on a real AF_UNIX socket."""

    def __init__(self, socket_path: str, status: int = 200, body: dict | None = None) -> None:
        self.status = status
        self.body = body if body is not None else {"accepted": True}
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(socket_path)
        self._listener.listen(4)
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._closed.is_set():
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            with connection:
                connection.settimeout(2.0)
                raw = b""
                while b"\r\n\r\n" not in raw:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    raw += chunk
                head, _, rest = raw.partition(b"\r\n\r\n")
                lines = head.decode("latin-1").split("\r\n")
                method, path, _version = lines[0].split(" ", 2)
                headers = {}
                for line in lines[1:]:
                    name, _, value = line.partition(":")
                    headers[name.strip().lower()] = value.strip()
                length = int(headers.get("content-length", "0"))
                body = rest
                while len(body) < length:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    body += chunk
                self.requests.append((method, path, headers, body[:length]))
                payload = json.dumps(self.body).encode("utf-8")
                reason = "OK" if self.status < 400 else "Unauthorized"
                connection.sendall(
                    f"HTTP/1.1 {self.status} {reason}\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(payload)}\r\n"
                    "Connection: close\r\n\r\n".encode("latin-1") + payload
                )

    def close(self) -> None:
        self._closed.set()
        self._listener.close()
        self._thread.join(timeout=2.0)


def test_takeout_client_speaks_http_over_unix_socket_with_bearer_token(tmp_path) -> None:
    socket_path = str(tmp_path / "bookarm.sock")
    server = _UnixSocketServer(
        socket_path,
        body={"accepted": True, "armStopped": True, "liftStopped": True, "taskInterrupted": True},
    )
    try:
        client = BookArmTakeoutClient(socket_path, 3.0, token="takeout-secret")

        stop = client.safe_stop()
        emergency = client.emergency_stop()
    finally:
        server.close()

    assert stop["taskInterrupted"] is True
    assert emergency["accepted"] is True
    assert [request[:2] for request in server.requests] == [
        ("POST", "/api/v1/safety/stop"),
        ("POST", "/api/v1/safety/emergency-stop"),
    ]
    for _method, _path, headers, body in server.requests:
        assert headers.get("authorization") == "Bearer takeout-secret"
        assert json.loads(body.decode("utf-8")) == {}


def test_takeout_client_omits_authorization_without_token(tmp_path) -> None:
    socket_path = str(tmp_path / "bookarm.sock")
    server = _UnixSocketServer(socket_path, body={"started": True, "mode": "real"})
    try:
        client = BookArmTakeoutClient(socket_path, 3.0)

        health = client.health()
    finally:
        server.close()

    assert health.available is True
    assert health.arm_connected is True
    assert health.arm_state == "standby"
    _method, path, headers, _body = server.requests[0]
    assert path == "/api/v1/takeout/health"
    assert "authorization" not in headers


def test_takeout_client_surfaces_http_errors(tmp_path) -> None:
    socket_path = str(tmp_path / "bookarm.sock")
    server = _UnixSocketServer(socket_path, status=401, body={"detail": "missing or invalid bearer token"})
    try:
        client = BookArmTakeoutClient(socket_path, 3.0, token="wrong")

        with pytest.raises(BookArmApiError, match="HTTP 401"):
            client.safe_stop()
    finally:
        server.close()


def test_takeout_client_reports_unreachable_socket(tmp_path) -> None:
    client = BookArmTakeoutClient(str(tmp_path / "missing.sock"), 0.5)

    with pytest.raises(BookArmApiError, match="不可达"):
        client.emergency_stop()

    health = client.health()
    assert health.available is False


def test_hardware_adapter_drives_takeout_stop_chain_in_order() -> None:
    client = BookArmTakeoutClient("/run/lr-bookarm/bookarm.sock", 1.0)
    requests = []
    client._request = lambda method, path, payload=None: requests.append(path) or {"accepted": True}
    adapter = HardwareAdapter(client)

    safe = adapter.safe_stop()
    emergency = adapter.emergency_stop()

    assert safe["accepted"] is True
    assert emergency["accepted"] is True
    assert requests == [
        "/api/v1/safety/stop",
        "/api/v1/safety/stop",
        "/api/v1/safety/emergency-stop",
    ]


def _bare_gateway_node(backend: str, token: str = "") -> EdgeGatewayNode:
    node = EdgeGatewayNode.__new__(EdgeGatewayNode)
    node.bookarm_backend = backend
    node.bookarm_takeout_token = token
    node.allowed_map_ids = {"MAP-1"}
    node.active_map_id = "MAP-1"
    node.map_min_x, node.map_max_x = 0.0, 1.0
    node.map_min_y, node.map_max_y = 0.0, 1.0
    node.max_abs_yaw = 6.284
    node.telemetry_period_seconds = 0.5
    node.pose_stale_timeout_seconds = 5.0
    node.max_position_variance = 0.5
    node.max_yaw_variance = 0.35
    node.manual_command_timeout_seconds = 1.0
    node.chassis_status_timeout_seconds = 2.5
    node.imu_stale_timeout_seconds = 2.0
    node.wheel_speed_timeout_seconds = 1.0
    node.base_stationary_speed_threshold_mps = 0.01
    node.bookarm_timeout_seconds = 3.0
    node.book_shelf_fine_tune_settle_delay_seconds = 1.0
    node.mqtt_tls_enabled = False
    node.mqtt_ca_file = ""
    node.mqtt_client_cert_file = ""
    node.mqtt_client_key_file = ""
    node.mqtt_protocol_version = "3.1.1"
    node.get_logger = lambda: SimpleNamespace(warn=lambda _message: None)
    return node


def test_gateway_configuration_accepts_the_takeout_backend() -> None:
    node = _bare_gateway_node("takeout")

    node._validate_configuration()


def test_gateway_configuration_rejects_an_unknown_backend() -> None:
    node = _bare_gateway_node("bogus")

    with pytest.raises(RuntimeError, match="legacy, sdk or takeout"):
        node._validate_configuration()
