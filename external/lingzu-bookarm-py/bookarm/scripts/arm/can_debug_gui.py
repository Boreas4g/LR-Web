#!/usr/bin/env python3
"""CAN 总线可视化诊断工具（Tkinter GUI）。

用法：python3 scripts/arm/can_debug_gui.py
功能与 can_debug.py 相同：适配器/接口/错误风暴检查、被动流量监听、
波特率扫描、机械臂反馈探测，全部结果可视化展示。
"""

from __future__ import annotations

import queue
import re
import socket
import struct
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk

CANABLE_IDS = ("1d50:606f", "16c0:05dd", "0483:5740")
BITRATES = (1_000_000, 500_000, 250_000, 125_000)
HOST_ID = 0xFD
COMM_TYPE_SET_AUTO_REPORT = 24
COMM_TYPE_FEEDBACK = 2
IFACE = "can0"


def run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return (proc.stdout + proc.stderr).strip()


def iface_state(iface: str) -> dict:
    out = run(["ip", "-details", "link", "show", iface])
    match = re.search(r"can state ([A-Z-]+)", out)
    bitrate = re.search(r"bitrate (\d+)", out)
    return {
        "exists": bool(out) and "does not exist" not in out,
        "up": "state UP" in out,
        "can_state": match.group(1) if match else "?",
        "bitrate": int(bitrate.group(1)) if bitrate else None,
    }


def error_counters(iface: str) -> dict:
    out = run(["ip", "-details", "-statistics", "link", "show", iface])
    match = re.search(r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+numtxqueues", out)
    if not match:
        return {}
    keys = ("restarted", "bus_errors", "arbit_lost", "error_warn", "error_pass", "bus_off")
    return {key: int(value) for key, value in zip(keys, match.groups())}


def link_config(iface: str, *, up: bool, bitrate: int | None = None) -> None:
    if up:
        cmd = ["sudo", "-n", "ip", "link", "set", iface, "up", "type", "can"]
        cmd += ["bitrate", str(bitrate if bitrate else 1_000_000), "restart-ms", "0"]
    else:
        cmd = ["sudo", "-n", "ip", "link", "set", iface, "down"]
    result = run(cmd)
    if "Error" in result or "error" in result.lower():
        raise RuntimeError(result)


def open_can_socket(iface: str) -> socket.socket:
    sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    sock.bind((iface,))
    sock.setblocking(False)
    return sock


def listen(sock: socket.socket, seconds: float, stop: threading.Event | None = None,
           on_frame=None) -> list[tuple[int, bytes]]:
    frames: list[tuple[int, bytes]] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not (stop and stop.is_set()):
        try:
            data = sock.recv(16)
        except BlockingIOError:
            time.sleep(0.005)
            continue
        except OSError:
            break
        can_id, payload = struct.unpack("=IB3x8s", data[:16])
        frame = (can_id & socket.CAN_EFF_MASK, payload)
        frames.append(frame)
        if on_frame:
            on_frame(frame)
    return frames


def send_frame(sock: socket.socket, can_id: int, payload: bytes) -> None:
    data = payload.ljust(8, b"\x00")[:8]
    sock.send(struct.pack("=IB3x8s", can_id | socket.CAN_EFF_FLAG, len(data), data))


class CanDebugApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("CAN 总线诊断工具")
        self.root.geometry("980x640")
        self.queue: queue.Queue = queue.Queue()
        self.stop_flag = threading.Event()

        top = ttk.Frame(root, padding=8)
        top.pack(fill=tk.X)
        self.adapter_var = tk.StringVar(value="适配器：检查中…")
        self.iface_var = tk.StringVar(value="接口：—")
        self.storm_var = tk.StringVar(value="错误风暴：—")
        for var in (self.adapter_var, self.iface_var, self.storm_var):
            label = ttk.Label(top, textvariable=var, font=("monospace", 11, "bold"))
            label.pack(side=tk.LEFT, padx=12)

        btns = ttk.Frame(root, padding=(8, 0))
        btns.pack(fill=tk.X)
        for text, cmd in (
            ("① 全面诊断", self.full_diagnosis),
            ("② 被动监听 2s", self.passive_listen),
            ("③ 波特率扫描", self.bitrate_sweep),
            ("④ 机械臂反馈探测", self.arm_probe),
            ("停止", self.stop_tasks),
            ("清空日志", self.clear_log),
        ):
            ttk.Button(btns, text=text, command=cmd).pack(side=tk.LEFT, padx=4, pady=4)

        middle = ttk.PanedWindow(root, orient=tk.VERTICAL)
        middle.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        frames_box = ttk.Frame(middle)
        columns = ("time", "id", "type", "motor", "data")
        self.tree = ttk.Treeview(frames_box, columns=columns, show="headings", height=12)
        for col, title, width in (
            ("time", "时间", 90), ("id", "帧 ID", 110),
            ("type", "类型位", 60), ("motor", "电机域", 60), ("data", "数据", 320),
        ):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, anchor=tk.W)
        scrollbar = ttk.Scrollbar(frames_box, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        middle.add(frames_box)

        log_box = ttk.Frame(middle)
        self.log = tk.Text(log_box, height=12, font=("monospace", 10), state=tk.DISABLED)
        log_scroll = ttk.Scrollbar(log_box, orient=tk.VERTICAL, command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        middle.add(log_box)

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W).pack(
            fill=tk.X, side=tk.BOTTOM,
        )

        self.root.after(100, self._poll_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._start(self._check_adapter)

    # ---------- 后台任务编排 ----------

    def _start(self, fn, *args) -> None:
        self.stop_flag.clear()
        thread = threading.Thread(target=fn, args=args, daemon=True)
        thread.start()

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self.log.configure(state=tk.NORMAL)
                    self.log.insert(tk.END, payload + "\n")
                    self.log.see(tk.END)
                    self.log.configure(state=tk.DISABLED)
                elif kind == "frame":
                    can_id, payload = payload
                    timestamp = time.strftime("%H:%M:%S")
                    self.tree.insert(
                        "", tk.END,
                        values=(timestamp, "0x%08X" % can_id,
                                (can_id >> 24) & 0x1F, can_id & 0xFF, payload.hex(" ")),
                    )
                    self.tree.yview_moveto(1.0)
                elif kind == "adapter":
                    self.adapter_var.set(payload)
                elif kind == "iface":
                    self.iface_var.set(payload)
                elif kind == "storm":
                    self.storm_var.set(payload)
                elif kind == "status":
                    self.status_var.set(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _log(self, text: str) -> None:
        self.queue.put(("log", text))

    def _on_close(self) -> None:
        self.stop_flag.set()
        self.root.destroy()

    # ---------- 诊断步骤 ----------

    def _check_adapter(self) -> None:
        out = run(["lsusb"])
        found = [line for line in out.splitlines() if any(vid in line for vid in CANABLE_IDS)]
        if found:
            self.queue.put(("adapter", "适配器：在线 ✔"))
            self._log("[OK] CANable 在线：%s" % found[0])
        else:
            self.queue.put(("adapter", "适配器：未找到 ✘"))
            self._log("[FAIL] 未找到 USB CAN 适配器，请检查供电/USB 线。")
        self._refresh_iface()

    def _refresh_iface(self) -> None:
        state = iface_state(IFACE)
        self.queue.put((
            "iface",
            "接口：%s  can_state=%s  bitrate=%s"
            % ("UP" if state["up"] else "DOWN", state["can_state"], state["bitrate"]),
        ))
        before = error_counters(IFACE)
        time.sleep(2.0)
        after = error_counters(IFACE)
        delta = after.get("error_pass", 0) - before.get("error_pass", 0)
        storming = delta > 1
        self.queue.put((
            "storm",
            "错误风暴：%s（%.0f/s）" % ("进行中 ⚠" if storming else "无", delta / 2.0),
        ))

    def _probe_common(self) -> socket.socket | None:
        try:
            link_config(IFACE, up=True, bitrate=1_000_000)
        except RuntimeError as error:
            self._log("[FAIL] 接口上线失败：%s" % error)
            return None
        try:
            return open_can_socket(IFACE)
        except OSError as error:
            self._log("[FAIL] 打开接口失败：%s" % error)
            return None

    def full_diagnosis(self) -> None:
        self._log("—— 开始全面诊断 ——")
        self._start(self._full_diagnosis_worker)

    def _full_diagnosis_worker(self) -> None:
        self._check_adapter()
        self._passive_listen_worker()
        self._sweep_worker()

    def passive_listen(self) -> None:
        self._start(self._passive_listen_worker)

    def _passive_listen_worker(self) -> None:
        self.queue.put(("status", "被动监听中…"))
        sock = self._probe_common()
        if sock is None:
            return
        frames = listen(sock, 2.0, self.stop_flag,
                        on_frame=lambda frame: self.queue.put(("frame", frame)))
        sock.close()
        if frames:
            self._log("[流量] 2 秒收到 %d 帧。" % len(frames))
        else:
            self._log("[流量] 总线安静：无流量（电机未上电 / 线路断开 / 波特率全错）。")
        self.queue.put(("status", "就绪"))

    def bitrate_sweep(self) -> None:
        self._start(self._sweep_worker)

    def _sweep_worker(self) -> list[int]:
        answering: list[int] = []
        for bitrate in BITRATES:
            if self.stop_flag.is_set():
                break
            self.queue.put(("status", "扫描 %d bps…" % bitrate))
            try:
                link_config(IFACE, up=False)
                link_config(IFACE, up=True, bitrate=bitrate)
                sock = open_can_socket(IFACE)
            except (OSError, RuntimeError) as error:
                self._log("[扫描] %d bps 配置失败：%s" % (bitrate, error))
                continue
            listen(sock, 0.3)
            try:
                for motor_id in range(1, 8):
                    send_frame(sock, (COMM_TYPE_SET_AUTO_REPORT << 24) | (HOST_ID << 8) | motor_id, b"\x01")
            except OSError:
                pass
            frames = listen(sock, 1.0, self.stop_flag,
                            on_frame=lambda frame: self.queue.put(("frame", frame)))
            sock.close()
            responses = [f for f in frames if ((f[0] >> 24) & 0x1F) == COMM_TYPE_FEEDBACK]
            motors = sorted({f[0] & 0xFF for f in responses})
            if responses:
                answering.append(bitrate)
                self._log("[扫描] %d bps ★ 应答 %d 帧，电机 %s" % (bitrate, len(responses), motors))
            else:
                self._log("[扫描] %d bps 无应答。" % bitrate)
        if answering:
            self._log("[结论] 有应答的波特率：%s" % answering)
            if 1_000_000 not in answering:
                self._log("[结论] ★ 电机波特率不是 1Mbps，请按应答档位调整软件或用厂家工具改回 1Mbps。")
        else:
            self._log("[结论] 全档位无应答 → 电机未上电 / CAN 物理断开（H-L 开路或短路）。")
        self.queue.put(("status", "就绪"))
        return answering

    def arm_probe(self) -> None:
        self._start(self._arm_probe_worker)

    def _arm_probe_worker(self) -> None:
        self.queue.put(("status", "机械臂反馈探测中…"))
        sock = self._probe_common()
        if sock is None:
            return
        listen(sock, 0.3)
        try:
            for motor_id in range(1, 8):
                send_frame(sock, (COMM_TYPE_SET_AUTO_REPORT << 24) | (HOST_ID << 8) | motor_id, b"\x01")
        except OSError as error:
            self._log("[探测] 发送失败：%s" % error)
        frames = listen(sock, 1.5, self.stop_flag,
                        on_frame=lambda frame: self.queue.put(("frame", frame)))
        sock.close()
        responses = [f for f in frames if ((f[0] >> 24) & 0x1F) == COMM_TYPE_FEEDBACK]
        motors = sorted({f[0] & 0xFF for f in responses})
        if motors:
            self._log("[探测] 在线电机：%s / 7" % motors)
        else:
            self._log("[探测] 0/7 电机应答——机械臂未上电或 CAN 线路故障。")
        self._refresh_iface()

    def stop_tasks(self) -> None:
        self.stop_flag.set()
        self.queue.put(("status", "已请求停止"))

    def clear_log(self) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        self.log.configure(state=tk.DISABLED)
        for item in self.tree.get_children():
            self.tree.delete(item)


def main() -> None:
    root = tk.Tk()
    CanDebugApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
