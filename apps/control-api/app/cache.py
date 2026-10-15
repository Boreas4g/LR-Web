from __future__ import annotations

import json
from typing import Any

from redis import Redis


class RedisCache:
    def __init__(self, url: str) -> None:
        self.client = Redis.from_url(url, decode_responses=True)

    def ping(self) -> bool:
        return bool(self.client.ping())

    def set_session(self, token: str, user: dict[str, str], ttl_seconds: int = 86400) -> None:
        self.client.setex(f"session:{token}", ttl_seconds, json.dumps(user))

    def get_session(self, token: str) -> dict[str, str] | None:
        value = self.client.get(f"session:{token}")
        return json.loads(value) if value else None

    def delete_session(self, token: str) -> None:
        self.client.delete(f"session:{token}")

    def get_idempotent_command(self, key: str) -> dict[str, Any] | None:
        value = self.client.get(f"command:idempotency:{key}")
        return json.loads(value) if value else None

    def set_idempotent_command(self, key: str, command: dict[str, Any], ttl_seconds: int) -> None:
        self.client.setex(f"command:idempotency:{key}", ttl_seconds, json.dumps(command, default=str))

    def delete_idempotent_command(self, key: str) -> None:
        """Remove a command that was persisted but could not be published."""
        self.client.delete(f"command:idempotency:{key}")

    def acquire_robot_lock(self, robot_id: str, command_id: str, ttl_seconds: int) -> bool:
        return bool(self.client.set(f"robot:lock:{robot_id}", command_id, nx=True, ex=ttl_seconds))

    def release_robot_lock(self, robot_id: str, command_id: str) -> None:
        key = f"robot:lock:{robot_id}"
        if self.client.get(key) == command_id:
            self.client.delete(key)
