from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import paho.mqtt.client as mqtt

from .config import Settings
from .schemas import RobotCommand, User

_logger = logging.getLogger(__name__)


class MqttPublisher:
    def __init__(self, settings: Settings, event_handler: Callable[[dict[str, Any]], None] | None = None) -> None:
        if not settings.mqtt_url:
            raise ValueError("LR_MQTT_URL is required for MQTT publishing")
        parsed = urlparse(settings.mqtt_url)
        self.host = parsed.hostname or "localhost"
        self.port = parsed.port or (8883 if parsed.scheme in {"mqtts", "ssl"} else 1883)
        if settings.mqtt_tls_enabled and parsed.scheme not in {"mqtts", "ssl"}:
            raise ValueError("LR_MQTT_URL must use mqtts:// when LR_MQTT_TLS_ENABLED=true")
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="lr-control-api")
        if settings.mqtt_username:
            self.client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
        if settings.mqtt_tls_enabled:
            if not settings.mqtt_ca_file:
                raise ValueError("LR_MQTT_CA_FILE is required when LR_MQTT_TLS_ENABLED=true")
            if bool(settings.mqtt_client_cert_file) != bool(settings.mqtt_client_key_file):
                raise ValueError(
                    "LR_MQTT_CLIENT_CERT_FILE and LR_MQTT_CLIENT_KEY_FILE must be configured together"
                )
            self.client.tls_set(
                ca_certs=settings.mqtt_ca_file,
                certfile=settings.mqtt_client_cert_file,
                keyfile=settings.mqtt_client_key_file,
            )
            self.client.tls_insecure_set(False)
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._connected = False
        self._event_handler = event_handler
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    @property
    def connected(self) -> bool:
        return self._connected

    def _on_connect(self, _client, _userdata, _flags, reason_code, _properties) -> None:
        self._connected = reason_code == 0
        if self._connected:
            self._subscribe_all()

    def _subscribe_all(self) -> None:
        self.client.subscribe("fleet/+/event", qos=1)
        self.client.subscribe("fleet/+/telemetry", qos=0)

    def resubscribe(self) -> None:
        """Re-issue subscriptions to heal silent broker-side subscription loss.

        The TCP connection can stay healthy while the broker quietly drops
        subscriptions (session expiry, broker restart). paho only retries on
        TCP failure, so inbound telemetry stops while outbound publish keeps
        working. Cheap idempotent call driven by a periodic watchdog.
        """
        if self._connected:
            self._subscribe_all()

    def _on_disconnect(self, _client, _userdata, _disconnect_flags, _reason_code, _properties) -> None:
        self._connected = False

    def _on_message(self, _client, _userdata, message) -> None:
        if not self._event_handler or not message.topic.startswith("fleet/"):
            return
        topic_parts = message.topic.split("/")
        if len(topic_parts) != 3 or not topic_parts[1] or topic_parts[2] not in {"event", "telemetry"}:
            return
        try:
            event = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(event, dict):
            return
        robot_id = topic_parts[1]
        if topic_parts[2] == "event":
            if event.get("robotId") == robot_id:
                self._dispatch(event)
            return
        if event.get("robotId") not in {None, robot_id}:
            return
        details = event.get("details") if isinstance(event.get("details"), dict) else event
        timestamp = event.get("timestamp")
        self._last_telemetry_at = time.monotonic()
        self._dispatch(
            {
                "event": "telemetry",
                "robotId": robot_id,
                "commandId": None,
                "timestamp": timestamp if isinstance(timestamp, str) else datetime.now(UTC).isoformat(),
                "details": details,
            }
        )

    def _dispatch(self, event: dict[str, Any]) -> None:
        """Deliver one decoded message to the store without risking the loop thread.

        paho runs user callbacks on its single network-loop thread and, with
        ``suppress_exceptions`` at its default ``False``, re-raises any callback
        exception out of ``loop_forever`` — silently killing all MQTT ingress
        until the process restarts (observed on LR-01 when a PostgreSQL
        deadlock in telemetry persistence froze every robot pose and dropped
        manual-control commands while publishes still reported success).
        A storage failure must degrade to one dropped frame, never a dead client.
        """
        try:
            self._event_handler(event)
        except Exception:  # the network thread must survive regardless
            _logger.exception("MQTT 消息处理失败，已丢弃该帧（robotId=%s）", event.get("robotId"))

    def start(self) -> None:
        self.client.connect_async(self.host, self.port, keepalive=30)
        self.client.loop_start()
        self._last_telemetry_at = time.monotonic()
        self._last_reconnect_at = 0.0
        self._watchdog_stop = threading.Event()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()

    def _watchdog_loop(self) -> None:
        """Reconnect when telemetry silently stops on a healthy connection.

        The broker can drop a session's subscription without dropping the TCP
        link: outbound publish keeps working while inbound telemetry stops.
        paho only retries on TCP failure, so a periodic forced reconnect
        (which re-issues subscriptions via on_connect) is the safe heal.
        Runs at most one reconnect per 60 seconds.
        """
        while not self._watchdog_stop.wait(15.0):
            self._check_telemetry_watchdog()

    def _check_telemetry_watchdog(self) -> None:
        if not self._connected:
            return
        now = time.monotonic()
        if now - self._last_telemetry_at < 25.0:
            return
        if now - self._last_reconnect_at < 60.0:
            return
        self._last_reconnect_at = now
        try:
            self.client.reconnect()
        except Exception:
            pass

    def stop(self) -> None:
        if getattr(self, "_watchdog_stop", None) is not None:
            self._watchdog_stop.set()
        self.client.loop_stop()
        self.client.disconnect()

    def publish_command(self, command: RobotCommand, user: User | None = None) -> bool:
        if not self.connected:
            return False
        # 免鉴权直控指令（manual_jog）没有登录用户，issuedBy 退化为 "web"。
        payload = command.model_dump(mode="json") | {
            "issuedBy": user.username if user else "web",
            "safetyLevel": "high",
        }
        result = self.client.publish(f"fleet/{command.robotId}/command", json.dumps(payload), qos=1)
        return result.rc == mqtt.MQTT_ERR_SUCCESS
