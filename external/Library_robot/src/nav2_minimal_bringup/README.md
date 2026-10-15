# mobile_robot_nav_bringup

Nav2 导航与 `slam_toolbox` 建图启动包。源码目录为 `src/nav2_minimal_bringup/`，ROS 2 包名为 `mobile_robot_nav_bringup`。

该包提供：

- 整车一键启动：底盘、IMU、里程计、雷达、定位和 Nav2
- 只启动 Nav2 定位与导航
- 启动 `slam_toolbox` 建图
- 保存当前 `/map`

## 包内容

```text
nav2_minimal_bringup/
├── config/
│   ├── nav2_params.yaml
│   └── slam_params.yaml
├── launch/
│   ├── full_navigation.launch.py
│   ├── navigation.launch.py
│   ├── slam.launch.py
│   └── save_map.launch.py
├── maps/
├── rviz/
└── scripts/
```

关键文件：

| 文件 | 说明 |
| --- | --- |
| `launch/full_navigation.launch.py` | 整车一键启动，包含底盘、IMU、里程计、雷达、定位和导航。 |
| `launch/navigation.launch.py` | 只启动 Nav2 定位与导航栈。 |
| `launch/slam.launch.py` | 启动 `slam_toolbox` 建图。 |
| `launch/save_map.launch.py` | 保存当前 `/map`。 |
| `config/nav2_params.yaml` | Nav2 参数。 |
| `config/slam_params.yaml` | slam_toolbox 参数。 |
| `maps/` | 包内地图目录。 |

## 依赖

- ROS 2 Jazzy
- `nav2_bringup`
- `navigation2`
- `slam_toolbox`
- `rviz2`
- `tf2_ros`
- 当前工作空间内的 `car_ctrl`、`lslidar_driver`

安装常用依赖：

```bash
sudo apt update
sudo apt install -y \
  ros-jazzy-navigation2 \
  ros-jazzy-nav2-bringup \
  ros-jazzy-slam-toolbox \
  ros-jazzy-rviz2 \
  ros-jazzy-tf2-ros \
  ros-jazzy-opennav-docking
```

## 编译

在工作空间根目录执行：

```bash
cd /home/boreas/Library_robot
source /opt/ros/jazzy/setup.bash
colcon build --packages-select mobile_robot_nav_bringup --symlink-install
source install/setup.bash
```

## 启动方式

整车导航一键启动：

```bash
ros2 launch mobile_robot_nav_bringup full_navigation.launch.py
```

同时打开 Nav2 RViz：

```bash
ros2 launch mobile_robot_nav_bringup full_navigation.launch.py use_nav_rviz:=true
```

只启动 Nav2 定位与导航栈，不启动底盘和雷达驱动：

```bash
ros2 launch mobile_robot_nav_bringup navigation.launch.py
```

只启动导航并打开 RViz：

```bash
ros2 launch mobile_robot_nav_bringup navigation.launch.py use_rviz:=true
```

显式指定地图：

```bash
ros2 launch mobile_robot_nav_bringup navigation.launch.py \
  map:=/absolute/path/to/map.yaml
```

启动 slam_toolbox 建图：

```bash
ros2 launch mobile_robot_nav_bringup slam.launch.py
```

保存地图：

```bash
ros2 launch mobile_robot_nav_bringup save_map.launch.py map_name:=my_map
```

显式指定保存目录：

```bash
ros2 launch mobile_robot_nav_bringup save_map.launch.py \
  save_dir:=/home/boreas/Library_robot/src/nav2_minimal_bringup/maps \
  map_name:=my_map
```

## 导航运行前提

运行导航前需要保证：

- `/scan` 正常发布
- `/odom` 正常发布
- TF 中存在 `odom -> base_link`
- 定位启动后在 RViz 中给出初始位姿
- 底盘能够接收 `/cmd_vel`

整车一键启动会默认启动 `car_ctrl` 和 `lslidar_driver`，只启动 `navigation.launch.py` 时需要外部已经提供上述话题和 TF。

## 建图流程

1. 启动建图：

```bash
ros2 launch mobile_robot_nav_bringup slam.launch.py
```

2. 遥控机器人缓慢覆盖目标区域。
3. 确认 `/map` 正常更新。
4. 保存地图：

```bash
ros2 launch mobile_robot_nav_bringup save_map.launch.py map_name:=my_map
```

默认地图保存到当前工作空间源码目录：

```text
src/nav2_minimal_bringup/maps/
```

生成文件通常为：

```text
maps/my_map.yaml
maps/my_map.pgm
```

## 常用参数

### full_navigation.launch.py

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `map` | `maps/1813lab.yaml` | 静态地图路径；LR-01 默认使用已批准的 `1813lab.yaml`。其他地图必须显式指定并在控制站登记。 |
| `nav2_params_file` | `config/nav2_params.yaml` | Nav2 参数文件。 |
| `car_params_file` | `car_ctrl/config/ddsm_hat_diff_drive.yaml` | 底盘、IMU、里程计参数。 |
| `lidar_params_file` | `lslidar_driver/params/lsx10.yaml` | 雷达参数。 |
| `use_nav_rviz` | `true` | 是否启动 Nav2 RViz。 |
| `use_lidar_rviz` | `false` | 是否启动雷达 RViz。 |
| `navigation_autostart` | `false` | 是否自动激活导航生命周期节点。 |
| `startup_navigation_on_initial_pose` | `true` | 是否在收到初始位姿后启动导航生命周期。 |

### navigation.launch.py

| 参数 | 说明 |
| --- | --- |
| `map` | 静态地图 YAML 路径。 |
| `params_file` | Nav2 参数文件。 |
| `use_sim_time` | 是否使用仿真时间。 |
| `use_rviz` | 是否启动 RViz。 |
| `use_composition` | 是否使用 Nav2 composition。 |

### slam.launch.py

| 参数 | 说明 |
| --- | --- |
| `slam_params_file` | slam_toolbox 参数文件。 |
| `use_sim_time` | 是否使用仿真时间。 |
| `start_lidar` | 是否同时启动雷达驱动。 |
| `use_rviz` | 是否启动 RViz。 |

### save_map.launch.py

| 参数 | 说明 |
| --- | --- |
| `save_dir` | 地图保存目录。 |
| `map_name` | 保存的地图名，不带扩展名。 |

## 验证

检查关键话题：

```bash
ros2 topic list | grep -E '/scan|/odom|/map|/cmd_vel|/amcl_pose'
```

检查 TF：

```bash
ros2 run tf2_ros tf2_echo map base_link
ros2 run tf2_ros tf2_echo odom base_link
```

检查 Nav2 生命周期节点：

```bash
ros2 lifecycle get /amcl
ros2 lifecycle get /controller_server
ros2 lifecycle get /bt_navigator
```

如果导航不动，优先检查：

- 地图是否与当前环境匹配
- `config/nav2_params.yaml` 中机器人半径、速度、加速度限制是否匹配底盘
- 底盘是否订阅 `/cmd_vel`
- `/scan` 的 frame 是否能正确变换到 `base_link`
- 是否已经在 RViz 中设置初始位姿

## 自主探索建图

自主探索使用 `explore_lite` 从 Nav2 全局代价地图中选择未知区域边界，
再通过 Nav2 的 `NavigateToPose` 动作行驶。该模式使用在线
`slam_toolbox` 地图，不会启动 AMCL 或静态地图服务器。

先在安全测试区域启动 SLAM 和 Nav2，但不让机器人移动：

```bash
source install/setup.bash
ros2 launch mobile_robot_nav_bringup autonomous_exploration.launch.py
```

确认 TF、`/scan`、`/map`、`/global_costmap/costmap` 和
`/navigate_to_pose` 正常后，再明确开启探索：

```bash
ros2 launch mobile_robot_nav_bringup autonomous_exploration.launch.py start_exploration:=true
```

Nav2 默认在底盘、雷达和 SLAM 启动 8 秒后创建，探索节点默认在 15 秒后创建，
以避免启动阶段资源争用。可用 `navigation_start_delay_sec` 和
`exploration_start_delay_sec` 增加等待时间。

运行时可暂停或恢复探索；暂停会取消当前导航目标，不会绕过现有的
`collision_monitor` 速度安全链：

```bash
ros2 topic pub --once /explore/resume std_msgs/msg/Bool "{data: false}"
ros2 topic pub --once /explore/resume std_msgs/msg/Bool "{data: true}"
ros2 topic echo /explore/status
```

参数位于 `config/exploration_params.yaml`。默认不会在探索结束时返回起点；
只有在地图、定位和回程路径均已验证时，才将 `return_to_init` 设为 `true`。
