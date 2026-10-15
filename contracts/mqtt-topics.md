# MQTT 消息契约

所有机器人均从车载机主动通过 mTLS 与控制站 EMQX 连接。中心平台只发布高层命令，Edge Gateway 负责转换为 ROS 2 Action、Topic 或机械臂 SDK 调用；浏览器不连接 MQTT，也不掌握机器人网络地址或硬件凭据。

| Topic | 方向 | QoS | 用途 |
| --- | --- | --- | --- |
| `fleet/{robotId}/telemetry` | Gateway → 平台 | 0 | 在线状态、位置、设备健康，建议 1 Hz；中心 API 也兼容现有 `event: telemetry` 回执 |
| `fleet/{robotId}/event` | Gateway → 平台 | 1 | 任务步骤、告警、地图状态和关键安全事件 |
| `fleet/{robotId}/command` | 平台 → Gateway | 1 | 带过期时间、幂等键的高层命令 |
| `fleet/{robotId}/command-ack` | Gateway → 平台 | 1 | 已接收、拒绝、执行中、完成或失败回执 |

命令必须包含 `commandId`、`taskId`、`expiresAt`、`idempotencyKey`、发起用户和安全级别。Gateway 对过期、重复、无租约或不安全命令必须拒绝，并写入审计日志。`cruise` 的 `waypoints` 由 Gateway 依序执行，并受 `loopCount` 上限约束；不得由网页逐点补发导航命令。

实时底盘速度、机械臂关节和升降台脉冲不通过 MQTT 直接透传；它们只在本机 Gateway 的安全控制层内生成。`manual_jog` 仅携带有效 `leaseId`、低速线/角速度与不超过 750 ms 的持续时间；Gateway 必须在租约、命令或通信任一超时后发布零速度，并经 `twist_mux` 和碰撞监控后才可驱动底盘。

已登录的网页启动后会自动连接中心 API 的 `/api/v1/realtime/robots` WebSocket。该连接只接收中心 API 已校验并落库的状态，不会直接暴露 ROS 或 MQTT。
