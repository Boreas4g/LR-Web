# arm_aux_devices

机械臂辅助设备 ROS 2 功能包，包含转盘节点 `Turntable` 和升降台节点 `lifttable`。

## 依赖

- ROS 2
- `rclpy`
- `std_msgs`
- `launch_ros`
- `ament_index_python`
- `python3-serial` 或 `pyserial`

安装串口依赖：

```bash
sudo apt install python3-serial
```

## 编译

在工作空间根目录执行：

```bash
colcon build --packages-select arm_aux_devices
source install/setup.bash
```

## 启动方式

启动转盘：

```bash
ros2 launch arm_aux_devices turntable.launch.py
```

启动升降台：

```bash
ros2 launch arm_aux_devices lifttable.launch.py
```

直接运行节点：

```bash
ros2 run arm_aux_devices Turntable
ros2 run arm_aux_devices lifttable
```

## 接口

### Turntable

订阅：

| 话题 | 类型 | 说明 |
| --- | --- | --- |
| `/Turntable/position` | `std_msgs/msg/Float64` | 目标位置，单位 rad。 |
| `/Turntable/command` | `std_msgs/msg/Float64MultiArray` | 完整命令 `[position, kp, velocity, kd]`。 |
| `/Turntable/relax` | `std_msgs/msg/Empty` | 发送 `0.000,0.000,0.000,0.000`。 |

发布：

| 话题 | 类型 | 说明 |
| --- | --- | --- |
| `/Turntable/last_command` | `std_msgs/msg/String` | 最近一次写入串口的命令。 |
| `/Turntable/serial_line` | `std_msgs/msg/String` | 控制板串口返回行。 |

### lifttable

订阅：

| 话题 | 类型 | 说明 |
| --- | --- | --- |
| `/lifttable/move_pulses` | `std_msgs/msg/Int32` | 发送一次增量脉冲，正负号决定方向。 |
| `/lifttable/direction` | `std_msgs/msg/Int8` | 连续运动方向：`1` 正向，`-1` 反向，`0` 停止。 |
| `/lifttable/stop` | `std_msgs/msg/Empty` | 停止升降台。 |

发布：

| 话题 | 类型 | 说明 |
| --- | --- | --- |
| `/lifttable/response` | `std_msgs/msg/String` | 升降台 Modbus 回复帧，十六进制字符串。 |

示例：

```bash
ros2 topic pub --once /lifttable/move_pulses std_msgs/msg/Int32 "{data: 500}"
ros2 topic pub --once /lifttable/direction std_msgs/msg/Int8 "{data: 1}"
ros2 topic pub --once /lifttable/stop std_msgs/msg/Empty "{}"
```

## 参数

### Turntable

默认配置文件：`config/turntable.yaml`

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `port` | `/dev/ttyACM2` | 转盘控制板串口。 |
| `baudrate` | `115200` | 串口波特率。 |
| `timeout` | `0.02` | 读超时，单位秒。 |
| `write_timeout` | `1.0` | 写超时，单位秒。 |
| `open_delay_s` | `2.0` | 打开串口后的等待时间。 |
| `send_hz` | `20.0` | 连续发送频率。 |
| `continuous_send` | `false` | 是否持续发送最近一次命令。 |
| `default_position` | `0.0` | 默认目标位置。 |
| `default_kp` | `2.0` | 默认位置增益。 |
| `default_velocity` | `0.0` | 默认速度。 |
| `default_kd` | `0.1` | 默认速度增益。 |

### lifttable

默认配置文件：`config/lifttable.yaml`

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `port` | `/dev/lift_port` | 升降台控制器串口。 |
| `baudrate` | `19200` | 串口波特率。 |
| `slave_id` | `1` | Modbus 从站地址。 |
| `timeout` | `0.1` | 读超时，单位秒。 |
| `speed_rpm` | `2000` | 目标速度。 |
| `accel` | `10000` | 加速度。 |
| `pulse_per_move` | `500` | 连续运动时每次发送的脉冲数。 |
| `send_interval_s` | `0.01` | 连续运动发送间隔，单位秒。 |
