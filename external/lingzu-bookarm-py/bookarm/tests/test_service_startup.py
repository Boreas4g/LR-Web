"""CLI startup-safety tests that never initialize BookArm hardware."""

from __future__ import annotations

import socket
import tempfile
import unittest
from pathlib import Path

from bookarm.service.__main__ import DEFAULT_STUDIO_DIR, _prepare_unix_socket, parse_args


class StudioStorageDefaultTests(unittest.TestCase):
    """The production spine library must live outside the code checkout."""

    def test_default_studio_dir_is_the_system_data_path(self):
        self.assertEqual(DEFAULT_STUDIO_DIR, Path("/var/lib/lr-bookarm/spine_studio"))
        self.assertEqual(parse_args([]).studio_dir, DEFAULT_STUDIO_DIR)

    def test_default_yolo_weights_track_the_inference_checkout(self):
        weights = parse_args([]).yolo_weights
        self.assertEqual(
            Path(*weights.parts[-3:]),
            Path("book_spine_inference_light") / "weights" / "best.pt",
        )


class UnixSocketPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "bookarm.sock"

    def test_removes_a_stale_socket(self) -> None:
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.path))
        stale.close()

        _prepare_unix_socket(self.path)

        self.assertFalse(self.path.exists())

    def test_refuses_to_replace_a_live_socket(self) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.path))
        listener.listen(1)
        self.addCleanup(listener.close)

        with self.assertRaisesRegex(RuntimeError, "live Unix socket"):
            _prepare_unix_socket(self.path)
        self.assertTrue(self.path.exists())

    def test_refuses_to_replace_a_regular_file(self) -> None:
        self.path.write_text("not a socket", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "non-socket path"):
            _prepare_unix_socket(self.path)


if __name__ == "__main__":
    unittest.main()
