"""One-owner command worker used by each physical device service."""

from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Queue
import threading
from typing import Any, Callable, Optional


@dataclass
class _Command:
    action: Callable[[], Any]
    done: threading.Event
    value: Any = None
    error: Optional[BaseException] = None


class DeviceCommandWorker:
    """Run normal device commands on one thread in submission order.

    A device's serial, CAN, or USB handle must only be used by this worker.
    Safety interrupts are intentionally exposed by the device controller
    itself, rather than queued behind a potentially long-running motion.
    """

    def __init__(self, name: str) -> None:
        self._name = str(name)
        self._queue: Queue[Optional[_Command]] = Queue()
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()

    @property
    def started(self) -> bool:
        return self._running.is_set()

    def start(self) -> None:
        if self._running.is_set():
            return
        self._running.set()
        self._thread = threading.Thread(
            target=self._run,
            name="bookarm-%s" % self._name,
            daemon=True,
        )
        self._thread.start()

    def call(self, action: Callable[[], Any], timeout_s: Optional[float] = None) -> Any:
        if not self._running.is_set():
            raise RuntimeError("device worker %s is not running" % self._name)
        command = _Command(action=action, done=threading.Event())
        self._queue.put(command)
        if not command.done.wait(timeout_s):
            raise TimeoutError("device worker %s command timed out" % self._name)
        if command.error is not None:
            raise command.error
        return command.value

    def stop(self, timeout_s: float = 2.0) -> None:
        if not self._running.is_set():
            return
        self._running.clear()
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, float(timeout_s)))
        self._thread = None

    def _run(self) -> None:
        while True:
            try:
                command = self._queue.get(timeout=0.1)
            except Empty:
                if not self._running.is_set():
                    return
                continue
            if command is None:
                return
            try:
                command.value = command.action()
            except BaseException as error:
                command.error = error
            finally:
                command.done.set()
