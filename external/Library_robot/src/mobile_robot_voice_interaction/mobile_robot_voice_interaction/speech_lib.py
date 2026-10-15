#!/usr/bin/env python3
"""Minimal serial helper for a command-based speech module."""

import glob
import os
import threading

import serial
from serial import SerialException
from serial.tools import list_ports


class Speech:
    FRAME_HEAD = b"\xAA\x55"
    FRAME_END = 0xFB
    FRAME_SIZE = 5
    NO_COMMAND = 999
    DEFAULT_BAUDRATE = 115200
    KNOWN_USB_IDS = {
        (0x1A86, 0x7522),
        (0x1A86, 0x7523),
    }

    def __init__(self, com="/dev/speech_port", baudrate=DEFAULT_BAUDRATE, timeout=0.05):
        env_port = os.environ.get("YAHBOOM_SPEECH_PORT")
        env_baudrate = os.environ.get("YAHBOOM_SPEECH_BAUDRATE")

        self.requested_port = env_port or com
        self.baudrate = int(env_baudrate or baudrate)
        self.timeout = float(timeout)

        self._serial = None
        self._selected_port = None
        self._buffer = bytearray()
        self._lock = threading.Lock()

        self.last_error = ""
        self._candidate_ports = []
        self._ensure_open()

    @property
    def port(self):
        return self._selected_port

    @property
    def candidate_ports(self):
        return list(self._candidate_ports)

    def close(self):
        with self._lock:
            self._disconnect_locked()

    def speech_read(self):
        if not self._ensure_open():
            return self.NO_COMMAND

        with self._lock:
            frame = self._pop_frame_locked()
            if frame is None:
                if not self._read_locked():
                    return self.NO_COMMAND
                frame = self._pop_frame_locked()

        if frame is None:
            return self.NO_COMMAND
        return frame[1]

    def void_write(self, voice_data):
        if not self._ensure_open():
            return False

        packet = bytes(
            (
                self.FRAME_HEAD[0],
                self.FRAME_HEAD[1],
                0xFF,
                int(voice_data) & 0xFF,
                self.FRAME_END,
            )
        )

        with self._lock:
            try:
                self._serial.write(packet)
                self._serial.flush()
            except (SerialException, OSError) as exc:
                self.last_error = f"Failed to write to speech module on {self._selected_port}: {exc}"
                self._disconnect_locked()
                return False
        return True

    def _ensure_open(self):
        if self._serial is not None and self._serial.is_open:
            return True

        port = self._resolve_port()
        if port is None:
            return False

        with self._lock:
            if self._serial is not None and self._serial.is_open:
                return True
            try:
                self._serial = serial.Serial(
                    port=port,
                    baudrate=self.baudrate,
                    timeout=self.timeout,
                    write_timeout=self.timeout,
                )
            except (SerialException, OSError) as exc:
                self.last_error = f"Failed to open speech module on {port}: {exc}"
                self._disconnect_locked()
                return False
            self._selected_port = port
            self.last_error = ""
        return True

    def _resolve_port(self):
        candidates = self._build_candidates()

        if self.requested_port and os.path.exists(self.requested_port):
            return self.requested_port
        if len(candidates) == 1:
            return candidates[0]

        requested = self.requested_port or "/dev/speech_port"
        if not candidates:
            self.last_error = (
                f"Speech module not found at {requested}. "
                "Set YAHBOOM_SPEECH_PORT or create /dev/speech_port."
            )
            return None

        joined = ", ".join(candidates)
        self.last_error = (
            f"Speech port is ambiguous. Preferred port {requested} does not exist. "
            f"Set YAHBOOM_SPEECH_PORT or pass com=... . Candidates: {joined}"
        )
        return None

    def _build_candidates(self):
        candidates = []

        def add(path):
            if not path or path in candidates:
                return
            if os.path.exists(path):
                candidates.append(path)

        add(self.requested_port)
        add("/dev/speech_port")

        for path in sorted(glob.glob("/dev/serial/by-id/*")):
            add(path)

        known_usb = []
        generic_serial = []
        for port in list_ports.comports():
            if not port.device:
                continue
            if (port.vid, port.pid) in self.KNOWN_USB_IDS:
                known_usb.append(port.device)
            elif port.device.startswith("/dev/ttyUSB") or port.device.startswith("/dev/ttyACM"):
                generic_serial.append(port.device)

        for path in sorted(known_usb):
            add(path)
        for path in sorted(generic_serial):
            add(path)
        for path in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")):
            add(path)

        self._candidate_ports = candidates
        return candidates

    def _read_locked(self):
        try:
            waiting = getattr(self._serial, "in_waiting", 0)
            chunk = self._serial.read(waiting or 1)
        except (SerialException, OSError) as exc:
            self.last_error = f"Failed to read speech module on {self._selected_port}: {exc}"
            self._disconnect_locked()
            return False

        if chunk:
            self._buffer.extend(chunk)
            self.last_error = ""
        return bool(chunk)

    def _pop_frame_locked(self):
        while len(self._buffer) >= 2:
            start = self._buffer.find(self.FRAME_HEAD)
            if start < 0:
                self._buffer.clear()
                return None
            if start > 0:
                del self._buffer[:start]
            if len(self._buffer) < self.FRAME_SIZE:
                return None
            if self._buffer[self.FRAME_SIZE - 1] != self.FRAME_END:
                del self._buffer[0]
                continue

            function_id = self._buffer[2]
            command_id = self._buffer[3]
            del self._buffer[:self.FRAME_SIZE]
            return function_id, command_id
        return None

    def _disconnect_locked(self):
        if self._serial is not None:
            try:
                self._serial.close()
            except (SerialException, OSError):
                pass
        self._serial = None
        self._selected_port = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
