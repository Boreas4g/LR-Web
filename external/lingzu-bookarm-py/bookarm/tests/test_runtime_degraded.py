"""Degraded startup must keep the service up when a device is missing."""

from bookarm.runtime.bookarm_runtime import BookArmRuntime


class _OkService:
    def __init__(self):
        self.started = False

    def start(self):
        self.started = True


class _FailingService:
    def start(self):
        raise OSError("could not open port /dev/lift_port")


def test_start_tolerates_failing_service():
    ok, failing = _OkService(), _FailingService()
    runtime = BookArmRuntime(sdk=object(), services=(ok, failing))
    runtime.start()
    assert runtime.started
    assert ok.started
    assert runtime.start_errors == {"_FailingService": "could not open port /dev/lift_port"}


def test_start_errors_empty_when_all_healthy():
    runtime = BookArmRuntime(sdk=object(), services=(_OkService(),))
    runtime.start()
    assert runtime.started
    assert runtime.start_errors == {}
