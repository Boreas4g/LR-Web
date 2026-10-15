# mobile_robot_voice_interaction

语音交互功能包，面向 Ubuntu 24.04 + ROS 2 Jazzy，适配命令词型串口语音模块。

这个包不做自由语音识别，也不做任意文本转语音。
它的工作方式是：

- 模块听到预置命令词后，返回一个命令码
- ROS 节点把命令码转换成话题或信号
- ROS 节点也可以把命令码发回模块，让模块播放对应的预置语音

适合做两类交互：

- 听到什么话，发什么信号
- 收到什么信号，说什么话

## 包内容

- `mobile_robot_voice_interaction/speech_lib.py`
  串口语音模块底层读写封装，负责自动找口、收包和发包。
- `mobile_robot_voice_interaction/voice_cmd_reader.py`
  只读命令码，发布原始识别结果，适合先接线调试。
- `mobile_robot_voice_interaction/speech_interaction.py`
  通用交互桥，支持语音命令转信号、信号转语音命令。
- `launch/speech_interaction.launch.py`
  带示例参数文件的启动入口。
- `config/speech_interaction_example.yaml`
  规则映射示例。

## 前提

- 已安装 ROS 2 Jazzy
- 已安装 `python3-serial`
- 语音模块已经通过 USB 串口接到主机
- 推荐通过 udev 绑定稳定设备名 `/dev/speech_port`
- 如果还没有绑定稳定设备名，启动时显式传 `port:=/dev/ttyUSBx`

## 编译

在工作空间根目录执行：

```bash
cd /home/boreas/mobile_robot_ws
source /opt/ros/jazzy/setup.bash
colcon build --packages-select mobile_robot_voice_interaction --symlink-install
source install/setup.bash
```

如果你第一次在这台机器上使用这个仓库，也可以直接在工作空间根目录全量编译：

```bash
cd /home/boreas/mobile_robot_ws
source /opt/ros/jazzy/setup.bash
rosdep install --from-paths src --ignore-src -r -y
colcon build --symlink-install
source install/setup.bash
```

## 启动

只看语音模块识别结果：

```bash
ros2 run mobile_robot_voice_interaction voice_cmd_reader --ros-args -p port:=/dev/speech_port
```

启动语音交互桥：

```bash
ros2 launch mobile_robot_voice_interaction speech_interaction.launch.py port:=/dev/speech_port
```

如果系统已经有 `/dev/speech_port`，也可以不传 `port`：

```bash
ros2 launch mobile_robot_voice_interaction speech_interaction.launch.py
```

## 接口

`voice_cmd_reader` 发布：

- `/speech_cmd` `std_msgs/msg/Int32`
  原始命令码
- `/speech_cmd_name` `std_msgs/msg/String`
  命令码对应的标准名称

`speech_interaction` 发布：

- `/speech_cmd` `std_msgs/msg/Int32`
  原始命令码
- `/speech_cmd_name` `std_msgs/msg/String`
  标准名称
- `/speech_heard_signal` `std_msgs/msg/String`
  根据映射规则产生的业务信号

`speech_interaction` 订阅：

- `/speech_say_cmd` `std_msgs/msg/Int32`
  直接按命令码播报
- `/speech_say_name` `std_msgs/msg/String`
  按命令名称播报
- `/speech_say_signal` `std_msgs/msg/String`
  按业务信号播报

## 内置命令名

- `0`: `idle_or_stop`
- `2`: `stop`
- `4`: `go_ahead`
- `5`: `back_off`
- `6`: `turn_left`
- `7`: `turn_right`
- `10`: `close_light`
- `11`: `red_light`
- `12`: `green_light`
- `13`: `blue_light`
- `14`: `yellow_light`
- `15`: `water_lamps`
- `16`: `gradient_light`
- `17`: `breathing_light`
- `18`: `display_electricity`
- `19`: `goal_one`
- `20`: `goal_two`
- `21`: `goal_three`
- `32`: `goal_four`
- `33`: `goal_origin`

## 配置

示例配置文件：

- `config/speech_interaction_example.yaml`

主要参数：

- `port`
  串口设备名，默认优先尝试 `/dev/speech_port`
- `baudrate`
  串口波特率，默认 `115200`
- `poll_hz`
  轮询频率
- `dedupe_window_sec`
  去重窗口，避免模块短时间重复回报码
- `heard_signal_rules`
  语音命令到业务信号的映射
- `speak_signal_rules`
  业务信号到语音命令的映射

规则格式支持：

- `19=nav_goal_1`
- `goal_one=nav_goal_1`
- `nav_ready=goal_one`
- `nav_arrived:33`

## 使用示例

监听原始语音命令：

```bash
ros2 topic echo /speech_cmd
ros2 topic echo /speech_cmd_name
ros2 topic echo /speech_heard_signal
```

主动触发模块播报：

```bash
ros2 topic pub -1 /speech_say_cmd std_msgs/msg/Int32 "{data: 19}"
ros2 topic pub -1 /speech_say_name std_msgs/msg/String "{data: 'goal_one'}"
ros2 topic pub -1 /speech_say_signal std_msgs/msg/String "{data: 'nav_ready'}"
```

命令行直接传映射规则：

```bash
ros2 run mobile_robot_voice_interaction speech_interaction --ros-args \
  -p port:=/dev/speech_port \
  -p heard_signal_rules:="[19=nav_goal_1,20=nav_goal_2]" \
  -p speak_signal_rules:="[nav_ready=goal_one,nav_arrived=goal_origin]"
```

## 使用流程

1. 先确认语音模块串口设备名，推荐绑定成 `/dev/speech_port`。
2. 启动 `voice_cmd_reader`，先看命令码是否能稳定读出来。
3. 再启动 `speech_interaction`，把命令码映射到自己的业务信号。
4. 业务节点订阅 `/speech_heard_signal`，做导航、灯光或状态机控制。
5. 业务节点向 `/speech_say_signal`、`/speech_say_name` 或 `/speech_say_cmd` 发消息，让语音模块播报反馈。

## 验证

检查包是否被当前工作空间接管：

```bash
cd /home/boreas/mobile_robot_ws
source /opt/ros/jazzy/setup.bash
source install/setup.bash
ros2 pkg prefix mobile_robot_voice_interaction
```

检查可执行入口：

```bash
ros2 pkg executables mobile_robot_voice_interaction
```

如果启动后一直没有命令，优先检查：

- `port` 是否传对
- 用户是否在 `dialout` 组
- 模块是否已经完成预置命令词配置
- 是否存在多个 `ttyUSB` 设备导致接错串口
