# bookarm

`bookarm` is an independent direct-control project for book-arm workflows.
It owns the EL-A3 CAN/SLCAN connection, D435 camera pipeline, YZ-AIM lift,
and rod-motor serial port; it does not import or start the original GUI or
WebUI services.

## Layout

```text
src/bookarm/
  api/             public SDK and request/result contracts
  application/     task engine and workflow use cases
  domain/          business models and safety policies
  ports/           hardware and persistence interfaces
  runtime/         device lifecycle, exclusive reservation, command workers
  devices/         direct EL-A3, D435, YZ-AIM and rod-motor implementations
  infrastructure/  independent configuration and composition root
```/home/boreas/project/lingzu_arm/lingzu-bookarm-py/scripts/setup_can.sh

`application`, `domain`, and `ports` do not import hardware SDKs, Qt, FastAPI,
the original GUI, or WebUI. Direct device transports exist only in `devices/`.

## Public API

```python
from pathlib import Path

from bookarm.api import TakeBookRequest
from bookarm.infrastructure import create_bookarm_runtime

runtime = create_bookarm_runtime(
    book_stand_slots_path=Path("config/calibration/book_stand_slots.json"),
    camera_calibration_path=Path("config/calibration/camera_to_base.json"),
)
with runtime:
    task = runtime.sdk.take_book(TakeBookRequest(...))
    result = runtime.sdk.wait(task.id, timeout_s=180)
```

`return_book()` and `return_book_to_end()` are intentionally registered as
planned use cases until their motion requirements and calibration values are
provided. They return an explicit unsupported result instead of silently
executing an incomplete workflow.

## Direct Device Stack

`devices/arm/` owns a dedicated command worker and direct SocketCAN or SLCAN
transport. It includes feedback freshness checks, six-joint arrival checks,
MoveJ interpolation, optional Pinocchio IK for MoveL/pose motion, gripper
control, and an out-of-band communication emergency stop.

`devices/camera/` owns the D435 pipeline, RGB-D capture, point cloud creation,
OpenCV book-spine homography matching, depth deprojection, and calibrated
camera-to-base conversion. Its default camera serial is `262322073961`.

`devices/lift/` imports the separately installed `aim_sdk` distribution. It
uses `AimDrive` and `LiftPlatform` for absolute height control and feedback. The public
`move_to_height_mm()` uses the same `LiftPlatform.move_to_height()` operation
as `scripts/lift/lift_move.py move-height`: it enables Modbus/output as needed,
checks the drive is ready, applies speed level 1--3, commands an absolute
height, and waits with the SDK defaults of 45 seconds and 10 pulses. Workflow
values remain millimetres; the supplied SDK receives centimetres and accepts
calibrated heights from 10--1700 mm.

`devices/rod/` owns the rod-motor serial JSON protocol and validates final
angle feedback. The SDK remains synchronous in this initial delivery:
`take_book()` returns a terminal task, while `wait()` remains stable for a
future queued executor. `cancel()` only applies at a stage boundary; an active
motion requires the runtime's physical safety process / emergency stop.

## Configuration

`config/calibration/` contains version-controlled templates for the camera,
arm resources, and book-stand slots. Populate all three slot plans with
validated base-frame poses before enabling a real placement workflow.

Copy `config/calibration/book_stand_slots.example.json` to a deployment-only
file and replace every `null` value with a verified `[x_m, y_m, z_m, rx_rad,
ry_rad, rz_rad]` pose. The factory rejects incomplete calibration before it
commands the lift or arm. `config/profiles/take_book.example.json` exposes the
GUI-derived take-book motion values without coupling SDK code to a widget.

Copy `camera_to_base.example.json` to `camera_to_base.json` and validate it
with a physical calibration procedure before allowing vision-guided motion.
The supplied EL-A3 URDF is packaged under `src/bookarm/resources/urdf/`; use
`pip install -e '.[kinematics]'` when Cartesian MoveL or pose IK is required.

## Installation

Install the hardware SDK first (from its own checkout or package index), then
install this project with its optional hardware dependencies.  This avoids a
machine-specific absolute path in the package metadata:

```bash
python -m pip install -e /path/to/aim_sdk
cd bookarm
python -m pip install -e ".[hardware,kinematics,service]"
```

## Supervised HTTP service

For a production control station, install the service once.  It listens only
on `/run/lr-bookarm/bookarm.sock`; the Web container proxies that local socket
and no BookArm hardware endpoint is published to the LAN.

```bash
sudo bash scripts/install-takeout-service.sh
# After checking the real hardware is safe:
sudo systemctl enable --now lr-bookarm-takeout.service
```

`LR-Web/scripts/start-all.sh` will recognize a running service but refuses to
start an offline BookArm daemon automatically. After an on-site CAN, joint,
camera and emergency-stop inspection, use
`LR_BOOKARM_ALLOW_START=true bash scripts/start-all.sh` for that explicit
hardware startup. It deliberately recognizes only the managed Unix socket:
an older loopback TCP service on `127.0.0.1:8090` is reported as a conflict and
must be stopped and migrated by an on-site operator before the new service is
started. This prevents two independent processes from owning the same CAN,
camera and serial devices.

The service journal is `recordings/runtime/bookarm-tasks.json`.  After a
process restart, queued or running tasks are retained and reported as
`recovery_required`; inspect the physical robot before clearing or replacing
such a task.  Once an operator has taken over manually, the control station
confirms it through `POST /api/v1/tasks/{task_id}/dismiss-recovery`: the
endpoint answers 200 and erases the record from the journal (dismissal is a
removal, not a new state, so a later GET on that id answers 404).  A task in
any other state is refused with 409 — live or honestly finished tasks stay
auditable and can never be erased through this endpoint.

Run the hardware-free regression suite with its explicit test dependencies:

```bash
python -m pip install -e '.[test]'
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/ -q
```

Before physical use, configure CAN or SLCAN, RealSense permissions, the AIM
RS485 port, and the rod-motor port. Never treat a successful software startup
as a substitute for the external emergency-stop circuit and low-speed hardware
acceptance procedure.

For the lift, copy `config/devices/lift.example.json` to a deployment-only
configuration and set its RS485 connection parameters.

## setup can

```bash
sudo bash scripts/arm/setup_can.sh can0 1000000
```


## Lift commissioning

`scripts/lift_move.py` provides direct, real-hardware tests for the public AIM
single-axis and lift APIs. It defaults to a read-only status request:

```bash
python bookarm/scripts/lift_move.py --port /dev/lift_port
```

Every command that can move the lift, enable output, change drive configuration,
home, clear coordinates, or persist settings requires `--confirm`. Begin with a
low-speed, known-safe target only after validating the external E-stop and
physical travel:

```bash
python bookarm/scripts/lift_move.py --port /dev/lift_port move-height \
  --height-mm 800 --speed-level 1 --timeout-s 45 --confirm
```

Use `python bookarm/scripts/lift_move.py --help` to see individual commands.
Coordinate clearing, homing, parameter persistence, raw register writes, and
baud-rate preparation are deliberately individual commands rather than an
automatic test sequence, because they alter calibration or device state.

Use `scripts/lift_control.py` when only connection, state, enable, or disable
control is required; it never sends a position command:

```bash
python bookarm/scripts/lift_control.py --port /dev/lift_port status
python bookarm/scripts/lift_control.py --port /dev/lift_port enable --confirm
python bookarm/scripts/lift_control.py --port /dev/lift_port disable --confirm
```
