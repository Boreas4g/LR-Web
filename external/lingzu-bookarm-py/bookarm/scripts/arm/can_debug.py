#!/usr/bin/env python3
"""CAN 总线诊断工具：定位 can0 的电气/波特率/节点问题。

用法：
    python3 scripts/arm/can_debug.py [--iface can0]

按顺序执行：
  1. 适配器存在性检查（USB CANable 1d50:606f）
  2. 接口状态 + 错误计数 + 3 秒错误风暴速率
  3. 被动监听 2 秒：总线上有没有任何流量、谁在说话
  4. 波特率扫描（1M/500k/250k/125k）：逐档发机械臂反馈请求，统计应答
  5. 输出诊断结论与物理排查建议
"""

from __future__ import annotations

import argparse
import re
import socket
import struct
import subprocess
import sys
import time

CANABLE_IDS = ("1d50:606f", "16c0:05dd", "0483:5740")
BITRATES = (1_000_000, 500_000, 250_000, 125_000)
HOST_ID = 0xFD
COMM_TYPE_SET_AUTO_REPORT = 24
COMM_TYPE_FEEDBACK = 2


def run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def section(title: str) -> None:
    print("\n=== %s ===" % title)


def check_adapter() -> bool:
    section("1. USB CAN 适配器")
    _rc, out = run(["lsusb"])
    found = [line for line in out.splitlines() if any(vid in line for vid in CANABLE_IDS)]
    if found:
        print("[OK] 找到适配器：%s" % found[0])
        return True
    print("[FAIL] 未找到 USB CAN 适配器（期望厂商 ID: %s）" % "/".join(CANABLE_IDS))
    print("       → 检查适配器供电与 USB 线，或适配器已在事故中损坏。")
    return False


def iface_state(iface: str) -> dict:
    _rc, out = run(["ip", "-details", "link", "show", iface])
    state = {"exists": "does not exist" not in out and out != ""}
    state["up"] = "state UP" in out
    match = re.search(r"can state ([A-Z-]+)", out)
    state["can_state"] = match.group(1) if match else "?"
    match = re.search(r"bitrate (\d+)", out)
    state["bitrate"] = int(match.group(1)) if match else None
    return state


def error_counters(iface: str) -> dict:
    _rc, out = run(["ip", "-details", "-statistics", "link", "show", iface])
    match = re.search(
        r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+numtxqueues",
        out,
    )
    if not match:
        return {}
    keys = ("restarted", "bus_errors", "arbit_lost", "error_warn", "error_pass", "bus_off")
    return {key: int(value) for key, value in zip(keys, match.groups())}


def link_config(iface: str, *, up: bool, bitrate: int | None = None) -> None:
    if up:
        cmd = ["sudo", "-n", "ip", "link", "set", iface, "up", "type", "can"]
        cmd += ["bitrate", str(bitrate) if bitrate else "1000000", "restart-ms", "0"]
    else:
        cmd = ["sudo", "-n", "ip", "link", "set", iface, "down"]
    rc, out = run(cmd)
    if rc != 0:
        raise RuntimeError("ip 配置失败（需 sudo 免密）：%s" % out)


def open_can_socket(iface: str) -> socket.socket:
    sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    sock.bind((iface,))
    sock.setblocking(False)
    return sock


def listen(sock: socket.socket, seconds: float) -> list[tuple[int, bytes]]:
    frames: list[tuple[int, bytes]] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            data = sock.recv(16)
        except BlockingIOError:
            time.sleep(0.005)
            continue
        except OSError:
            break
        can_id, payload = struct.unpack("=IB3x8s", data[:16])
        frames.append((can_id & socket.CAN_EFF_MASK, payload))
    return frames


def send_frame(sock: socket.socket, can_id: int, payload: bytes) -> None:
    data = payload.ljust(8, b"\x00")[:8]
    frame = struct.pack("=IB3x8s", can_id | socket.CAN_EFF_FLAG, len(data), data)
    sock.send(frame)


def enable_feedback_frame_id(motor_id: int) -> int:
    return (COMM_TYPE_SET_AUTO_REPORT << 24) | (HOST_ID << 8) | motor_id


def section2_state(iface: str) -> bool:
    section("2. 接口状态与错误风暴")
    state = iface_state(iface)
    if not state["exists"]:
        print("[FAIL] 接口 %s 不存在（驱动未加载？）" % iface)
        return False
    print("接口: up=%s  can_state=%s  bitrate=%s" % (state["up"], state["can_state"], state["bitrate"]))
    before = error_counters(iface)
    time.sleep(3.0)
    after = error_counters(iface)
    for key in ("error_pass", "bus_off", "bus_errors"):
        delta = after.get(key, 0) - before.get(key, 0)
        rate = delta / 3.0
        mark = "（风暴！）" if rate > 1 else ""
        print("计数器 %-11s +%d（%.0f/s）%s" % (key, delta, rate, mark))
    storm = (after.get("error_pass", 0) - before.get("error_pass", 0)) > 1
    return not storm


def section3_listen(iface: str) -> None:
    section("3. 被动监听 2 秒（总线流量）")
    try:
        sock = open_can_socket(iface)
    except OSError as error:
        print("[FAIL] 无法打开 %s：%s" % (iface, error))
        return
    frames = listen(sock, 2.0)
    sock.close()
    if not frames:
        print("总线安静：没有任何帧（电机未上电、线未接通，或波特率全错）")
        return
    print("收到 %d 帧，前 10 帧：" % len(frames))
    for can_id, payload in frames[:10]:
        comm = (can_id >> 24) & 0x1F
        motor = can_id & 0xFF
        print("  id=0x%08X 类型位=%d 电机域=%d 数据=%s" % (can_id, comm, motor, payload.hex(" ")))


def section4_sweep(iface: str) -> list[int]:
    section("4. 波特率扫描（机械臂反馈探测）")
    answering: list[int] = []
    for bitrate in BITRATES:
        try:
            link_config(iface, up=False)
            link_config(iface, up=True, bitrate=bitrate)
        except RuntimeError as error:
            print("  %8d bps: 配置失败 %s" % (bitrate, error))
            continue
        try:
            sock = open_can_socket(iface)
        except OSError as error:
            print("  %8d bps: 打开失败 %s" % (bitrate, error))
            continue
        listen(sock, 0.3)  # flush
        try:
            for motor_id in range(1, 8):
                send_frame(sock, enable_feedback_frame_id(motor_id), b"\x01")
        except OSError:
            pass
        frames = listen(sock, 1.0)
        sock.close()
        responses = [frame for frame in frames if ((frame[0] >> 24) & 0x1F) == COMM_TYPE_FEEDBACK]
        motors = sorted({frame[0] & 0xFF for frame in responses})
        if responses:
            answering.append(bitrate)
            print("  %8d bps: ★ 有应答（%d 帧，电机 %s）" % (bitrate, len(responses), motors))
        else:
            state = iface_state(iface)
            print("  %8d bps: 无应答（can_state=%s）" % (bitrate, state["can_state"]))
    return answering


def main() -> int:
    parser = argparse.ArgumentParser(description="CAN 总线诊断工具")
    parser.add_argument("--iface", default="can0", help="CAN 接口名（默认 can0）")
    args = parser.parse_args()
    iface = args.iface

    adapter_ok = check_adapter()
    if not adapter_ok:
        return 1
    if not section2_state(iface):
        print("\n提示：错误风暴通常是 终端电阻缺失/短路/波特率不一致 导致。")
    section3_listen(iface)
    answering = section4_sweep(iface)

    section("5. 诊断结论")
    if answering:
        print("机械臂在以下波特率有应答：%s" % ", ".join(str(rate) for rate in answering))
        if 1_000_000 not in answering:
            print("★ 关键发现：电机波特率不是 1Mbps！软件侧应改用应答档位，或用厂家工具把电机改回 1Mbps。")
        else:
            print("总线电气与波特率正常，问题在别处（检查上层协议/服务占用）。")
    else:
        print("所有波特率档位均无应答。按概率排查：")
        print("  1) 机械臂电源是否打开（电机未上电=总线静默）；")
        print("  2) CAN H/L 是否接反或短接（断电测 H-L 电阻，正常约 60Ω）；")
        print("  3) 终端电阻：总线两端各 120Ω；")
        print("  4) 线缆/接头损坏（短路事故可能已伤线）；")
        print("  5) 若监听阶段看到异常流量：有其他节点以错误波特率占用总线。")
    return 0 if answering else 2


if __name__ == "__main__":
    sys.exit(main())
