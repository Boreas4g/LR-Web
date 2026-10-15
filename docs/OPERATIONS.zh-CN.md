# 智巡图书馆机器人平台统一部署与操作手册

本文是现场维护人员和操作员的统一入口，按实际顺序说明从空白设备部署控制站、接入第一台或多台机器人、安装操作员桌面端，到日常定位、手动控制、多点巡航、扩容、轮换和排障的完整操作。

除非特别标注，所有生产命令均在对应设备的终端执行。不要在操作员电脑或机器人电脑保存控制站根 CA 私钥，不要把密码、私钥、`platform.env`、`mqtt-credentials.env`、证书导出包提交到 Git 或发送到普通群聊。

相关细节参考：

- [证书与凭据维护](./PKI.zh-CN.md)：PKI 命令的完整说明。
- [桌面端说明](./DESKTOP.zh-CN.md)：桌面端安全和构建说明。
- [启动说明](./STARTUP.zh-CN.md)：架构边界和本地调试说明。

## 快速索引

| 你的角色/当前目标 | 从这里开始 | 完成标志 |
| --- | --- | --- |
| 第一次部署整个现场 | 第 2 节 -> 第 3 节 -> 第 4 节 -> 第 5 节 -> 第 7 节 | 控制站、地图、至少一台车和一个桌面端全部上线 |
| 只新增一台同构机器人 | 第 6 节，然后按第 5 节安装新车 | 新车独立上线，旧车状态不受影响 |
| 操作员安装并使用桌面端 | 第 7 节，然后阅读第 8 节 | 可安全登录、确认定位、控制或调度 |
| 机器人位置不更新或显示未定位 | 第 8.2 节和第 9.2 节 | `/amcl_pose` 与网页坐标时间戳持续更新 |
| 安全手动接管机器人 | 第 8.3 节 | 了解“按住移动、松开即停”和硬件急停优先级 |
| ~~创建多点巡航~~（本版本已下线） | 第 8.4 节 | 功能已移除；巡航点标注见 地图与导航 -> 地图建模，循环取还用 多轮取还书 |
| 更新控制站、日检或排障 | 第 9 节和第 10 节 | 服务、证书、遥测和网络检查通过 |
| 单车凭据可能泄露 | 第 9.5 节 | 新凭据上线，旧交付副本销毁 |

## 1. 先理解系统边界

生产部署至少包含三类设备：

| 设备 | 运行内容 | 允许谁使用 | 不允许做什么 |
| --- | --- | --- | --- |
| 控制站电脑 | 网页、FastAPI、PostgreSQL、Redis、EMQX、证书维护工具 | 部署/维护人员 | 不运行底盘、ROS、CAN 或串口驱动 |
| 机器人车载电脑 | ROS 2、雷达、IMU、AMCL/Nav2、底盘、自动定位、Edge Gateway | 维护人员 | 不向局域网开放 ROS、DDS、串口、CAN 或 `/cmd_vel` |
| 操作员电脑 | 浏览器或“智巡机器人调度”桌面端 | 操作员 | 不保存机器人私钥，不直接访问机器人 IP 或 ROS |

数据流固定如下：

```text
操作员网页/桌面端 -- HTTPS/WSS --> 控制站
机器人 Edge Gateway -- MQTT/mTLS --> 控制站 EMQX
Edge Gateway -- 本机 ROS 2 --> Nav2、AMCL、IMU、雷达、底盘
```

网页和桌面端只向控制站提交高层操作意图。机器人实际速度、避障、限速、命令超时和底盘控制都由车载 Edge Gateway 与 ROS 安全链路处理。遇到危险时，**优先按实体硬件急停**，不要等待网页响应。

## 2. 部署前准备

### 2.1 填写现场参数表

部署前先为每个现场填写并保存下表。示例值仅用于解释，不能直接照抄到生产环境。

| 项目 | 示例 | 说明 |
| --- | --- | --- |
| 控制站 DNS 名称 | `control-station.example` | 推荐使用固定 DNS 名称；所有机器人和桌面端都必须能解析它 |
| 控制站局域网 IP | `192.168.10.10` | 若操作员或机器人会用 IP 访问，签发证书时必须一并填写 |
| HTTPS 地址 | `https://control-station.example` | 非 443 端口时写完整端口，例如 `https://control-station.example:8443` |
| 控制站项目目录 | `/home/boreas/project/LR/LR-Web` | 本手册以此目录为例 |
| 机器人工作空间 | `/home/boreas/Library_robot` | 每台机器人车载电脑均应有该工作空间 |
| 机器人 ID | `LR-01` | 必须匹配 `LR-` 加字母、数字、下划线或连字符，例如 `LR-01`、`LR-A02` |
| 机器人显示名称 | `智巡一号` | 仅用于网页显示，不能含逗号（CSV 限制） |
| 机器人车载机地址 | `192.168.10.101` | 用于维护 SSH 和网页登记中的 Gateway 主机地址 |
| 机器人地图 ID | `MAP-XXXXXXXXXXXX` | 控制站导入地图后生成，必须填入机器人 Gateway 配置 |

### 2.2 网络、时间和防火墙要求

1. 控制站、机器人、操作员电脑应使用稳定的局域网和固定 IP/DNS 记录。
2. 三类设备都应校准时间。证书和定位时间戳依赖正确时间；建议使用单位 NTP 服务。
3. 控制站只开放：
   - 操作员网段到 `443/tcp`（或实际 HTTPS 端口）。
   - 机器人网段到 `8883/tcp`（MQTT/mTLS）。
4. 控制站的 `8080`、`1883`、`5432`、`6379`、`9000`、`9001`、`18083` 不应暴露到局域网。`18083` 仅控制站本机维护脚本使用。
5. 机器人电脑不应向局域网开放 ROS 2/DDS、MQTT、串口、CAN 或底盘控制端口；运行环境应保持 `ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST`。
6. 机器人连接控制站使用的 DNS 名称或 IP，必须与控制站证书中的 SAN 完全匹配。不要以 `--insecure`、浏览器忽略告警或关闭 TLS 校验绕过问题。

### 2.3 首次部署前的检查清单

- 控制站已安装 Docker Engine 与 Docker Compose，维护账号可执行 Docker 或可使用 `sudo`。
- 控制站已安装 `curl`、`jq`、`openssl`、`flock`、`awk`、`tar` 和 `ssh`；Ubuntu/Debian 可执行 `sudo apt install -y curl jq openssl util-linux gawk tar openssh-client`。
- 机器人工作空间已构建成功，`/home/boreas/Library_robot/install/setup.bash` 存在。
- 每台机器人底盘、雷达、IMU、激光雷达和硬件急停经过单车现场测试。
- 已取得机器人实际运行的 ROS 地图 YAML、PGM，以及巡航点配置 `src/robot_decision/config/points.yaml`。
- 现场能通过 SSH 安全维护机器人；机器人私钥交付过程使用受控 SSH/SCP 或加密介质。
- 已决定谁负责根 CA 的离线加密备份。根 CA 丢失后不能为现有车队签发新凭据。

## 3. 控制站首次部署

本节全部在**控制站电脑**执行。

### 3.1 创建机器人清单

实际清单不要放入 Git。创建仅 root 可读的维护目录和 CSV：

```bash
sudo install -d -o root -g root -m 0700 /root/fleet
sudoedit /root/fleet/robots.csv
sudo chmod 0600 /root/fleet/robots.csv
```

内容示例：

```csv
robot_id,name
LR-01,智巡一号
LR-02,智巡二号
```

规则：每行一个机器人；ID 必须唯一；名称不能含逗号；CSV 中有重复或格式错误时，批量签发会在开始前拒绝整个文件，不会留下半完成的凭据。

### 3.2 创建控制站环境文件

```bash
cd /home/boreas/project/LR/LR-Web
sudo install -d -o root -g root -m 0750 /etc/lr-platform
sudo cp infra/control-station.env.example /etc/lr-platform/platform.env
sudo chown root:root /etc/lr-platform/platform.env
sudo chmod 0640 /etc/lr-platform/platform.env
sudoedit /etc/lr-platform/platform.env
```

至少替换以下值。推荐用 `openssl rand -hex 32` 分别生成每个密码或 cookie；生成的十六进制字符串可安全写入 shell 环境文件。

```dotenv
LR_PLATFORM_MODE=production

# 必须是操作员实际打开的控制站“源”，包含协议和非标准端口；不带末尾 /。
LR_CORS_ORIGINS=https://control-station.example

LR_POSTGRES_PASSWORD=替换为独立随机值
LR_MINIO_ROOT_PASSWORD=替换为独立随机值
LR_EMQX_DASHBOARD_USERNAME=替换为非默认管理员名
LR_EMQX_DASHBOARD_PASSWORD=替换为独立随机值
LR_EMQX_NODE_COOKIE=替换为独立随机值

# 默认端口通常保持不变；Dashboard 必须保持 127.0.0.1。
LR_WEB_TLS_PORT=443
LR_MQTT_TLS_PORT=8883
LR_EMQX_DASHBOARD_BIND_ADDRESS=127.0.0.1
LR_EMQX_DASHBOARD_PORT=18083
```

不要手工填写或复用 `LR_MQTT_PASSWORD`、`LR_MQTT_CERTS_DIR`、`LR_WEB_TLS_CERTS_DIR`、`LR_EMQX_BOOTSTRAP_USERS_FILE`。后续 `export-station` 会生成 `/etc/lr-platform/platform-pki.env` 并自动覆盖这些 PKI 变量。

可由脚本为数据库、MinIO、EMQX Dashboard 和 EMQX 节点生成独立随机秘密；它不会把秘密打印到终端或写入 Git：

```bash
sudo ./scripts/initialize-control-station-secrets.sh
sudo cat /root/lr-platform-maintenance/initial-secrets.txt
```

将 `LR_EMQX_DASHBOARD_USERNAME` 和 `LR_EMQX_DASHBOARD_PASSWORD` 保存到受控密码管理器。其余值用于服务内部运行，也应保存一份加密的维护备份，但不需要操作员记忆。读取后立即将该清单离线加密保存或由维护策略销毁；不要发送到聊天群、工单截图或仓库。

### 3.3 创建根 CA、签发站点和车队凭据

以下示例把控制站 DNS 设为 `control-station.example`，IP 设为 `192.168.10.10`。请替换为参数表中的真实值。

```bash
cd /home/boreas/project/LR/LR-Web

# 只在该现场第一次执行。已经存在时脚本会拒绝覆盖。
sudo ./scripts/manage-fleet-pki.sh init-ca

# 第二个 IP 参数可选；机器人或桌面端以 IP 访问时必须提供。
sudo ./scripts/manage-fleet-pki.sh issue-station control-station.example 192.168.10.10

# 为 robots.csv 中全部机器人签发独立客户端证书和独立 MQTT 密码。
sudo ./scripts/manage-fleet-pki.sh batch /root/fleet/robots.csv

# 验证证书链、证书用途、私钥配对和账号清单。
sudo ./scripts/manage-fleet-pki.sh verify

# 将控制站运行时所需文件导出到固定目录；导出包不含 ca.key。
sudo ./scripts/manage-fleet-pki.sh export-station /etc/lr-platform
```

成功后应存在以下文件：

```text
/etc/lr-platform/
  client-ca.crt
  emqx-users.csv
  platform.env
  platform-pki.env
  mqtt-certs/
    ca.crt
    server.crt
    server.key
    control-api.crt
    control-api.key
  web-certs/
    web.crt
    web.key
```

必须同时确认以下安全事实：

```bash
sudo test ! -e /etc/lr-platform/ca.key && echo '控制站目录未包含根 CA 私钥'
sudo ls -ld /var/lib/lr-platform/pki /etc/lr-platform
sudo ls -l /var/lib/lr-platform/pki/ca/ca.key
```

预期是根私钥仅存在于 `/var/lib/lr-platform/pki/ca/ca.key`，且仅 root 可读。立即把该目录中的 `ca.key` 与 `ca.crt` 做离线加密备份；备份介质不能长期放在机器人或操作员电脑上。

### 3.4 同步控制站巡航点快照

控制站只读取自己的已批准巡航点快照 `/etc/lr-platform/cruise-points.yaml`。它不会自动读取机器人工作空间。每次机器人路线变更时必须同步此文件，否则网页显示的巡航点可能和车上实际坐标不一致。

巡航点注入由 `LR_CRUISE_POINTS_ENABLED` 总开关控制（默认 `false`，即导入地图与「同步巡航点」都不会注入任何点）。仓库内的 `infra/cruise-points.example.yaml` 只是开发样例，不会被自动读取；部署好批准快照后把该开关设为 `true`，再到地图建模页用「同步巡航点」（执行前会弹出新增/跳过差异预览）把点预标到地图上。

首次部署可从已确认的机器人取回 `points.yaml`。以 `LR-01` 为例：

```bash
# 在控制站执行；将 IP 改为车载电脑地址。
scp boreas@192.168.10.101:/home/boreas/Library_robot/src/robot_decision/config/points.yaml /tmp/LR-01-points.yaml

# 人工核对文件中的 points 坐标、map 和当前实际路线后，再安装为控制站快照。
less /tmp/LR-01-points.yaml
sudo install -o root -g root -m 0640 /tmp/LR-01-points.yaml /etc/lr-platform/cruise-points.yaml
rm -f /tmp/LR-01-points.yaml
```

当前控制站配置使用一份全局巡航点快照。因此同一控制站下的机器人必须使用兼容的同一地图/路线快照。若不同机器人使用不同地图或路线，不要让它们共用这份文件；应先由维护人员确认地图与路线配置方案，再上线，否则网页地图标注和机器人实际路线会不一致。

### 3.5 封装为控制站一体机并启动

交付时不要让普通用户运行 Docker、填写地址或维护服务。完成前面的证书导出与环境文件配置后，以维护人员身份执行一次一体机安装。该脚本会验证网页证书 SAN、写入唯一设备 ID 和唯一 HTTPS 地址、安装平台开机自启，并启用仅在同一局域网有效的发现广播：

```bash
cd /home/boreas/project/LR/LR-Web
sudo ./scripts/install-control-station-appliance.sh \
  --station-url https://control-station.example \
  --station-name '图书馆机器人控制站'
```

`--station-url` 必须与 `web.crt` 的 DNS 或 IP SAN 一致，且必须是根 HTTPS 地址；不能填写机器人地址、`127.0.0.1`、页面路径或尾随路径。脚本可以安全重复执行，用于更改显示名称或在维护窗口更新控制站地址；地址变更前必须先签发包含新 SAN 的网页证书。

若该电脑此前用 `http://localhost:8000` 或其他 HTTP 端口做过本地调试，一体机安装会自动停止旧的同项目 Docker 容器，再以 HTTPS `443` 和 MQTT/mTLS `8883` 重建。该切换会短暂中断旧调试网页，但不会删除 PostgreSQL、Redis、MinIO 数据卷。

安装程序会同时处理旧本地数据卷的两项必要迁移：将既有 PostgreSQL 的 `library_robot` 账号密码迁移为当前私有配置值，并为 EMQX 的非 root 运行用户授予仅限 TLS 服务私钥和启动账号清单的最小读取权限。它不会删除数据库、重新签发机器人证书或打印任何秘密。

预期 `lr-platform.service` 显示为 `active (exited)`，而 `lr-station-discovery.service` 显示为 `active (running)`。前者由 systemd 管理 Docker Compose，实际容器继续在后台运行；后者每 3 秒向本局域网 IPv4 多播组 `239.255.77.77:39777` 广播站点名称、地址和非敏感设备 ID。

广播只用于显示候选控制站，**不是信任凭据**。桌面端每次选择仍会验证 HTTPS 证书和 `/health` 返回的唯一设备 ID；发现广播被伪造、VPN 禁止多播或跨网段收不到广播时，都不会降低 TLS 安全要求，维护人员可改用手动地址连接。

如只需要传统服务开机自启、暂时不启用一体机发现，也可使用原命令：

```bash
cd /home/boreas/project/LR/LR-Web
sudo ./scripts/install-platform-autostart.sh
sudo systemctl status lr-platform.service --no-pager
```

预期状态为 `active (exited)`。这是正常的：systemd 管理 Docker Compose，实际容器继续在后台运行。

使用 CA 验证 HTTPS 健康接口；不要加 `-k`：

```bash
curl --fail --show-error --cacert /etc/lr-platform/client-ca.crt \
  https://control-station.example/health
```

预期返回 JSON，包含 `"status":"ok"`、`postgres`、`redis`、`mqtt` 等依赖状态。若控制站使用非标准 HTTPS 端口，URL 必须写成 `https://control-station.example:端口/health`。

如复用了已有 EMQX 数据卷，或不确定首次启动是否已导入 `emqx-users.csv`，执行一次账号同步，然后重启平台：

```bash
cd /home/boreas/project/LR/LR-Web
sudo ./scripts/manage-fleet-pki.sh sync-emqx all
sudo systemctl restart lr-platform.service
```

`sync-emqx` 只连接控制站本机 `127.0.0.1:18083`，使用 `platform.env` 中的 EMQX Dashboard 管理员账号。它不会把 Dashboard 暴露到局域网。

### 3.6 检查控制站端口和日志

```bash
sudo ss -ltnp | grep -E ':(443|8883|18083|8080|1883)\b'
```

生产环境预期：`443` 可面向操作员网段，`8883` 可面向机器人网段，`18083`、`8080`、`1883` 应仅为本机监听。查看容器状态或日志时，使用与生产环境变量一致的命令：

```bash
sudo bash -c '
  set -a
  . /etc/lr-platform/platform.env
  . /etc/lr-platform/platform-pki.env
  set +a
  cd /home/boreas/project/LR/LR-Web
  docker compose -f infra/docker-compose/docker-compose.yml -f infra/docker-compose.production.yml ps
  docker compose -f infra/docker-compose/docker-compose.yml -f infra/docker-compose.production.yml logs --tail=100
'
```

## 4. 导入地图、登记机器人和建立地图 ID 对应关系

本节在控制站网页完成，建议先完成再启动生产 Gateway；否则机器人配置中的 `active_map_id` 无法填写正确值。

### 4.1 登录控制站

使用浏览器或桌面端打开：

```text
https://control-station.example
```

当前系统初始管理员为 `admin / 123456`，只用于首次部署检查。**当前版本尚未提供修改初始管理员密码、提升用户角色或禁用注册的完整管理界面/命令，因此不能把带默认口令的实例直接交付到生产网络。** 在这些账号管理能力补齐前，应把本平台限制在隔离验收网或由现场访问控制严格隔离的维护网；不要以“更改前端提示文字”代替真正的账号加固。

页面右上角显示“控制站已连接”表示浏览器已连接控制站实时状态流，不表示浏览器直连机器人。

### 4.2 导入与机器人完全相同的 ROS 地图

1. 打开左侧菜单 **地图与导航 -> 地图建模**。
2. 点击 **导入地图**。
3. 输入易识别名称，例如“302 实验室一层”。
4. 选择同一次建图产生、文件名相互匹配的一对 YAML 和 PGM。
5. 点击 **开始导入**，等待地图预览和地图属性出现。

YAML 的 `image` 字段必须引用本次上传的 PGM 文件名；地图必须与车载 Nav2 正在加载的地图一致。不要把不同场地、不同分辨率或不同原点的 YAML/PGM 混合导入。

### 4.3 获取控制站地图 ID

机器人 Gateway 需要控制站生成的 `MAP-...` ID；页面当前不会在地图属性卡直接显示该值。维护人员可在控制站安全查询。下列命令不会移动机器人：

```bash
BASE='https://control-station.example'
CA='/etc/lr-platform/client-ca.crt'

read -rp '管理员账号：' ADMIN_USER
read -rsp '管理员密码：' ADMIN_PASSWORD; echo
LOGIN_JSON="$(jq -cn --arg username "$ADMIN_USER" --arg password "$ADMIN_PASSWORD" \
  '{username: $username, password: $password}')"
TOKEN="$(curl --fail --silent --show-error --cacert "$CA" \
  -H 'Content-Type: application/json' \
  --data "$LOGIN_JSON" "$BASE/api/v1/auth/login" | jq -er '.token')"

curl --fail --silent --show-error --cacert "$CA" \
  -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/library/maps" \
  | jq -r '.[] | [.id, .name, .version, .yamlFile, .pgmFile] | @tsv'

unset ADMIN_PASSWORD LOGIN_JSON TOKEN
```

输出第一列即地图 ID，例如 `MAP-F18C1F27DC2E`。将其写入现场参数表。不要在终端历史、截图或工单中保留登录令牌。

### 4.4 同步巡航点到新导入地图

确认 `/etc/lr-platform/cruise-points.yaml` 与机器人 `points.yaml` 坐标一致后：

1. 在 **地图与导航 -> 地图建模** 选择刚导入的地图。
2. 点击 **同步巡航点**。
3. 在“全部标注”中确认 `point1`、`point2` 等巡航点数量、坐标和朝向正确。
4. 若需要更易懂的显示名称，可打开标注详情修改名称，但不要改变其物理含义。

安全做法是每次地图或巡航坐标变更都导入新的地图版本、重新同步并现场验证。不要在正在使用的旧地图中直接替换坐标后继续执行任务：同步逻辑会保留已有标注名称，坐标变化可能产生新的标注，容易让操作员误选旧点。

### 4.5 在网页登记机器人

打开 **系统 -> 机器人管理**，点击 **登记机器人**，填写：

| 表单字段 | 应填写的值 |
| --- | --- |
| 机器人 ID | 证书签发的 ID，例如 `LR-01`，必须完全一致 |
| 显示名称 | 例如“智巡一号” |
| Gateway 主机地址 | 机器人车载机固定 IP 或维护 DNS，例如 `192.168.10.101` |

登记后机器人会先显示离线和“等待 Gateway 首次连接”。这属于正常状态。只有机器人车载 Gateway 带着相同 ID 上线并上传遥测后，平台才会显示位置、定位状态和底盘健康。

登记入口只有 **系统 -> 机器人管理** 一处（旧"系统设置 -> 机器人配置"占位页已随单机版移除，路由表中不再存在）。

## 5. 接入一台机器人车载电脑

本节按“先在控制站准备交付包，再在机器人电脑安装”的顺序执行。以下以 `LR-01` 为例；多台车逐台重复，绝不把 `LR-01` 的私钥或密码复制给另一台车。

### 5.1 在控制站导出该车交付包

```bash
cd /home/boreas/project/LR/LR-Web
sudo ./scripts/manage-fleet-pki.sh export-robot LR-01
sudo find /var/lib/lr-platform/pki/exports/robots/LR-01 -maxdepth 2 -type f -printf '%m %p\n'
```

交付包内容：

```text
/var/lib/lr-platform/pki/exports/robots/LR-01/
  mqtt/
    ca.crt
    LR-01.crt
    LR-01.key
  mqtt-credentials.env
```

其中 `LR-01.key` 和 `mqtt-credentials.env` 是秘密。`ca.crt`、`LR-01.crt` 可以公开，但仍建议随交付包一起受控传输。交付包不含根 CA 私钥。

可使用已批准的加密介质，或使用维护 SSH 直接传输到机器人临时目录。以下示例通过受控 SSH 管道传输，不在普通用户目录生成明文私钥；它要求控制站和机器人上的 `boreas` 维护账户已配置 SSH，并且机器人上已由管理员配置仅允许此安装命令免交互 `sudo`。先在机器人上测试该前提：

```bash
ssh boreas@192.168.10.101 'sudo -n true'
```

只有该命令成功时，才执行传输：

```bash
sudo tar -C /var/lib/lr-platform/pki/exports/robots/LR-01 \
  -czf - mqtt mqtt-credentials.env \
  | ssh boreas@192.168.10.101 \
    'sudo install -d -o root -g boreas -m 0700 /etc/lr-robot/staging/LR-01 && sudo tar -xzf - -C /etc/lr-robot/staging/LR-01'
```

如果现场 SSH 策略不同，先由现场管理员在机器人终端创建 `root:boreas`、`0700` 的 `/etc/lr-robot/staging/LR-01`，再使用受控加密介质交付；不要用 `scp` 将私钥先放入 `/home/boreas`，也不要为了方便把私钥目录权限改成 `777`、上传到网盘或发送到非受控聊天工具。

### 5.2 在机器人电脑安装 MQTT 证书

以下命令在**机器人车载电脑**执行：

```bash
ROBOT_ID='LR-01'
STAGING="/etc/lr-robot/staging/$ROBOT_ID"

sudo test -r "$STAGING/mqtt/ca.crt"
sudo test -r "$STAGING/mqtt/$ROBOT_ID.crt"
sudo test -r "$STAGING/mqtt/$ROBOT_ID.key"
sudo test -r "$STAGING/mqtt-credentials.env"

sudo install -d -o root -g boreas -m 0750 /etc/lr-robot/mqtt
sudo install -o root -g boreas -m 0644 "$STAGING/mqtt/ca.crt" /etc/lr-robot/mqtt/ca.crt
sudo install -o root -g boreas -m 0644 "$STAGING/mqtt/$ROBOT_ID.crt" "/etc/lr-robot/mqtt/$ROBOT_ID.crt"
sudo install -o root -g boreas -m 0640 "$STAGING/mqtt/$ROBOT_ID.key" "/etc/lr-robot/mqtt/$ROBOT_ID.key"
sudo ls -ld /etc/lr-robot/mqtt
sudo ls -l /etc/lr-robot/mqtt
```

预期目录权限为 `root:boreas`、`0750`；私钥权限为 `root:boreas`、`0640`。不要把私钥设置为所有用户可读。

### 5.3 创建并填写 Gateway 私有配置

在机器人电脑执行：

```bash
cd /home/boreas/Library_robot
sudo cp src/robot_decision/config/edge_gateway.control-station.yaml.example \
  /etc/lr-robot/edge-gateway.yaml
sudo chown root:boreas /etc/lr-robot/edge-gateway.yaml
sudo chmod 0640 /etc/lr-robot/edge-gateway.yaml
sudoedit /etc/lr-robot/edge-gateway.yaml
```

必须修改以下内容，所有值都来自前面记录的现场参数：

```yaml
edge_gateway_node:
  ros__parameters:
    robot_id: "LR-01"

    # 必须是签发站点证书时使用的 DNS 名称，或当时写入 SAN 的 IP。
    mqtt_host: "control-station.example"
    mqtt_port: 8883
    mqtt_protocol_version: "3.1.1"
    mqtt_tls_enabled: true
    mqtt_ca_file: "/etc/lr-robot/mqtt/ca.crt"
    mqtt_client_cert_file: "/etc/lr-robot/mqtt/LR-01.crt"
    mqtt_client_key_file: "/etc/lr-robot/mqtt/LR-01.key"

    # 从 /etc/lr-robot/staging/LR-01/mqtt-credentials.env 读取并填写。
    mqtt_username: "LR-01"
    mqtt_password: "此处填写该车独立密码"

    # 从第 4.3 节查到的控制站地图 ID 填写；两处必须相同。
    allowed_map_ids:
      - "MAP-F18C1F27DC2E"
    active_map_id: "MAP-F18C1F27DC2E"
```

只在 root 终端读取密码文件，并在编辑后关闭终端或清理屏幕：

```bash
sudo cat /etc/lr-robot/staging/LR-01/mqtt-credentials.env
```

不要把 `mqtt-credentials.env` 的内容复制回工作空间 YAML、Git、截图或普通维护记录。

其余地图边界字段也必须与车载 Nav2 地图一致：

```yaml
    map_min_x: 地图原点x
    map_max_x: 地图原点x + 地图宽度px × 分辨率
    map_min_y: 地图原点y
    map_max_y: 地图原点y + 地图高度px × 分辨率
    max_abs_yaw: 6.284
```

地图原点、宽高与分辨率可从同一份 ROS 地图 YAML 和网页“地图属性”取得。边界不应比实际地图更窄，否则 Gateway 会拒绝合法目标；也不应随意扩大到未知区域。

### 5.4 设置车载服务开机自启并启动

仍在机器人电脑执行：

```bash
cd /home/boreas/Library_robot
sudo ./scripts/install-robot-runtime-autostart.sh

sudo sh -c 'printf "%s\n" \
  "LR_GATEWAY_PARAMS_FILE=/etc/lr-robot/edge-gateway.yaml" \
  "LR_ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST" \
  > /etc/lr-robot/runtime.env'
sudo chown root:boreas /etc/lr-robot/runtime.env
sudo chmod 0640 /etc/lr-robot/runtime.env

sudo systemctl restart lr-robot-runtime.service
sudo systemctl status lr-robot-runtime.service --no-pager
```

车载服务启动顺序为：导航与 AMCL、独立 IMU 守护进程、自动定位恢复、Edge Gateway。IMU 驱动退出后会自动重启；机器人电脑通电并且 `lr-robot-runtime.service` 已启用时，会持续启动 IMU、定位和 Gateway，不需要操作员手工打开 ROS。

查看只读日志：

```bash
sudo journalctl -u lr-robot-runtime.service -n 200 --no-pager
sudo journalctl -u lr-robot-runtime.service -f
```

正常联网后日志应出现 Gateway 已连接 MQTT/订阅命令主题的含义。若 `mqtt_host`、证书 SAN、账号密码或 8883 网络不正确，Gateway 不会上线；不要通过关闭 TLS 校验解决。

### 5.5 清理临时交付文件并确认网页上线

确认 `/etc/lr-robot/mqtt/` 和 `/etc/lr-robot/edge-gateway.yaml` 已正确安装后，删除临时目录：

```bash
sudo rm -rf /etc/lr-robot/staging/LR-01
```

回到控制站网页的 **系统 -> 机器人管理**。正常情况下，该车会从“未连接”变成在线状态，并逐步显示地图、位置、定位状态、健康状态和最近更新时间。若网页仍离线，先按第 10 节排查，不要重复签发证书或随意重启底盘。

## 6. 多台机器人批量接入

当新车结构相同，只需重复每车独立步骤：

1. 在 `/root/fleet/robots.csv` 增加一行，或单独签发：

   ```bash
   cd /home/boreas/project/LR/LR-Web
   sudo ./scripts/manage-fleet-pki.sh issue-robot LR-03 智巡三号
   sudo ./scripts/manage-fleet-pki.sh export-robot LR-03
   ```

2. 在网页 **系统 -> 机器人管理** 登记相同的 `LR-03` 和其 Gateway 地址。
3. 使用第 5 节方法把 `LR-03` 的独立证书和密码安装到这台车，配置 `robot_id: "LR-03"`。
4. 控制站平台已经运行时，将新账号同步进 EMQX：

   ```bash
   sudo ./scripts/manage-fleet-pki.sh sync-emqx LR-03
   ```

5. 重启新车的 `lr-robot-runtime.service`，确认该车独立上线。

不要复制任意已有车辆的 `*.key`、`mqtt_password` 或 `robot_id`。每辆车必须持有自己签发的客户端证书、账号密码和配置文件。EMQX ACL 已限制机器人只能订阅自己的命令主题、发布自己的事件和遥测主题。

## 7. 操作员桌面端安装与首次使用

桌面端显示的是控制站提供的原网页，所有图形界面和功能与浏览器一致；它不包含 ROS、MQTT、机器人证书、底盘接口或控制站私钥。

### 7.1 维护人员准备交付文件

从控制站交付两类文件：

1. Linux 安装包：`release/desktop/LR-Robot-Dispatch-1.0.0-amd64.deb`。
2. 控制站 CA 公钥：`/etc/lr-platform/client-ca.crt`。

CA 公钥不含私钥，可以交付给操作员电脑用于信任控制站；绝不能交付 `/var/lib/lr-platform/pki/ca/ca.key`。

### 7.2 Linux 操作员电脑安装

在操作员电脑执行。先完全退出桌面端，再由维护人员把 `install-desktop-ca.sh` 和 `client-ca.crt` 放到本机受控目录，例如 `~/Downloads/lr-platform/`：

```bash
cd ~/Downloads/lr-platform
sudo apt install libnss3-tools
sudo ./install-desktop-ca.sh ./client-ca.crt
sudo apt install ./LR-Robot-Dispatch-1.0.0-amd64.deb
```

可先验证信任链与网络：

```bash
curl --fail --show-error https://control-station.example/health
```

然后从系统应用菜单打开 **智巡机器人调度**，或执行：

```bash
lr-robot-dispatch-desktop
```

首次启动时桌面端会自动显示同一局域网内发现的控制站。操作员只需点击“图书馆机器人控制站”并等待证书与身份验证完成，不需要知道 IP、端口、Docker 或 MQTT。

若现场网络禁止多播、操作员电脑在其他网段或 VPN，候选列表会为空。这时由维护人员在“未发现时，由维护人员输入地址”中输入控制站完整地址，例如：

```text
https://control-station.example
```

若控制站使用非标准端口，必须输入端口，例如 `https://control-station.example:8443`。局域网远程操作不允许使用 `http://`；`http://localhost:8000` 和 `http://127.0.0.1:8000` 只用于控制站本机开发调试。

桌面端会调用 `/health` 验证控制站证书和设备身份后再进入登录页，并记住该设备 ID。后续即使同一地址被其他设备占用，身份 ID 不一致也会拒绝连接。证书不受信任、过期、主机名不匹配或网络不可达时会拒绝连接，不存在“忽略证书错误”选项。

### 7.3 Windows 操作员电脑

将同一份 `client-ca.crt` 导入 **本地计算机 -> 受信任的根证书颁发机构**，然后安装由 Windows 构建机生成的 NSIS 安装包。Windows 安装包在 Windows 构建机执行以下命令生成：

```bash
npm run desktop:build:win
```

不要把 CA 私钥导入 Windows 证书库。

## 8. 日常地图定位与手动控制（多点巡航已下线，见 8.4）

### 8.1 每天开机后的操作顺序

1. 先打开控制站，确认健康接口和网页可访问。
2. 再给机器人车载电脑与底盘上电；等待 `lr-robot-runtime.service` 自动启动。
3. 打开桌面端或浏览器登录，确认右上角为“控制站已连接”。
4. 在 **系统 -> 机器人管理** 或 **地图与导航 -> 地图建模** 查看机器人是否在线、位置时间是否持续刷新、底盘健康是否通过。
5. 若机器人被搬动、位置过期或显示未定位，先完成第 8.2 节的定位确认；在可靠定位前不要创建导航或巡航任务。

### 8.2 网页确认机器人初始位置

此操作只向 AMCL 发送初始位置，**不会让机器人移动**。

1. 确保你能看到机器人真实位置，机器人已加载与网页选中地图相同的 ROS 地图。
2. 打开 **地图与导航 -> 地图建模**，选择正确地图。
3. 点击右上角 **网页确认位置**。
4. 选择机器人。
5. 二选一：
   - 从已有停靠位、充电区、交付点或巡航点选择实际附近的点；或
   - 选择“手动在地图上选择位置和朝向”，在地图上从机器人实际位置向车头方向拖动。
6. 核对弹窗显示的 ROS 坐标和车头朝向，点击 **确认并发送**。
7. 等待地图右侧实时机器人状态由“确认位置中”变为可靠定位。未收敛时不要反复发送，应检查激光、地图、AMCL 与真实位置是否匹配。

### 8.3 安全手动控制

入口：**系统 -> 机器人控制**（地图建模页也提供同一套直控按钮）。

手动控制已无租约与角色门槛：网页点动指令经 control-api 直发 MQTT，Gateway 按固定限值执行。开始控制前必须满足：

- 现场通道已清空，维护人员站在硬件急停可触及位置。
- 机器人不是离线、故障、充电或软件急停锁定状态。
- 底盘健康检查显示通过。

操作步骤：

1. 在目标机器人卡片点击 **手动控制**；若机器人正在导航，第一段点动到达时 Gateway 会先取消导航再执行。
2. 三种输入方式任选其一：
   - **页面按钮**：按住“前进/后退/左转/右转”，松开立即发停止。
   - **键盘**：按住 `W` 或上箭头前进，`S` 或下箭头后退，`A` 或左箭头左转，`D` 或右箭头右转；空格立即停止。
   - **手柄**：点击 **启用手柄**，使用左摇杆前后和转向，死区为 18%。
3. 操作结束点击 **立即停止**，再点击 **结束控制**。

这是“按住移动、松开停止”，不是点一次持续行驶。网页约每 350 ms 刷新点动命令，单条运动命令约 500 ms；Gateway 对单条命令还有最大 750 ms 限制。键盘优先于页面按钮，页面按钮优先于手柄。**手动点动不经过碰撞监控与速度平滑**（驾驶员目测负责避让）；导航任务仍走完整避障安全链。

以下情况都会请求发送零速度或结束控制：松开按钮/按键、按空格、页面失焦、切换页面、最小化、浏览器隐藏、命令超时、网络中断。车端另有约 1 秒看门狗：网页停止发令后 Gateway 自动抱死停车。无论任何网页状态，危险时始终优先使用硬件急停。

#### 软件紧急停止与硬件急停的区别

- **请求紧急停止**：网页向 Gateway 发送软件急停请求，现场仍应立即确认并优先按硬件急停。
- **解除软件急停锁定**：仅系统管理员可在现场确认机器人静止、硬件急停已释放、底盘健康通过后点击。它不会解除实体硬件急停，不会发送速度，也不能修复底盘/传感器故障。

### 8.4 创建多点巡航（本版本已下线）

**该功能已随单机版移除**：旧"机器人调度 -> 调度总台 -> 多点巡航"页面与
`POST /dispatch/cruise` 创建端点均已删除，路由表中不再有调度总台。本节操作步骤
仅作历史存档，当前版本无法创建多点巡航任务。

现有替代能力：

- 巡航点**标注**仍保留：在 **地图与导航 -> 地图建模** 中以当前位置标注巡航点，
  并用"同步巡航点"（sync-cruise）把控制站巡航点快照落到地图标注；
- 取还书类的循环作业用 **作业 -> 多轮取还书**（`/circulation/multi-round`）。

~~创建前必须确认：机器人可靠定位、机器人当前地图 ID 与所选地图相同、路线已现场验证、通道无人且没有手动接管。创建条件不满足时平台会拒绝任务。~~

历史操作步骤（存档）：选择执行机器人（故障、离线或手动接管中的不可选）→ 选择与
机器人当前地图一致的地图版本 → 选 2–50 个巡航点并调整执行顺序 → 选 1 至 10 轮
循环 → 现场确认无障碍后创建 → 在任务队列观察进度（Gateway 在车端按顺序逐点调用
Nav2，不由网页直接持续发布速度）。

## 9. 日常维护操作

### 9.1 控制站日检

在控制站执行：

```bash
sudo systemctl is-active lr-platform.service
sudo systemctl is-active lr-station-discovery.service
sudo ./scripts/manage-fleet-pki.sh verify
curl --fail --show-error --cacert /etc/lr-platform/client-ca.crt \
  https://control-station.example/health
```

`verify` 会检查本地 CA、控制站证书、机器人证书、私钥配对、证书用途、24 小时内过期风险及账号清单；它不会重启机器人或发送运动命令。

### 9.2 机器人日检

在每台机器人执行：

```bash
sudo systemctl is-active lr-robot-runtime.service
sudo journalctl -u lr-robot-runtime.service -n 100 --no-pager
```

只读检查 AMCL 是否仍发布位姿（不驱动机器人）：

```bash
cd /home/boreas/Library_robot
source /opt/ros/jazzy/setup.bash
source install/setup.bash
ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST ros2 topic echo /amcl_pose --once
```

应看到带时间戳的位置数据。若机器人被推动而 `/amcl_pose` 时间戳和坐标长期不变，应先检查 AMCL、激光、里程计、IMU 和地图是否都在运行，再检查 Gateway/控制站遥测链路；不要仅刷新网页或重新签发证书。

### 9.3 更新平台版本

在维护窗口、控制站执行：

```bash
cd /home/boreas/project/LR/LR-Web
sudo ./scripts/manage-fleet-pki.sh verify
sudo ./scripts/update-platform.sh
sudo systemctl status lr-platform.service --no-pager
```

更新不会替换 `/etc/lr-platform/platform.env`、`platform-pki.env` 或 PKI 目录。更新后重新检查 `/health` 和机器人在线状态。
若本次维护还包含经验证的 EMQX 镜像升级，在私有 `platform.env` 设置新的 `LR_EMQX_IMAGE`，并仅在该维护窗口执行 `LR_PLATFORM_PULL_BROKER=true sudo ./scripts/update-platform.sh`。

### 9.4 新增机器人

重复第 5 节，并在控制站增加账号后同步 Broker：

```bash
cd /home/boreas/project/LR/LR-Web
sudo ./scripts/manage-fleet-pki.sh issue-robot LR-03 智巡三号
sudo ./scripts/manage-fleet-pki.sh export-robot LR-03
sudo ./scripts/manage-fleet-pki.sh sync-emqx LR-03
```

随后在网页登记 `LR-03`，在该车安装自己的交付包并重启其车载服务。新增车不应影响已有车辆。

### 9.5 轮换一台机器人凭据

适用于机器人私钥、密码或交付介质可能泄露。安排维护窗口，并严格按顺序执行：

```bash
cd /home/boreas/project/LR/LR-Web

# 1. 生成新凭据；旧凭据会归档到 PKI archive 目录。
sudo ./scripts/manage-fleet-pki.sh rotate-robot LR-01

# 2. 导出新包，安全传给 LR-01；先安装新证书和 YAML 密码，但暂不重启车载服务。
sudo ./scripts/manage-fleet-pki.sh export-robot LR-01

# 3. 切换 Broker 中的密码，然后立即重启 LR-01 车载服务。
sudo ./scripts/manage-fleet-pki.sh sync-emqx LR-01
```

接着在机器人执行：

```bash
sudo systemctl restart lr-robot-runtime.service
sudo systemctl status lr-robot-runtime.service --no-pager
```

确认 `LR-01` 在网页恢复在线后，销毁临时交付副本。轮换只会影响指定机器人；不要轮换控制站 CA，除非已进入全车队整体证书迁移维护窗口。

### 9.6 控制站证书临近过期、根 CA 丢失或泄露

- 叶子证书默认有效约 825 天；`verify` 会提示 24 小时内到期，但应至少在到期前数周制定维护计划。
- 根 CA 默认有效约 10 年。根 CA 私钥丢失后，现有机器人仍可能继续工作，但无法签发新证书或正常轮换；应从离线备份恢复。
- 根 CA 私钥泄露属于重大安全事件：建立新 CA、重新签发控制站和所有机器人证书、重新部署操作员 CA 信任、在受控窗口整体切换。不要只轮换单车密码来代替 CA 迁移。

## 10. 故障排查表

| 现象 | 先检查什么 | 安全处理方式 |
| --- | --- | --- |
| 桌面端提示证书不受信任 | 访问地址是否与签发时 DNS/IP 一致；操作员电脑是否安装 `client-ca.crt`；证书是否过期 | 重新安装 CA 或签发正确 SAN 的站点证书；不要忽略证书错误 |
| 桌面端未发现控制站 | `lr-station-discovery.service`、操作员和控制站是否在同一二层局域网、组播是否被交换机/VPN 阻断 | 用手工地址作为维护兜底；不要为了发现功能关闭 HTTPS 校验 |
| 桌面端无法连接控制站 | `https://控制站/health`、控制站服务、网络、443 防火墙 | 用 `curl` 验证；查看控制站日志；不要改为远程 HTTP |
| 网页显示“正在重连控制站” | 浏览器到控制站 WSS、登录令牌、控制站 API | 刷新并重新登录；检查 `/health` 和 Docker 容器 |
| 机器人始终离线 | `lr-robot-runtime.service`、Gateway 日志、DNS、8883、防火墙、证书和账号 | 检查 `mqtt_host`、证书路径/权限、`robot_id` 和 Broker 同步；不要关闭 mTLS |
| 机器人在线但不能导航/巡航 | 定位状态、`mapId`、地图一致性、巡航点、车端边界 | 先网页确认位置；核对 Gateway 的 `active_map_id` 与所选地图 ID |
| 推动车辆坐标不变 | `/amcl_pose` 是否更新，激光/里程计/IMU/AMCL 是否正常，Gateway 是否上传遥测 | 按第 9.2 节做只读检查；不要用手动控制来“刷新位置” |
| 手动控制没有运动 | 是否真正按住输入；页面是否失焦；硬件急停；底盘/碰撞监控；车端 runtime 是否已重启到免租约版本 | 先按实体急停确认安全，再检查 Gateway 日志；不重复点击制造连续指令 |
| 新车证书已安装但 MQTT 被拒绝 | `sync-emqx LR-xx` 是否完成；账号密码是否属于该车；证书 CN/ID 是否一致 | 控制站同步指定账号，重启该车 Gateway；不要复制其他车凭据 |
| `sync-emqx` 登录失败 | `/etc/lr-platform/platform.env` 的 EMQX Dashboard 用户名密码；18083 仅本机监听 | 在控制站本机检查环境文件与服务；不要开放 18083 到局域网 |
| 巡航点坐标和实际不一致 | 控制站快照与机器人 `points.yaml` 是否相同；地图是否是同一版 | 暂停任务，导入新地图版本、同步点位并现场验证后再使用 |

## 11. 禁止事项

- 不要把 `ca.key`、机器人 `*.key` 或 `mqtt-credentials.env` 复制到操作员电脑、Git、网盘或聊天群。
- 不要通过 `curl -k`、浏览器“继续访问”、Electron `--no-sandbox`、关闭 TLS 校验或把机器人私钥设为全员可读来解决问题。
- 不要让浏览器、桌面端直接连接机器人 ROS、DDS、串口、CAN 或 `/cmd_vel`。
- 不要在人员、障碍物或书架附近做未确认的手动控制或巡航测试。
- 不要在不一致的地图、路线点或地图 ID 条件下执行导航和巡航。
- 不要删除机器人记录来解决短暂离线；删除会清理关联任务、告警和待执行命令，且不可恢复。
- 不要在没有离线加密备份的情况下移动、删除或覆盖根 CA 私钥。

## 12. 交付验收清单

全部通过后，现场应记录日期、维护人员、控制站地址、每台车 ID、地图 ID、证书到期时间和测试结论。

- [ ] 控制站 `lr-platform.service` 开机自启且 `/health` 通过 CA 校验。
- [ ] 控制站 `lr-station-discovery.service` 开机自启；同一局域网的桌面端能发现并在验证后连接该站。
- [ ] 仅 443 和 8883 按网络边界开放，Dashboard/API 等管理端口不对局域网暴露。
- [ ] 根 CA 已做离线加密备份，控制站/机器人/桌面端均没有 `ca.key`。
- [ ] 每台机器人持有独立 `LR-xx.crt`、`LR-xx.key`、MQTT 密码和相同 ID 的 Gateway 配置。
- [ ] 每台机器人已在 **系统 -> 机器人管理** 登记，Gateway 上线并持续上传位置时间戳。
- [ ] 网页导入的地图、机器人 Nav2 地图、Gateway `active_map_id` 和 `allowed_map_ids` 一致。
- [ ] 控制站巡航点快照与机器人 `points.yaml` 一致，并在网页地图中完成巡航点同步和现场核对。
- [ ] 操作员电脑已信任 `client-ca.crt`，桌面端可访问控制站且不能绕过证书错误。
- [ ] 已在空旷安全区域验证：网页确认位置、按住式手动控制、空格停止、页面失焦停止、软件急停流程和至少一条多点巡航路线。
