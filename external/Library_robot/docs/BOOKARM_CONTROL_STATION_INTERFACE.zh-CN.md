# BookArm 控制站与车端接口

本文定义控制站向车端下发“按书脊图片取书/还书”的版本 2 契约。车端实现位于
`robot_decision.edge_gateway_protocol`、`spine_artifact`、`bookarm_sdk_backend` 和
`edge_gateway_node`。控制站不得直接连接机械臂、CAN、串口或 BookArm SDK。

## 1. 当前能力边界

- 当前 SDK 开放借书 `operation=borrow`、`workflowMode=takeout`，以及还书
  `operation=return`、`workflowMode=putback`。
- 取书使用 `destinationSlot=1..4`；还书使用 `sourceSlot=1..3`，两者是不同方向的车载槽位。
- 升降台、机械臂、相机和推书杆由车端单一进程持有；旧 MotorStudio 与 SDK 后端不能同时持有硬件。
- 取消只能请求 SDK 在安全边界停止。机构运动期间，物理急停仍是最终安全路径，尤其是升降台运动。

车端 SDK 运行时只监听 `127.0.0.1`，由 Gateway 调用以下内部接口；控制站不能直接访问：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/v1/health` | 只读运行时健康状态 |
| `GET` | `/api/v1/capabilities` | 只读能力声明 |
| `POST` | `/api/v1/preflight` | 按 `operation` 构建但不启动对应运行时 |
| `POST` | `/api/v1/take-book` | 启动取书 |
| `POST` | `/api/v1/return-book` | 启动还书 |
| `GET` | `/api/v1/workflows/{id}` | 查询工作流 |
| `POST` | `/api/v1/workflows/{id}/cancel` | 请求取消 |
| `POST` | `/api/v1/safety/stop` | 请求本地安全停止 |

## 2. 高度含义和前端校验

控制站字段 `shelfLevel.liftTargetMm` 表示书架该层的真实层高，不是传给升降台的最终目标。
车端始终执行以下换算，控制站不能自行传入偏移后的值：

```text
effectiveLiftTargetMm = shelfLevel.liftTargetMm - 60 mm
```

BookArm AIM 升降台的标定目标范围为 `10..1700 mm`，所以前端层高允许范围是
`70..1760 mm`，包含两个端点。控制站应在保存书架层和创建任务时都校验该范围；车端会在
下载图片、导航或调用硬件之前再次校验。超限时不能截断到边界。

其他 SDK 参数由车端固定使用安全默认值：

| 参数 | 默认值 |
| --- | --- |
| `liftSpeedLevel` | `1` |
| 取书 `destinationSlot` | `1`；命令可显式选择 `1..4` |
| 还书 `sourceSlot` | `1`；命令可显式选择 `1..3` |
| 取书停止阶段 | `arm_standby`，执行完整取书流程 |
| 还书停止阶段 | `shelf_place`，执行完整还书流程 |

`shelfLevel.slotNo` 是书在书架上的格位，不是车载书架槽位。两者不能混用。

## 3. MQTT 命令

主题保持为 `fleet/{robotId}/command`。完整命令示例：

```json
{
  "commandId": "CMD-01JXYZ",
  "robotId": "LR-01",
  "command": "book_task",
  "payload": {
    "taskId": "TSK-01JXYZ",
    "operation": "borrow",
    "workflowMode": "takeout",
    "book": {"id": "CPY-001", "title": "机器人学导论"},
    "spineImage": {
      "artifactId": "CPY-001",
      "mediaType": "image/jpeg",
      "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "sizeBytes": 184230
    },
    "shelf": {
      "mapId": "MAP-A7E3270A2942",
      "annotationId": "ANN-SHELF-01",
      "frameId": "map",
      "x": 1.2,
      "y": -0.8,
      "yaw": 1.57
    },
    "shelfLevel": {
      "id": "SLV-001",
      "levelNo": 2,
      "slotNo": 11,
      "liftTargetMm": 680,
      "liftToleranceMm": 5
    },
    "destinationSlot": 1,
    "delivery": {
      "mapId": "MAP-A7E3270A2942",
      "annotationId": "ANN-DELIVERY-01",
      "frameId": "map",
      "x": 0.5,
      "y": 0.2,
      "yaw": 0
    }
  },
  "expiresAt": "2026-08-05T12:01:00+08:00",
  "safetyLevel": "high"
}
```

约束：

- `spineImage.artifactId` 必须等于 `book.id`。
- 还书不发送 `spineImage`，而必须发送 `leftNeighborSpineImage`；它是控制站保存的
  书架左邻居书脊图片，`artifactId` 不要求等于当前还书副本。
- 还书不发送 `destinationSlot`，必须发送 `sourceSlot`；车载还书 pickup 标定目前只接受 `1..3`。
- `sha256` 是图片原始字节的 64 位小写十六进制 SHA-256。
- `sizeBytes` 是原始字节数，范围 `1..8388608`。
- `mediaType` 只允许 `image/jpeg`、`image/png`、`image/webp`。
- MQTT 中不能放 Base64 图片、文件系统路径、任意 URL 或控制参数。
- 整个 MQTT 命令仍受车端 `64 KiB` 上限约束。

机器可读的 payload 契约见
[`contracts/book-task-v2.schema.json`](contracts/book-task-v2.schema.json)。地图坐标是否在当前地图边界内仍由车端运行时配置判定。

还书 payload 的关键部分如下（外层命令、`shelf`、`shelfLevel` 与取书相同；真实层高仍由车端减
60 mm）：

```json
{
  "taskId": "TSK-RETURN-01",
  "operation": "return",
  "workflowMode": "putback",
  "book": {"id": "CPY-001", "title": "机器人学导论"},
  "leftNeighborSpineImage": {
    "artifactId": "CPY-LEFT-001",
    "mediaType": "image/jpeg",
    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "sizeBytes": 184230
  },
  "shelf": {
    "mapId": "MAP-A7E3270A2942", "annotationId": "ANN-SHELF-01",
    "frameId": "map", "x": 1.2, "y": -0.8, "yaw": 1.57
  },
  "shelfLevel": {
    "id": "SLV-001", "levelNo": 2, "slotNo": 11,
    "liftTargetMm": 680, "liftToleranceMm": 5
  },
  "sourceSlot": 1
}
```

## 4. 车辆书脊下载端点

车端根据已配置的控制站 HTTPS origin 构造固定地址，不采用 MQTT 中的 URL：

```http
GET /api/v1/vehicle/book-spines/{artifactId}
Accept: image/jpeg
```

当前 `LR-Web/apps/control-api` 已实现该端点，并沿用车辆地图下载端点的 mTLS 认证链：

1. 反向代理验证车辆客户端证书，将经过验证的证书 CN 传给 API。
2. API 通过现有 `get_vehicle_identity` 解析 `LR-xx`，确认车辆已登记。
3. 只按 `artifactId` 读取服务端数据库中的 `BookCopy.spine_image`；禁止接收或拼接客户端路径。
4. 取书时确认图片属于 `book.id`；还书时确认图片等于任务中的
   `leftNeighborSpineImage.artifactId`，并执行车辆/有效任务授权策略。
5. 返回原始图片字节、准确的 `Content-Type` 和 `Content-Length`；不要重定向。
6. 图片缺失时，控制站不得创建任务，应返回业务错误。端点意外缺失时返回 `404`。

车端使用 MQTT 相同的每车 CA、客户端证书和私钥建立 HTTPS mTLS，校验证书链及主机名，最多读取
`8 MiB`，再校验响应类型、字节数、SHA-256 和 JPEG/PNG/WebP 文件签名。通过后图片以 `0600`
权限保存到 `/var/lib/lr-robot/book-spines/{taskId}/`，任务结束时清理。

## 5. 事件和错误码

事件主题保持为 `fleet/{robotId}/event`。取书正常顺序：

```text
book_task_dispatching
book_task_navigating
book_task_finetune_started
book_task_finetune_succeeded
book_task_workflow
book_task_delivery_dispatching
book_task_navigating
book_task_succeeded
```

还书正常顺序：

```text
book_task_dispatching
book_task_navigating
book_task_finetune_started
book_task_finetune_succeeded
book_task_workflow
book_task_succeeded
```

书架导航成功不是机构流程的准入证据。每笔取书和还书任务在 Nav2 到达书架后，车端都会使用
任务 `shelf.x/y/yaw` 调用本机 `finetune`。精调控制方式与巡逻精调一致：在 map 系下以机器人
当前朝向（base 系）计算位置误差，按 x → y → yaw 分阶段做低速 P 控制（y 阶段通过
"转 90°—前进—回正"修正横向误差，最多两轮），速度直接发布 `/cmd_vel` 到底盘，不经过
velocity_smoother / collision_monitor。
`book_task_finetune_started` 表示开始该关卡；只有收到
`book_task_finetune_succeeded` 后才会进入 `book_task_workflow`。前端不能把前者当作机构可动作。
微调服务不可用、目标无效、TF 缺失、超时、取消或最终误差超限都会产生
`book_task_failed`，此时 `requiresMechanismRecovery=false` 且取书库存仍为 `on_shelf`
（还书仍为 `off_shelf`），因为机构尚未启动。控制站无需也不得新增 MQTT 字段、微调坐标、
速度或容差；仍然只下发已审核的书架地图姿态。

`book_task_workflow` 可重复。取书最终包含 6 个阶段；还书最终包含 5 个阶段
`lift_move`、`arm_debug_pose`、`book_stand_grasp`、`vision_target`、`shelf_place`。
控制站不能把中间事件当作独立物理到位证明，必须等待 `book_task_succeeded`。

预检失败使用 `command_rejected`，`details` 中包含 `errorCode` 和 `reason`。车端当前稳定错误码：

| 错误码 | 含义 |
| --- | --- |
| `BOOK_TASK_INVALID` | 任务字段、地图或业务组合无效 |
| `BOOKARM_NOT_READY` | SDK、标定或本机设备预检未通过 |
| `SPINE_IMAGE_MISSING` | 取书缺少 `spineImage`，或还书缺少 `leftNeighborSpineImage` |
| `SPINE_IMAGE_INVALID` | 书脊描述或图片文件签名无效 |
| `SPINE_IMAGE_TOO_LARGE` | 图片超过车端上限 |
| `SPINE_IMAGE_DOWNLOAD_FAILED` | mTLS、HTTP 或网络下载失败 |
| `SPINE_IMAGE_TLS_CONFIG_INVALID` | 车端下载证书配置不可用 |
| `SPINE_IMAGE_MEDIA_TYPE_MISMATCH` | HTTP 类型与任务描述不一致 |
| `SPINE_IMAGE_SIZE_MISMATCH` | 实际字节数与任务描述不一致 |
| `SPINE_IMAGE_INTEGRITY_FAILED` | SHA-256 不一致 |
| `SPINE_IMAGE_CACHE_FAILED` | 车端私有缓存写入失败 |
| `LIFT_TARGET_OUT_OF_RANGE` | 层高减 60 mm 后不在 `10..1700 mm` |
| `BOOK_SHELF_FINETUNE_UNAVAILABLE` | 车端未启用或未部署书架最终位姿微调服务，任务在下载图片/导航前被拒绝 |

`LIFT_TARGET_OUT_OF_RANGE` 还会带回：`shelfHeightMm`、`heightOffsetMm`、
`effectiveLiftTargetMm`、`minimumLiftTargetMm` 和 `maximumLiftTargetMm`，前端应直接显示这些值。
工作流启动后的错误使用 `book_task_failed`，并保留 `inventoryDisposition` 与
`requiresMechanismRecovery`，控制站不得只根据“命令已接收”修改库存。

## 6. 控制站实现状态

控制站必须为两种操作分别生成图片摘要：取书使用目标 `spineImage`，还书使用左邻居
`leftNeighborSpineImage`。两种图片都通过车辆 mTLS 任务级下载授权和车端 SHA-256/大小校验；
层高 `70..1760 mm` 仍由控制站和车端双重校验。车端 `errorCode` 应持久化并显示。

控制站允许自动取书前仍会检查车辆遥测：`bookWorkflowModes` 包含 `takeout`，并且
`liftAbsolutePositionFeedback`、`liftTargetArrivalFeedback`、`armHomeFeedback` 和
`bookHandoffFeedback` 均为 `true`。AIM 绝对位置方案不要求额外声明独立
`liftHomeSensor=true`。

机械臂采用安全使能生命周期：本机旧 WebUI 服务启动时只自动连接 CAN，不使能电机；任务
通过 Gateway 门禁后才连接/使能并校验实时反馈。新 SDK 自动取还书成功后，必须通过 SDK 公共
接口移动到失能位、校验到位并确认电机失能，之后才允许上报正常终态。失能验证失败按工作流
失败处理并要求机构恢复；取消或故障只请求停止，不自动执行新的回位运动。旧后端没有可验证的
安全失能接口，因此不允许用其直接 `/arm/disable` 冒充这一收尾证据。

## 7. 车端启用前检查

代码合入不等于允许动作。切换 `bookarm_backend: sdk` 前必须完成：

- 本车已确认使用外部 BookArm 项目中的
  `config/calibration/camera_to_base.example.json` 作为相机到机械臂基座的 4x4 外参。
- `config/devices/camera.example.json` 是 D435 序列号、分辨率、帧率等设备配置，不是外参；
  车端分别通过 `--camera-config` 和 `--camera-calibration` 传入，不能互换。
- 审核车载书架目标槽位标定；当前外部文件只包含槽位 `1..3`，且文件说明仍含临时标定提示。
- 还书必须额外配置 `config/calibration/book_stand_pickup_slots.json` 和
  `config/profiles/return_book.example.json`；取书使用 `book_stand_slots.json` 和
  `take_book.example.json`。
- 确认 `/dev/lift_port`、`/dev/rodmotor`、D435 和 CAN 接口身份稳定且无占用冲突。
- 在同一次受控停机窗口中停止旧 MotorStudio 硬件持有者，再启用 SDK 后端；禁止两者并行。
- 先完成只读遥测和配置验收，再进入动作验收。
- 最终动作验收必须现场急停可用、机构清场、低速、有人监护；无需重启整台电脑。

SDK 运行服务的配置切换是显式操作，示例（仅供完成上述检查后使用）：

```bash
sudo ./scripts/install-bookarm-runtime.sh \
  --backend sdk \
  --bookarm-root '/home/boreas/project/lingzu_arm/lingzu-bookarm-py/bookarm' \
  --book-stand-slots '/home/boreas/project/lingzu_arm/lingzu-bookarm-py/bookarm/config/calibration/book_stand_slots.json' \
  --book-stand-pickup-slots '/home/boreas/project/lingzu_arm/lingzu-bookarm-py/bookarm/config/calibration/book_stand_pickup_slots.json' \
  --camera-calibration '/home/boreas/project/lingzu_arm/lingzu-bookarm-py/bookarm/config/calibration/camera_to_base.example.json' \
  --camera-config '/home/boreas/project/lingzu_arm/lingzu-bookarm-py/bookarm/config/devices/camera.example.json' \
  --take-book-profile '/home/boreas/project/lingzu_arm/lingzu-bookarm-py/bookarm/config/profiles/take_book.example.json' \
  --return-book-profile '/home/boreas/project/lingzu_arm/lingzu-bookarm-py/bookarm/config/profiles/return_book.example.json' \
  --spine-storage '/var/lib/lr-robot/book-spines'
```

这与外部项目脚本的调用关系一致：`return_book_test.py --camera-calibration` 读取带
`matrix` 的几何外参；若需要固定 D435 序列号和采集参数，则额外使用
`--camera-config config/devices/camera.example.json`。车端直接调用相同的公开 SDK composition
root，不执行测试脚本，也不依赖交互式 `--confirm`。

该安装命令默认只写入服务文件和私有环境，不会自动重启；切换前必须确认旧
MotorStudio 服务不会与 SDK 服务同时占用硬件。Gateway 私有 YAML 还要把
`bookarm_backend` 改为 `sdk`，并在受控窗口单独重启对应服务。当前对话没有执行上述命令，
也没有启动、停止或重启任何车端服务。
