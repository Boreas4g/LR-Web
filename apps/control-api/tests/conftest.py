import pytest

from app.store import store


@pytest.fixture(autouse=True)
def synchronous_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests write-through: persist synchronously instead of debouncing."""
    monkeypatch.setattr(store, "persist_delay_seconds", 0)
