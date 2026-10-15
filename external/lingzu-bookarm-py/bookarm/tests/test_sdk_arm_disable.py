"""Public SDK arm-disable confirmation tests."""

from __future__ import annotations

import unittest

from bookarm.api.sdk import BookArmSDK
from bookarm.application.task_manager import TaskManager


class FakeTakeBook:
    def __init__(self) -> None:
        self.disable_calls: list[float] = []

    def disable_arm(self, duration_s: float) -> dict[str, object]:
        self.disable_calls.append(duration_s)
        return {"disable_command_sent": True}


class PublicSdkArmDisableTests(unittest.TestCase):
    def test_requires_explicit_confirmation_before_disabling_arm(self) -> None:
        take_book = FakeTakeBook()
        sdk = BookArmSDK(
            task_manager=TaskManager(),
            take_book=take_book,
            return_book=object(),
            return_book_to_end=object(),
        )

        with self.assertRaisesRegex(ValueError, "explicit operator confirmation"):
            sdk.disable_arm(confirmed=False)
        self.assertEqual(take_book.disable_calls, [])

        result = sdk.disable_arm(confirmed=True, duration_s=2.5)
        self.assertEqual(take_book.disable_calls, [2.5])
        self.assertTrue(result["disable_command_sent"])


if __name__ == "__main__":
    unittest.main()
