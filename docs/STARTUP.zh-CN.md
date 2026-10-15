# LR 图书馆机器人平台使用说明

> 完整的首次部署、机器人接入、桌面端安装、日常操作与排障请优先阅读 [统一部署与操作手册](./OPERATIONS.zh-CN.md)。本文保留架构和启动参考。

交付后，普通操作人员只访问控制站网页；不需要也不能登录机器人电脑运行 ROS、底盘、数据库或前端服务。

## 日常使用

控制站一体机开机后会自动启动。操作员打开桌面端即可自动发现同一局域网中的控制站并选择，不需要登录控制站电脑、运行 Docker 或知道 IP。浏览器仍可打开部署人员提供的地址，例如：

```text
https://控制站IP或域名
```

当前演示初始管理员为 `admin / 123456`。当前版本尚未完成管理员改密与禁用注册能力，因此带该默认口令的实例只能部署在隔离验收网或严格访问控制的维护网，不能直接暴露给普通生产网络。

页面右上角的 **控制站已连接** 表示浏览器已连接控制站的实时状态流；它不表示直接连接机器人。显示“正在重连控制站”时可以刷新页面或联系维护人员，网页不会绕过中心服务向机器人下发任何指令。

如果平台没有随开机启动，由部署人员在控制站项目目录执行一次：

```bash
./scripts/start-platform.sh
```

该命令只使用本机已有镜像，不会因外网/Docker Hub 临时不可达而拉取或构建。发布新版本请在维护窗口使用 `./scripts/update-platform.sh`；它默认只更新本地构建的 Web 与 control-api，不会因 Broker 镜像拉取失败而放弃应用更新。如需要同时升级经过验证的 Broker 镜像，在私有 `platform.env` 设置 `LR_EMQX_IMAGE` 并显式设置 `LR_PLATFORM_PULL_BROKER=true`。启动成功后，终端会打印可访问地址。停止整个平台：

```bash
./scripts/stop-platform.sh
```

机械臂服务是独立的硬件进程，不随平台网页自动上电。它未安装、未启动或现场检查不通过时，平台仍可正常启动；书脊工作台、取书和还书接口会返回不可用。受管机械臂服务上线后，下一次执行 `./scripts/start-platform.sh` 会自动挂载其私有 Unix socket。不得同时运行旧的 `:8090` BookArm TCP 服务与新的 socket 服务。

低内存静态预览命令 `node scripts/serve-static.cjs --port 8000` 同样默认代理 `/run/lr-bookarm/bookarm.sock`。如确有经过现场确认的旧 TCP 服务需要临时兼容，必须显式设置 `LR_BOOKARM_TARGET=127.0.0.1:8090`；该兼容开关不适用于与新受管服务并行运行。

## 生产部署：控制站与机器人分离

生产环境有两台独立电脑，且两者只通过 MQTT/TLS 交换高层命令与状态：

```text
操作员浏览器 -- HTTPS/WSS --> 控制站（网页、API、数据库、Redis、EMQX）
机器人 Edge Gateway -- MQTT/mTLS --> 控制站 EMQX
Edge Gateway -- 本机 ROS 2 --> Nav2、AMCL、IMU、雷达、底盘
```

浏览器不得访问机器人 IP 的 ROS、DDS、串口、CAN 或 `/cmd_vel`；控制站 API 也不得导入 ROS 或硬件驱动。即使 MQTT 断线，机器人本地定位持续运行，Gateway 的手动控制超时仍会发送零速度。

### 控制站一次性安装

以下步骤只由部署人员在控制站执行。生产配置使用 TLS 网页和 mTLS MQTT；绝不能使用仓库中的示例密码或证书。

```bash
cd /home/boreas/project/LR/LR-Web
sudo install -d -o root -g root -m 0750 /etc/lr-platform
sudo cp infra/control-station.env.example /etc/lr-platform/platform.env
sudo chown root:root /etc/lr-platform/platform.env
sudo chmod 0640 /etc/lr-platform/platform.env
# 使用签发脚本创建并导出证书、Control API 账号和每台机器人账号。
sudo ./scripts/manage-fleet-pki.sh init-ca
sudo ./scripts/manage-fleet-pki.sh issue-station control-station.example 192.168.10.10
sudo ./scripts/manage-fleet-pki.sh batch /root/fleet/robots.csv
sudo ./scripts/manage-fleet-pki.sh verify
sudo ./scripts/manage-fleet-pki.sh export-station /etc/lr-platform
# 编辑 platform.env：填写数据库强密码和 EMQX Dashboard 管理员账号；随后封装为一体机。
sudo ./scripts/install-control-station-appliance.sh \
  --station-url https://control-station.example \
  --station-name '图书馆机器人控制站'
# 若复用了已有 EMQX 数据卷，显式同步全部账号后重启平台。
sudo ./scripts/manage-fleet-pki.sh sync-emqx all
sudo systemctl restart lr-platform.service
```

脚本生成的 `/etc/lr-platform/platform-pki.env` 会自动配置 `LR_MQTT_CERTS_DIR`、`LR_WEB_TLS_CERTS_DIR`、`LR_EMQX_BOOTSTRAP_USERS_FILE` 及 Control API MQTT 密码；根 CA 私钥不会出现在 `/etc/lr-platform`。每台车使用独立 `LR-xx` 证书和密码。新增车或轮换后，在部署车端新文件后执行 `sudo ./scripts/manage-fleet-pki.sh sync-emqx LR-xx`，再重启该车载运行时。完整签发、交付、轮换和恢复过程见 [证书维护说明](./PKI.zh-CN.md)。

### 机器人一次性安装

以下步骤只由部署人员在机器人电脑执行。先将 `src/robot_decision/config/edge_gateway.control-station.yaml.example` 复制到 `/etc/lr-robot/edge-gateway.yaml`，填写本车 ID、控制站地址、地图参数、证书路径和该车 MQTT 密码；证书及私钥仅放在 `/etc/lr-robot/mqtt/`，目录权限设为 `0750 root:boreas`，私钥设为 `0640 root:boreas`。

```bash
cd /home/boreas/Library_robot
sudo install -d -o root -g boreas -m 0750 /etc/lr-robot/mqtt
sudo cp src/robot_decision/config/edge_gateway.control-station.yaml.example /etc/lr-robot/edge-gateway.yaml
sudo chown root:boreas /etc/lr-robot/edge-gateway.yaml
sudo chmod 0640 /etc/lr-robot/edge-gateway.yaml
sudo ./scripts/install-robot-runtime-autostart.sh
sudo sh -c 'printf "%s\\n" "LR_GATEWAY_PARAMS_FILE=/etc/lr-robot/edge-gateway.yaml" "LR_ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST" > /etc/lr-robot/runtime.env'
sudo chown root:boreas /etc/lr-robot/runtime.env
sudo chmod 0640 /etc/lr-robot/runtime.env
sudo systemctl restart lr-robot-runtime.service
```

此服务只启动车载底盘、雷达、AMCL/Nav2、IMU、自动定位恢复和 Edge Gateway。`mqtt_host` 必须与控制站服务器证书的 DNS 名称或 IP Subject Alternative Name 匹配。机器人开机后会主动连接控制站；控制站不可用时，车端传感器和定位仍会启动，连接恢复后自动继续上传状态。

### 防火墙与访问边界

控制站仅允许操作员网段访问 `443/tcp`（或配置的 HTTPS 端口），仅允许机器人网段访问 `8883/tcp`。`8080`、`1883`、`5432`、`6379`、`9000`、`9001` 和 `18083` 都必须保持本机绑定或仅供维护网段访问。机器人电脑不开放 ROS 2/DDS、MQTT、串口、CAN 或任何底盘控制端口给局域网；`ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST` 已使 ROS 2 发现限制在车内。

## 单机本地调试（保留）

现有同机调试不受影响：不设置 `LR_PLATFORM_MODE` 时，`./scripts/start-platform.sh` 仍在 `http://localhost:8000` 提供网页，MQTT 仅绑定在 `127.0.0.1:1883`。机器人运行时若未设置 `LR_GATEWAY_PARAMS_FILE`，继续使用工作空间中原有的 `edge_gateway.yaml`（其中 MQTT 为 `127.0.0.1`）。

控制站中的 `/etc/lr-platform/cruise-points.yaml` 是供网页标注用的已批准巡航点副本；维护人员变更机器人路线后，应在同一维护窗口同步该副本。机器人执行的路线、选点顺序与速度安全逻辑均不因架构分离而改变。

## 定位与地图

机器人重启后会自动启动底盘、雷达、AMCL/Nav2、自动定位恢复和 Edge Gateway，并把实时位置上传至平台。同一地图且未搬动时会自动恢复上次稳定位置；无历史位置时会自动使用激光与地图搜索位置。自动匹配未收敛或机器人被搬运后，平台才会提示操作员在图形化定位界面确认实际位置。

若地图建模显示“位置过期”或“未定位”，无需重启。管理员可在“图书管理 -> 地图建模”点击 **网页确认位置**：可从已导入的巡航点、停靠位、充电区或交付点中选择一个作为初始位置，也可从机器人实际位置向车头方向拖动后确认；AMCL 接收该初始位置后即可恢复实时位置。RViz 的 **2D Pose Estimate** 仍可作为现场维护备用方式；详细操作见机器人端的 `docs/交付启动说明.md`。

## 运维检查

只有部署或排障时才使用：

```bash
systemctl status lr-platform.service
systemctl status lr-robot-runtime.service
```

平台日志：

```bash
cd /home/boreas/project/LR/LR-Web
docker compose -f infra/docker-compose/docker-compose.yml logs --tail=100
```

需要部署新版本时，由部署人员执行：

```bash
cd /home/boreas/project/LR/LR-Web
sudo ./scripts/update-platform.sh
```

## 操作员桌面端

操作员也可安装“智巡机器人调度”桌面端；它加载同一控制站网页，功能和界面不分叉。构建、安装和安全边界见 [桌面端说明](./DESKTOP.zh-CN.md)。
