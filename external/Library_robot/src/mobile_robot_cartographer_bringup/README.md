# mobile_robot_cartographer_bringup

最小 Cartographer 建图功能包，面向 Ubuntu 24.04 + ROS 2 Jazzy，并对齐当前 `mobile_robot_ws` 的实车底盘、IMU、雷达、TF 和地图目录布局。

这个包是给当前工作空间单独补的一套 Cartographer 方案，不会改现有 `mobile_robot_nav_bringup` 的导航与 `slam_toolbox` 工作流。

默认对齐的实车接口：

- 里程计：`/odom`
- 雷达：`/scan`
- 底盘基座：`base_link`
- 雷达帧：`laser_link`
- 地图输出：`/map`

默认也复用当前工作空间已经在用的硬件配置：

- 底盘/IMU 参数：`car_ctrl/config/ddsm_hat_diff_drive.yaml`
- 雷达参数：`lslidar_driver/params/lsx10.yaml`
- 地图保存目录：工作空间根目录下的 `maps/`

## 包内容

- `launch/cartographer_mapping.launch.py`
  实车 Cartographer 建图入口，默认一并启动底盘、IMU、里程计、雷达、静态 TF、Cartographer 和占据栅格。
- `launch/save_map.launch.py`
  保存当前 `/map` 到工作空间根目录 `maps/`。
- `config/mobile_robot_2d.lua`
  面向当前实车话题和 TF 的 2D Cartographer 参数。

## 为什么不能直接用旧代码

原来那份 `yahboomcar_nav` 里的 Cartographer 启动代码不能直接拿到当前环境用，主要原因是：

- 包名和工作空间结构不同，旧代码依赖 `yahboomcar_nav`
- 旧代码不会启动当前 `mobile_robot_ws` 的底盘、IMU、里程计和雷达驱动
- 旧代码没有对齐当前实车的 `base_link -> laser_link` 静态 TF
- 旧代码的参数目录和当前工作空间地图/配置布局不一致

这个包已经把这些差异按当前环境对齐了。

另外，这台机器当前安装的 `cartographer_ros` 与 `fastcdr` 二进制包之间有一个 ABI 兼容问题。
这个包已经内置兼容 shim，并在提供的 launch 里自动预加载，不需要你手动处理系统库。

## 前提

- 已安装 ROS 2 Jazzy
- 已安装 `cartographer_ros`
- 已安装当前工作空间其余依赖
- 底盘、IMU、雷达设备连接与现有工作空间保持一致

如果系统还没装 Cartographer：

```bash
sudo apt update
sudo apt install -y ros-jazzy-cartographer-ros
```

## 编译

在工作空间根目录执行：

```bash
cd /home/boreas/mobile_robot_ws
source /opt/ros/jazzy/setup.bash
colcon build --packages-select mobile_robot_cartographer_bringup --symlink-install
source install/setup.bash
```

如果你要连同整个工作空间一起编译：

```bash
cd /home/boreas/mobile_robot_ws
source /opt/ros/jazzy/setup.bash
rosdep install --from-paths src --ignore-src -r -y
colcon build --symlink-install
source install/setup.bash
```

## 启动

实车建图：

```bash
cd /home/boreas/mobile_robot_ws
source /opt/ros/jazzy/setup.bash
source install/setup.bash

ros2 launch mobile_robot_cartographer_bringup cartographer_mapping.launch.py
```

默认会一起启动：

- `car_ctrl` 的 `ddsm_hat_diff_drive_node`
- `car_ctrl` 的 `imu_driver`
- `car_ctrl` 的 `car_odometry`
- `lslidar_driver`
- `base_link -> laser_link` 静态 TF
- `cartographer_node`
- `cartographer_occupancy_grid_node`

如果你只是想在硬件驱动已经运行时单独起 Cartographer：

```bash
ros2 launch mobile_robot_cartographer_bringup cartographer_mapping.launch.py \
  start_robot_drivers:=false
```

如果你要打开 RViz：

```bash
ros2 launch mobile_robot_cartographer_bringup cartographer_mapping.launch.py \
  use_rviz:=true
```

## 保存地图

建图一段时间后，另开终端保存当前地图：

```bash
cd /home/boreas/mobile_robot_ws
source /opt/ros/jazzy/setup.bash
source install/setup.bash

ros2 launch mobile_robot_cartographer_bringup save_map.launch.py map_name:=cartographer_map
```

默认保存到：

```bash
/home/boreas/mobile_robot_ws/maps/
```

会生成：

- `maps/cartographer_map.yaml`
- `maps/cartographer_map.pgm`

如果要显式指定保存目录：

```bash
ros2 launch mobile_robot_cartographer_bringup save_map.launch.py \
  save_dir:=/home/boreas/mobile_robot_ws/maps \
  map_name:=cartographer_map
```

## 关键参数

Cartographer Lua 配置在：

- `config/mobile_robot_2d.lua`

当前默认值重点对齐如下：

- `map_frame = "map"`
- `tracking_frame = "base_link"`
- `published_frame = "base_link"`
- `odom_frame = "odom"`
- `use_odometry = true`
- `provide_odom_frame = false`
- `num_laser_scans = 1`
- `use_imu_data = false`

默认静态 TF 也和现有导航/建图配置保持一致：

- `base_link -> laser_link`
- 平移：`x=0.26, y=0.0, z=0.0`
- 偏航：`yaw=-0.78539816339`

## 使用流程

1. 确认底盘、IMU、雷达设备接线与当前工作空间现有配置一致。
2. 启动 `cartographer_mapping.launch.py`。
3. 缓慢驾驶机器人覆盖建图区域。
4. 观察 `/map` 是否持续更新。
5. 使用 `save_map.launch.py` 保存地图。
6. 后续把保存出的地图给 `mobile_robot_nav_bringup` 继续做定位与导航。

## 验证

检查包是否被当前工作空间接管：

```bash
cd /home/boreas/mobile_robot_ws
source /opt/ros/jazzy/setup.bash
source install/setup.bash
ros2 pkg prefix mobile_robot_cartographer_bringup
```

检查关键话题：

```bash
ros2 topic list | grep -E '/scan|/odom|/map|/tf'
```

检查 TF：

```bash
ros2 run tf2_ros tf2_echo base_link laser_link
ros2 run tf2_ros tf2_echo odom base_link
```

如果建图不起作用，优先检查：

- `/scan` 是否正常发布
- `/odom` 是否正常发布
- TF 中是否存在 `odom -> base_link` 和 `base_link -> laser_link`
- 雷达最小量程、安装角度和 Lua 参数是否匹配实机
