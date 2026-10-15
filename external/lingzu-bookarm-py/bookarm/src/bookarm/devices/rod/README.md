# 推书杆电机 SDK

本目录提供 `RodMotorService`，用于通过 JSON 串口控制推书杆电机。

## 功能

- 以配置的串口、波特率连接和断开电机。
- 调用 `move_to_deg()` 将推书杆移动至指定角度，并传入速度、加速度、到位容差和超时时间。
- 运动命令发送后持续读取电机角度反馈；仅在实际角度进入容差范围时返回成功。
- 目标角度限制为 `-100` 到 `150` 度（包含端点）；超出范围时不会向电机发送命令。
- 串口操作由独立命令队列串行执行，避免并发读写。

## 基本用法

```python
from bookarm.devices.rod import RodDeviceConfig, RodMotorService

rod = RodMotorService(RodDeviceConfig(port="/dev/rodmotor"))
rod.start()
try:
    rod.move_to_deg(0.0, speed=1000, acceleration=50, tolerance_deg=1.5, timeout_s=12.0)
finally:
    rod.stop()
```

实机测试脚本位于 `bookarm/scripts/rod_move.py`。不传 `--angles` 时只验证连接；传入的每个角度必须在安全范围内。
