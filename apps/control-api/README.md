# LR 中心控制 API

这是 `LR-Web` 仓库中的 FastAPI 中心控制 API。它提供认证、机器人状态、任务、告警、书架和高层机器人命令接口。配置 PostgreSQL、Redis 与 EMQX 后，会自动使用它们；不配置时保持内存模式，方便页面演示和测试。

## 方式一：使用 Conda 本地启动 API（推荐）

```bash
cd /home/boreas/project/LR/LR-Web
conda activate lr-control-api
LR_STORAGE_BACKEND=memory \
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

首次配置 Conda 环境：

```bash
cd /home/boreas/project/LR/LR-Web
conda create -y -n lr-control-api python=3.11 pip
conda activate lr-control-api
python -m pip install -e 'apps/control-api[dev]'
```

看到 `Uvicorn running on http://0.0.0.0:8080` 后，API 已启动。另开终端验证：

```bash
curl http://127.0.0.1:8080/health
```

接口文档：<http://localhost:8080/docs>，ReDoc：<http://localhost:8080/redoc>，健康检查：<http://localhost:8080/health>。

默认演示账户：`admin / 123456`。前端会调用此 API 登录，保存 Bearer Token，并自动附带到后续机器人、任务、告警和书架请求。

地图导入与「同步巡航点」会只读控制站批准的巡航点副本，并将其中 `frame_id: map` 的点自动预标到地图上。该功能默认关闭：需要把批准的快照部署到容器默认路径 `/app/config/cruise-points.yaml`（本机开发可用 `LR_CRUISE_POINTS_FILE` 覆盖），并设置 `LR_CRUISE_POINTS_ENABLED=true` 才会注入；仓库内的 `infra/cruise-points.example.yaml` 只是开发样例，不会被自动读取。页面中修改巡航点显示名称不会写回该 YAML，因此不会影响机器人正在运行的巡航路线。维护人员变更车端路线时，必须在同一维护窗口同步控制站副本。

停止 API：在运行 `uvicorn` 的终端按 `Ctrl+C`。退出 Conda 环境：`conda deactivate`。

## 方式二：启动前端并联调 API

保持 API 终端运行，再开第二个终端：

```bash
cd /home/boreas/project/LR/LR-Web
export PATH="$PWD/.tools/node/bin:$PATH"
export LR_API_URL=http://127.0.0.1:8080
npm ci                         # 仅首次执行
npm run dev
```

访问 <http://localhost:8000>。前端开发代理会把 HTTP `/api/` 请求转发到 FastAPI；登录、注册、机器人登记/删除、任务和告警页面此时使用后端接口。Utoopack 开发服务器不能转发任意 WebSocket 升级请求，因此实时机器人状态会直接连接 `LR_API_URL`（未设置时按当前网页主机的 `8080` 端口连接）。

停止前端：在运行 `npm run dev` 的终端按 `Ctrl+C`。修改 `LR_API_URL` 后必须重启前端。

## 已实现接口

- `POST /api/v1/auth/login`：登录并返回 Bearer token。
- `POST /api/v1/auth/register`：注册馆员账户。
- `GET /api/v1/auth/me`、`GET /api/v1/current-user`：读取当前账户。
- `GET` / `POST /api/v1/robots`、`DELETE /api/v1/robots/{robotId}`：查询、登记和永久删除机器人。
- `GET /api/v1/dispatch/tasks`、`GET /api/v1/alerts`：读取业务数据。
- `POST /api/v1/robots/{robotId}/commands`：创建高层机器人命令，检查机器人状态并支持幂等键。
- `WS /api/v1/realtime/robots`：已登录网页启动后自动连接的机器人状态流；先发送快照，再推送经过中心 API 校验的位姿更新。浏览器通过 `Sec-WebSocket-Protocol: lr-auth.<token>` 传递登录令牌。

除登录、注册、健康检查外的接口都要求：

```text
Authorization: Bearer <token>
```

## 方式三：Docker 启动基础服务，本机 Conda 运行 API

```bash
cd /home/boreas/project/LR/LR-Web
docker compose -f infra/docker-compose/docker-compose.yml up -d postgres redis minio emqx
docker compose -f infra/docker-compose/docker-compose.yml ps
```

Docker 仅启动 PostgreSQL、Redis、MinIO 和 EMQX。再另开一个终端，以 Conda 启动连接这些服务的 FastAPI：

```bash
cd /home/boreas/project/LR/LR-Web
conda activate lr-control-api
export LR_STORAGE_BACKEND=postgres
export LR_DATABASE_URL='postgresql://library_robot:change-me-before-production@127.0.0.1:5432/library_robot'
export LR_REDIS_URL='redis://127.0.0.1:6379/0'
export LR_MQTT_URL='mqtt://127.0.0.1:1883'
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

API 地址为 <http://localhost:8080/docs>。此时再按“方式二”的前端命令启动前端联调。

停止容器（保留数据卷）：

```bash
docker compose -f infra/docker-compose/docker-compose.yml down
```

若基础服务已直接安装在本机，可跳过 Docker 命令，但需使用相同的连接地址与端口。

## 基础设施接入

| 服务 | 当前实现 | 用途 |
| --- | --- | --- |
| PostgreSQL | 设置 `LR_DATABASE_URL` 后自动建表、首次写入演示数据，并持久化用户、机器人、任务、告警、书架、命令和审计事件。 | 业务主数据与审计。 |
| Redis | 设置 `LR_REDIS_URL` 后保存 Bearer 会话、命令幂等结果和机器人控制锁。 | 会话、去重与冲突控制。 |
| EMQX/MQTT | 设置 `LR_MQTT_URL` 后，命令会以 QoS 1 发布至 `fleet/{robotId}/command`，并订阅 `fleet/+/event` 与 `fleet/+/telemetry`。 | 向机器人 Edge Gateway 下发高层命令和接收实时位姿。 |

`/health` 会返回三项连接状态。开发时若 EMQX 暂不可用，命令仍会记录；设置 `LR_REQUIRE_MQTT=true` 后，MQTT 未连接会拒绝创建命令。生产环境必须启用 TLS/mTLS、独立密钥、数据库迁移、备份、审计保留和 Gateway 回执消费。`LR_MQTT_TLS_ENABLED=true` 时必须同时设置 `LR_MQTT_CA_FILE`；若使用客户端证书，`LR_MQTT_CLIENT_CERT_FILE` 与 `LR_MQTT_CLIENT_KEY_FILE` 必须成对设置。
