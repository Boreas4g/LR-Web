from __future__ import annotations

import asyncio
from threading import Lock
from typing import Any

from .schemas import RobotSummary


class RobotRealtimeHub:
    """Fan out validated robot updates from MQTT worker threads to WebSocket clients."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def start(self) -> None:
        with self._lock:
            self._loop = asyncio.get_running_loop()

    def stop(self) -> None:
        with self._lock:
            self._subscribers.clear()
            self._loop = None

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    def publish_robot(self, robot: RobotSummary) -> None:
        payload = {"type": "robot.updated", "robot": robot.model_dump(mode="json")}
        with self._lock:
            loop = self._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(self._publish, payload)

    def _publish(self, payload: dict[str, Any]) -> None:
        # Retain only the newest pose for a slow browser instead of building a stale backlog.
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    continue
            queue.put_nowait(payload)


robot_realtime_hub = RobotRealtimeHub()
