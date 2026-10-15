# Library-robot

面向 Ubuntu 24.04 + ROS 2 Jazzy 的实机机器人工作空间。仓库根目录就是 ROS 2 工作空间根目录，源码包位于 `src/`。

控制站接入按书脊图片取书时，使用
[`docs/BOOKARM_CONTROL_STATION_INTERFACE.zh-CN.md`](docs/BOOKARM_CONTROL_STATION_INTERFACE.zh-CN.md)
中的 MQTT、mTLS 图片下载和高度换算契约。

## 包结构和简介

```text
Library-robot/
├── archive/                         # 历史归档代码，不参与 colcon 构建
├── maps/                            # 工作空间级地图文件
└── src/
    ├── arm_aux_devices/             # 转盘、升降台等机械臂辅助设备
    ├── car_ctrl/                    # DDSM 底盘控制、IMU 驱动、里程计
    ├── lslidar_driver/              # 雷神激光雷达驱动
    ├── lslidar_msgs/                # 雷神激光雷达消息定义
    ├── mobile_robot_cartographer_bringup/  # Cartographer 建图启动包
    ├── mobile_robot_voice_interaction/     # 串口语音模块交互包
    └── nav2_minimal_bringup/        # Nav2 导航、slam_toolbox 建图、整车启动
```

主要功能包：

| 功能包 | 简介 |
| --- | --- |
| `arm_aux_devices` | 转盘 `Turntable` 和升降台 `lifttable` ROS 2 节点。 |
| `car_ctrl` | 底盘串口控制、IMU 串口读取、轮速/IMU 里程计。 |
| `lslidar_driver` | 雷神激光雷达驱动，发布 `/scan`。 |
| `lslidar_msgs` | 雷神激光雷达驱动依赖的消息定义。 |
| `mobile_robot_cartographer_bringup` | Cartographer 实车建图和地图保存。 |
| `mobile_robot_voice_interaction` | 语音模块命令读取、命令码映射和播报触发。 |
| `mobile_robot_nav_bringup` | 位于 `src/nav2_minimal_bringup/`，提供 Nav2 导航、slam_toolbox 建图和整车一键启动。 |
| `robot_decision` | 巡航、定位初始化及 Edge Gateway；将中心 MQTT 指令安全转换为本机 Nav2 目标。 |

## 依赖和编译方式

基础依赖：

- Ubuntu 24.04
- ROS 2 Jazzy
- `python3-colcon-common-extensions`
- `python3-serial`
- `python3-paho-mqtt`
- `python3-yaml`
- `libpcap-dev`
- `libpcl-dev`
- `ros-jazzy-navigation2`
- `ros-jazzy-nav2-bringup`
- `ros-jazzy-slam-toolbox`
- `ros-jazzy-cartographer-ros`
- `ros-jazzy-rviz2`

安装常用依赖：

```bash
sudo apt update
sudo apt install -y \
  python3-colcon-common-extensions \
  python3-serial \
  python3-paho-mqtt \
  python3-yaml \
  libpcap-dev \
  libpcl-dev \
  ros-jazzy-navigation2 \
  ros-jazzy-nav2-bringup \
  ros-jazzy-slam-toolbox \
  ros-jazzy-cartographer-ros \
  ros-jazzy-rviz2 \
  ros-jazzy-tf2-ros \
  ros-jazzy-pcl-conversions \
  ros-jazzy-diagnostic-updater \
  ros-jazzy-opennav-docking
```

编译：

```bash
cd /path/to/Library_robot
source /opt/ros/jazzy/setup.bash
rosdep install --from-paths src --ignore-src -r -y
colcon build --symlink-install
source install/setup.bash
```

## 各功能包启动方式

整车导航一键启动：

```bash
ros2 launch mobile_robot_nav_bringup full_navigation.launch.py
```

只启动 Nav2 定位与导航：

```bash
ros2 launch mobile_robot_nav_bringup navigation.launch.py
```

启动 slam_toolbox 建图：

```bash
ros2 launch mobile_robot_nav_bringup slam.launch.py
```

保存 slam_toolbox 地图：

```bash
ros2 launch mobile_robot_nav_bringup save_map.launch.py map_name:=my_map
```

启动 Cartographer 建图：

```bash
ros2 launch mobile_robot_cartographer_bringup cartographer_mapping.launch.py
```

保存 Cartographer 地图：

```bash
ros2 launch mobile_robot_cartographer_bringup save_map.launch.py map_name:=cartographer_map
```

启动底盘、IMU 和里程计：

```bash
ros2 launch car_ctrl ddsm_hat_diff_drive.launch.py
```

启动激光雷达：

```bash
ros2 launch lslidar_driver lslidar_launch.py
```

启动语音交互：

```bash
ros2 launch mobile_robot_voice_interaction speech_interaction.launch.py
```

启动转盘：

```bash
ros2 launch arm_aux_devices turntable.launch.py
```

启动升降台：

```bash
ros2 launch arm_aux_devices lifttable.launch.py
```

启动中心控制 Edge Gateway（必须先启动定位和 Nav2）：

```bash
ros2 launch robot_decision edge_gateway.launch.py
```

真机 MQTT、地图白名单与安全联调说明见 [src/robot_decision/README.zh-CN.md](src/robot_decision/README.zh-CN.md)。
供车载电脑上的 Codex 执行实际接入时，先阅读
[docs/VEHICLE_CODEX_HANDOFF.zh-CN.md](docs/VEHICLE_CODEX_HANDOFF.zh-CN.md)；在
`Library_robot/` 启动 Codex 会自动读取其中引用的 `AGENTS.md` 安全约束。

导航和建图的详细说明见：

- [src/nav2_minimal_bringup/README.md](src/nav2_minimal_bringup/README.md)
