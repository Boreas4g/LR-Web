# ROS 2 与硬件接入边界

## 部署边界

生产环境把中心控制站和机器人车载机分开部署。控制站运行网页、FastAPI、PostgreSQL、Redis 与 EMQX；车载机只运行 ROS 2、硬件驱动、导航定位和 Edge Gateway。浏览器只连接控制站 HTTPS/WSS，Edge Gateway 只从车载机主动连接控制站 MQTT/mTLS。ROS 2 Discovery 限制为 `LOCALHOST`，任何 ROS、DDS、串口、CAN 与 `/cmd_vel` 都不得向局域网暴露。

## 现有 ROS 2 工作空间

机器人侧复用 `/home/boreas/Library_robot` 的 ROS 2 Jazzy 工作空间：

- `car_ctrl`：DDSM 底盘、`/cmd_vel`、轮速、IMU 和里程计。
- `mobile_robot_nav_bringup`：AMCL、Nav2、双雷达融合、碰撞监控、地图加载与 SLAM。
- `robot_decision`：书架点位、导航后二次精调、书本中心检测。
- `arm_aux_devices`：升降台和转盘。

机械臂复用 `/home/boreas/project/lingzu_arm/lingzu-bookarm-py` 的 `ELA3Interface`。机械臂须由单一 Arm Bridge 进程持有，禁止 Gateway、MotorStudio 和其他脚本并发占用 CAN。

## Gateway 动作映射

| 平台命令 | Gateway 动作 | 当前基础 |
| --- | --- | --- |
| `navigate` | 发送 `NavigateToPose` 并回传 feedback/result | Nav2 已具备 |
| `cancel` / `pause` | 取消 Nav2 Goal，停止任务状态机 | 需封装 |
| `charge` | 调用 OpenNav Dock Action | 参数已配置，需实测充电桩与 BMS |
| `fine_align` | 调用书架对位 Action | 现有节点需从 Trigger 升级为 Action |
| `lift` | 调用升降台 Action | 现有 Topic 控制需封装并增加行程限制 |
| `pick` / `place` | 调用 Arm Bridge 预定义、审核过的动作序列 | 需实现 |

## 速度与安全链

网页不能直接发布 `/cmd_vel`。推荐链路：

```text
Gateway teleop/nav request -> twist_mux -> collision_monitor -> /cmd_vel -> car_ctrl
```

现有 Nav2 已使用 `cmd_vel_smoothed -> collision_monitor -> /cmd_vel`。上线前必须把精调节点也迁入该链路，确保不存在绕过避障的速度发布者。
