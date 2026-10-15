# 智巡-图书馆机器人

面向实体图书馆机器人的管理与调度平台。前端基于官方 [Ant Design Pro](https://github.com/ant-design/ant-design-pro) 构建，项目目录为：

```text
/home/boreas/project/LR/LR-Web
```

## 当前完成内容

- Ant Design Pro 企业后台基础、中文导航和六个一级菜单。
- 系统总览：机器人在线数、状态、告警与进行中任务。
- 图书馆管理：地图版本、楼层区域、书架、充电区和停靠位姿可视化骨架。
- 机器人调度：前往书架、回充、暂停/停止和任务队列的安全接口边界。
- 图书管理：书目/实体书、书架格口、照片分割入库和审核流程。
- 借阅管理：自主取书/还书的可恢复任务步骤。
- 系统设置：机器人、书架、告警规则和审计日志。
- REST 与 MQTT Gateway 契约、PostgreSQL/Redis/MinIO/EMQX 容器编排模板。
- FastAPI 中心控制 API MVP 位于 `apps/control-api`，提供认证、机器人状态、任务、告警、书架和高层命令接口。

所有真实机器人控制仍需经车载 Edge Gateway 接入，页面不会直接操作 ROS、CAN 或串口。生产部署采用控制站与车载端分离：浏览器只连接控制站网页/API，机器人只主动通过 MQTT/mTLS 连接控制站。

## 启动项目

完整、可持久化的启动方式见 [启动指南](docs/STARTUP.zh-CN.md)。默认命令保留本地单机调试兼容；交付生产环境请按指南使用控制站的 TLS/mTLS 分离部署。

仅查看页面可使用前端 Mock：

```bash
cd /home/boreas/project/LR/LR-Web
export PATH="$PWD/.tools/node/bin:$PATH"
npm ci
npm run dev
```

访问 <http://localhost:8000>。Mock 模式不会保存机器人变更。

API 路由和命令格式见 [robot-gateway-v1.yaml](contracts/robot-gateway-v1.yaml)，MQTT Topic 与安全约束见 [mqtt-topics.md](contracts/mqtt-topics.md)。

## ROS 2 与机械臂接入

- 小车、雷达、Nav2、SLAM、书架点位精调和升降台 ROS 2 实现：`/home/boreas/Library_robot`。
- 机械臂、夹爪、CAN 与 RealSense SDK：`/home/boreas/project/lingzu_arm/lingzu-bookarm-py`。

每台机器人应运行 `apps/edge-gateway` 对应的独立服务：订阅中心 MQTT 后，在本机调用 ROS 2 Action/Topic 与 Arm Bridge。接入原则和速度安全链见 [ros-integration.md](docs/architecture/ros-integration.md)。

## 基础服务

本地启动 PostgreSQL、Redis、MinIO 与 EMQX（Compose 只管理这些基础设施；control-api 由宿主 `lr-control-api.service` 运行，前端产物由 `scripts/serve-static.cjs` 提供）：

```bash
docker compose -f infra/docker-compose/docker-compose.yml up -d
```

生产部署前必须替换默认密码，配置 TLS/mTLS、备份、日志保留和访问控制。生产控制站使用 `LR_PLATFORM_MODE=production` 叠加 `infra/docker-compose.production.yml`（仅加固 EMQX 的 TLS/mTLS），证书和私钥仅保存在 `/etc/lr-platform/`，不进入仓库。

## 安全边界

- 浏览器只创建高层调度命令，绝不直接发布 `/cmd_vel`、CAN 帧或串口指令。
- 底盘速度需经过 `twist_mux`、`collision_monitor` 后才到 `/cmd_vel`。
- 机械臂由唯一 Arm Bridge 占用 CAN，禁止多个进程并发控制。
- 精细控制要求维护角色、控制租约、速度/行程限制、硬件急停和审计。
- 书脊分割仅用于检测书本边界；创建实体书前必须 OCR/条码/RFID 或人工审核。
