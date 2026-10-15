# 车端-控制站硬件联调验收

本清单用于 `LR-01` 的分阶段现场验收。它不改变实体急停、限位、碰撞保护或现有服务的
独立安全责任。任何动作均需现场负责人明确授权、清场、急停可用和监护；前端发起操作即
视为现场确认（车端 `remote_actuation_enabled` 开关已于 2026-09-26 移除）。

## 当前软件声明

Gateway 每个 telemetry 帧会发布 `details.hardwareCapabilities`。当前
`LR-01-edulite-a3-v1` 仅依据已接入的本机接口声明：

```json
{
  "schemaVersion": "lr-vehicle-hardware-v1",
  "profile": "LR-01-edulite-a3-v1",
  "navigation": true,
  "mapSwitching": true,
  "manualJog": true,
  "softwareStop": true,
  "liftRelativeMotion": true,
  "liftHomeSensor": false,
  "liftAbsolutePositionFeedback": false,
  "liftTargetArrivalFeedback": false,
  "armMotion": true,
  "armFeedback": true,
  "armHomeFeedback": false,
  "rodMotion": true,
  "cameraCapture": true,
  "bookHandoffFeedback": false,
  "bookWorkflowModes": []
}
```

因此，当前版本允许维护用途的受限相对升降及手动机械臂流程，但拒绝绝对层位、机构自动
回零和全部自动取/还书。相对升降的本机 worker 回执只会发布
`lift_move_driver_acknowledged`，不会被视为物理到位。

## A. 遥测与地图（无动作）

现场负责人确认后，在双开关保持关闭时检查：

1. MQTT/mTLS 连通、控制站显示实时 telemetry、`bootId` 和地图 ID。
2. `hardwareCapabilities` 与上方 JSON 一致，未知项没有被显示为可用。
3. `armReady`、`liftReady`、`cameraReady`、`rodReady`、CAN 反馈字段会在设备掉线后变为 `false`。
4. 本地已校验地图才进入 `availableMapIds`；切图成功仅在 Nav2 LoadMap 成功后确认。
5. 动作验收前，现场维护人员只读核对控制站记录、Gateway telemetry 的 `mapId`、本机
   `active-map` 状态和 `/map` 元数据。地图 ID 与 PGM 几何（原点、分辨率、宽高）必须
   对应同一已登记版本；任一不一致时保持 observe，禁止导航、点动和机构流程。不要将
   `map_server.yaml_filename` 或 ROS 参数 `active_map_id` 当作切图后的实时身份：Nav2
   LoadMap 和节点内部地图切换会保留它们的启动值。
6. Gateway 与自动定位会拒绝落在当前地图边界外的 AMCL 或人工初始位姿，并报告
   `manual_required`。不得通过扩大边界、沿用旧坐标或直接发布速度来绕过该保护。

失败回退：保持 observe，修复网络、地图或本机硬件健康后重新从 A 开始。

## B. 底盘维护点动（单独授权）

验收短时点动、松开/失焦/点动到时零速度、导航接管取消和实体急停。记录编码器反馈、
`manual_jog_*` 事件及人工急停结果。

失败回退：关闭车端远程动作，保持底盘维护状态，修复安全速度链或编码器后重新验收。

## C. 相对升降维护（单独授权）

验收底盘静止、无租约、单次小距离、软/硬限位和停止链。证明 Gateway 在驱动 ACK 后仍报告
`physicalCompletionVerified: false`。超时、取消和 emergency stop 必须请求本机 arm-cancel 与
lift-stop，且不得生成“已到位”事件。

失败回退：停止机构、现场检查高度/障碍/限位；若位置不确定，不得继续任何书架层位任务。

## D. 绝对升降（待硬件补齐）

要将以下能力由 `false` 变为 `true`，维护人员必须先实现并提供每项证据：独立原点输入、
回零后 mm 物理高度、掉电/失步后失效、目标公差内稳定测量、限位与报警检测、超时安全停止。

完成后，先以空载逐层验证 `targetMm`、`measuredMm`、`toleranceMm` 和
`physicalCompletionVerified`，再允许控制站创建层位自动任务。

## E. 机械臂、推书杆与视觉（待零位反馈）

单独验收 CAN 新鲜反馈、各轴使能、故障拦截、推书杆行程/堵转保护、相机时间戳和安全停止。
只有经编码器/限位验证现场安全零位后，才可将 `armHomeFeedback` 标为 `true`。

## F. 自动取还书（待书本交接证据）

启用前必须同时满足 D、E 以及 `bookHandoffFeedback: true`，并对每个已验收模式单独填入
`bookWorkflowModes`。以空载/测试书验证导航前失败、机构中失败、取书后失败、还书中失败：

- 未开始机构动作：`on_shelf`；
- 已有可靠书本交接证据：`on_shelf` 或 `off_shelf`；
- 任一交接不确定：`unknown`，控制站锁定库存等待人工核验；
- 机构未能确认安全位：保留恢复锁，只有独立验证的 `mechanism_home_succeeded` 才可解除。

只有 A--F 全部留存证据后，现场负责人才能分别开启控制站 control 模式和车端远程动作。
