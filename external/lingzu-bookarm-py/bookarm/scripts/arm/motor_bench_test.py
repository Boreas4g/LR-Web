"""Standalone single-motor bench test: enable one motor unloaded, hold position, log torque/temp.

Usage:
    python scripts/arm/motor_bench_test.py --motor-id 3 --duration 240

Stops with Ctrl-C or after --duration seconds; always disables the motor on exit.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

BOOKARM_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BOOKARM_ROOT / "src"))

from bookarm.devices.arm.transport import SocketCanTransport  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--motor-id", type=int, default=3)
    parser.add_argument("--duration", type=float, default=240.0)
    parser.add_argument("--kp", type=float, default=80.0)
    parser.add_argument("--kd", type=float, default=4.0)
    parser.add_argument("--rate", type=float, default=100.0)
    parser.add_argument("--interface", default="can0")
    args = parser.parse_args()

    mid = args.motor_id
    transport = SocketCanTransport(interface=args.interface)
    transport.connect()
    print(f"[bench] CAN {args.interface} connected, enabling motor {mid} ...")

    out_path = BOOKARM_ROOT / "recordings" / "arm_telemetry" / (
        f"motor{mid}_bench_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    target_pos = None
    t0 = time.monotonic()
    last_log = 0.0
    period = 1.0 / args.rate
    hold_started = None

    try:
        transport.enable_motor(mid)
        with out_path.open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["elapsed_s", "position_rad", "velocity_rad_s", "torque_nm", "temperature_c", "fault_code"])
            while True:
                now = time.monotonic()
                elapsed = now - t0
                if elapsed >= args.duration:
                    break

                fb = transport.feedback().get(mid)
                if target_pos is None and fb is not None:
                    target_pos = fb.position_rad
                    hold_started = elapsed
                    print(f"[bench] feedback ok, holding at {target_pos:.4f} rad")

                if target_pos is None:
                    # no feedback yet: send zero-stiffness command to solicit feedback
                    transport.send_motion(mid, 0.0, 0.0, 0.0, 0.0, 0.0)
                else:
                    transport.send_motion(mid, target_pos, 0.0, args.kp, args.kd, 0.0)

                if fb is not None and elapsed - last_log >= 1.0:
                    last_log = elapsed
                    writer.writerow([
                        f"{elapsed:.1f}", f"{fb.position_rad:.4f}", f"{fb.velocity_rad_s:.3f}",
                        f"{fb.torque_nm:.3f}", f"{fb.temperature_c:.1f}", fb.fault_code,
                    ])
                    fh.flush()
                    print(
                        f"[bench] t={elapsed:6.1f}s pos={fb.position_rad:+.4f} "
                        f"tq={fb.torque_nm:+.3f}Nm temp={fb.temperature_c:.1f}C fault={fb.fault_code}"
                    )

                time.sleep(period)
    except KeyboardInterrupt:
        print("\n[bench] interrupted")
    finally:
        try:
            transport.send_motion(mid, target_pos or 0.0, 0.0, 0.0, 0.0, 0.0)
            transport.disable_motor(mid)
        finally:
            transport.close()
        print(f"[bench] motor {mid} disabled, log: {out_path}")
        if hold_started is not None:
            print(f"[bench] held position for {time.monotonic() - t0 - hold_started:.0f}s")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
