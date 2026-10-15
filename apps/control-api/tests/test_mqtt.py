"""Tests for the MQTT telemetry-subscription watchdog."""

from __future__ import annotations

import time

from app.mqtt import MqttPublisher


class _FakeClient:
    def __init__(self) -> None:
        self.reconnects = 0

    def reconnect(self) -> None:
        self.reconnects += 1


def _publisher_stub(*, connected: bool, telemetry_age_s: float, reconnect_age_s: float) -> MqttPublisher:
    publisher = MqttPublisher.__new__(MqttPublisher)
    publisher.client = _FakeClient()
    publisher._connected = connected
    publisher._last_telemetry_at = time.monotonic() - telemetry_age_s
    publisher._last_reconnect_at = time.monotonic() - reconnect_age_s if reconnect_age_s >= 0 else 0.0
    return publisher


def test_watchdog_reconnects_when_telemetry_is_stale() -> None:
    publisher = _publisher_stub(connected=True, telemetry_age_s=120.0, reconnect_age_s=120.0)

    publisher._check_telemetry_watchdog()

    assert publisher.client.reconnects == 1


def test_watchdog_skips_when_telemetry_is_fresh() -> None:
    publisher = _publisher_stub(connected=True, telemetry_age_s=1.0, reconnect_age_s=120.0)

    publisher._check_telemetry_watchdog()

    assert publisher.client.reconnects == 0


def test_watchdog_debounces_reconnects() -> None:
    publisher = _publisher_stub(connected=True, telemetry_age_s=120.0, reconnect_age_s=5.0)

    publisher._check_telemetry_watchdog()

    assert publisher.client.reconnects == 0


def test_watchdog_skips_when_disconnected() -> None:
    publisher = _publisher_stub(connected=False, telemetry_age_s=120.0, reconnect_age_s=120.0)

    publisher._check_telemetry_watchdog()

    assert publisher.client.reconnects == 0
