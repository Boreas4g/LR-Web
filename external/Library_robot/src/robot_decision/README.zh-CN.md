# robot_decision 真机 Edge Gateway

`edge_gateway_node` 是部署在机器人本机的唯一云端控制入口。它把 LR-Web 经 EMQX 下发的高层任务转换为本机 Nav2 的 `navigate_to_pose` Action；浏览器和中心 API 都不直接连接 ROS 2、底盘串口或 `/cmd_vel`。

## 安全边界

- 只接受 `navigate`、`cancel`、`stop`、`emergency_stop` 四种高层指令。
- 每条指令必须匹配本机 `robot_id`，携带未过期的 `expiresAt` 和 `safetyLevel: high`。
- 导航任务只能使用 `map` 坐标系、已启用的地图 ID、配置范围内的 `x/y/yaw`。
- 同一时间只允许一个导航目标；QoS 重投按 `commandId` 去重。
- 网页的“急停”只取消当前 Nav2 Goal，**绝不能替代物理急停、底盘驱动急停或碰撞监控**。

## 首次安装与编译

在每台真机上执行：

```bash
sudo apt update
sudo apt install -y python3-paho-mqtt python3-yaml

cd /path/to/Library_robot
source /opt/ros/jazzy/setup.bash
colcon build --packages-select robot_decision --symlink-install
source install/setup.bash
```

## 配置真机

生产车辆不要复制工作空间的 [edge_gateway.yaml](config/edge_gateway.yaml)：该文件保留了
本地单机调试参数。应由控制站为每台车导出独立交付包，再运行
`scripts/install-control-station-gateway.sh` 写入 `/etc/lr-robot/` 私有配置。完整步骤见
[`../../docs/VEHICLE_CODEX_HANDOFF.zh-CN.md`](../../docs/VEHICLE_CODEX_HANDOFF.zh-CN.md)。

下面仅说明字段关系，所有值均为占位示例，不是可直接用于现场的配置：

```yaml
robot_id: "LR-01"
mqtt_host: "control-station.example"
mqtt_port: 8883
mqtt_tls_enabled: true
mqtt_ca_file: "/etc/lr-control/emqx-ca.pem"
mqtt_username: "lr-01"
mqtt_password: "每台机器人独立的高强度密码"
allowed_map_ids:
  - "MAP-REPLACE-WITH-APPROVED-ID"
```

同时按地图 YAML 的实际范围填写 `map_min_x`、`map_max_x`、`map_min_y`、`map_max_y`。`allowed_map_ids` 必须与真机当前由 Nav2 加载的地图、LR-Web 调度时选中的地图版本一致。

EMQX ACL 至少应限制：

```text
允许订阅 fleet/LR-01/command
允许发布 fleet/LR-01/event
拒绝其他 fleet/# topic
```

## 启动顺序

1. 先启动底盘、定位、地图和 Nav2，确认 `ros2 action list | grep navigate_to_pose` 有结果。
2. 再启动 Edge Gateway：

```bash
cd /path/to/Library_robot
source /opt/ros/jazzy/setup.bash
source install/setup.bash
ros2 launch robot_decision edge_gateway.launch.py \
  params_file:=/path/to/Library_robot/src/robot_decision/config/edge_gateway.yaml
```

3. 在 LR-Web 的“区域与书架”完成书架和地图标注关联；在“调度总台”选择相同地图版本和机器人后创建导航任务。

## 观测与验收

网关订阅 `fleet/LR-01/command`，回传 `fleet/LR-01/event`。中心 API 收到 `navigation_started`、`navigation_succeeded`、`navigation_failed`、`navigation_cancelled` 后会更新任务和机器人状态。

网关还会以默认 2 Hz 读取 Nav2/AMCL 的 `/amcl_pose`，将 `map` 坐标、朝向、当前地图 ID 和 Nav2 可用状态以 `telemetry` 事件回传。LR-Web 的“地图建模”页面会每 2 秒刷新并显示红色机器人图标；超过 10 秒没有新遥测会灰显为“已过期”。若没有位置，请先确认：`ros2 topic echo /amcl_pose --once` 有 `frame_id: map` 数据，且 `active_map_id` 与平台导入的地图 ID 一致。

首次真机测试应在空旷区域执行：低速、短距离、人员可随时按下物理急停，并确认碰撞监控已经启用。不要在载书、人员密集或未经定位确认的情况下首次联调。
