"""Own the lifecycle of bookarm's direct hardware services."""

from __future__ import annotations

from typing import Any, Iterable

from bookarm.api.sdk import BookArmSDK


class BookArmRuntime:
    """A started runtime is the only object allowed to own physical devices."""

    def __init__(self, sdk: BookArmSDK, services: Iterable[Any]) -> None:
        self.sdk = sdk
        self._services = tuple(services)
        self._started = False
        self._start_errors: dict[str, str] = {}

    @property
    def started(self) -> bool:
        return self._started

    @property
    def start_errors(self) -> dict[str, str]:
        """Per-service start failures collected during degraded startup."""
        return dict(self._start_errors)

    def start(self) -> None:
        # Degraded startup: a missing or unpowered device must not take the
        # whole HTTP service down. Each failure is recorded by service class
        # name; the status endpoints report the device as not connected and
        # any workflow touching it fails with that device's own error.
        if self._started:
            return
        self._start_errors = {}
        for service in self._services:
            start = getattr(service, "start", None)
            if callable(start):
                try:
                    start()
                except Exception as error:
                    self._start_errors[type(service).__name__] = str(error)
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        for service in reversed(self._services):
            stop = getattr(service, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception:
                    # Stop every remaining device even if a transport fails.
                    pass
        self._started = False

    def __enter__(self) -> "BookArmRuntime":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()
