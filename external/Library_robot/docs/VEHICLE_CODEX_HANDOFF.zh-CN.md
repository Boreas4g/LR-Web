# 车载端 Codex 接入任务书

本文件供运行在真实机器人车载电脑上的 Codex 使用。目标是将已经具备 ROS 2、
底盘、雷达、定位和可选机械臂的机器人，安全接入图书馆本地控制站。

正确的连接拓扑如下：

```text
操作员浏览器 / Electron
        HTTPS + WSS :443
                |
                v
本地控制站（网页、API、PostgreSQL、Redis、EMQX）
                ^
                | MQTT + mTLS :8883，由车端主动发起
                |
车载 Edge Gateway -> 本机 ROS 2 / Nav2 / 底盘 / BookArm
```

车端不得暴露 ROS、DDS、CAN、串口、`/cmd_vel` 或 BookArm API 给局域网。控制站和
用户端都不能直接操作这些接口。

## 0. 强制安全规则

1. 默认假定机器人、机械臂和升降台已接通电源。未获得现场负责人明确授权时，只能
   做只读检查、编译、配置文件写入和网络/TLS 验证。
2. 禁止直接发布 ROS 速度或运动指令，禁止直接调用 CAN/串口，禁止修改物理急停、
   限位、碰撞检测或底盘驱动的安全逻辑。
3. 未通过完整现场验收前，控制站必须保持 `LR_OPERATION_MODE=observe`。车端
   `remote_actuation_enabled` 开关已于 2026-09-26 按现场负责人决定整体移除：
   前端发起的操作即视为现场确认安全，Gateway 收到命令即执行。
4. 禁止关闭 TLS 校验、使用明文 MQTT、复用其他机器人的证书或账号，以及为了排障
   向局域网开放 ROS/DDS/MQTT/底盘控制端口。
5. 不得将私钥、MQTT 密码或 `/etc/lr-robot/` 私有配置写进 Git、补丁、终端输出、
   截图或聊天记录。仅报告文件是否存在、权限是否正确和校验是否通过。
6. 若位置未知、实体急停不可用、运动范围内有人、地图不匹配或硬件反馈异常，停止在
   只读诊断阶段并要求现场人员处理。

## 1. 最终交付标准

在不产生实际运动的前提下，完成并证明：

- 车载机已具备 Ubuntu 24.04、ROS 2 Jazzy、依赖和可构建工作空间。
- 控制站地址、时间、路由、`8883/tcp` 和 TLS 证书链均验证正常。
- 本车拥有独立 `LR-xx` 身份、MQTT 客户端证书、私钥和密码。
- Gateway 私有配置安装在 `/etc/lr-robot/`，权限正确，秘密未回写到仓库。
- 在现场允许启动后，Gateway 能上报真实遥测，控制站将机器人显示为在线。
- 远程动作仍保持关闭，直到单独完成现场动作验收。

最终报告必须包含：代码版本、网络/TLS 结果、配置权限、服务状态、平台在线状态、
当前两个安全开关的状态、是否发生过实际运动，以及尚需哪位人员提供什么信息或授权。

## 2. 先收集现场参数

不得猜测下列信息。缺少任一必填项时，停在相应关卡并向部署人员索取：

| 项目 | 示例 | 要求 |
| --- | --- | --- |
| 机器人 ID | `LR-01` | 必须与平台记录、MQTT 用户名和客户端证书 CN 完全一致。 |
| 控制站主机 | `lr-control.local` 或 `192.168.10.10` | 必须是控制站证书 SAN 中的 DNS/IP；不能填写机器人 IP 或 URL 路径。 |
| 车端交付包 | `/etc/lr-robot/staging/LR-01` | 包含 `mqtt/ca.crt`、`mqtt/LR-01.crt`、`mqtt/LR-01.key`、`mqtt-credentials.env`。 |
| 地图 ID | `MAP-XXXXXXXXXXXX` | 来自控制站已批准地图。 |
| 地图边界 | `min_x max_x min_y max_y` | 来自当前加载地图 YAML、图像尺寸和分辨率。 |
| 服务用户 | `boreas` | 当前脚本默认使用该用户；若不同，先报告，不要静默改变权限。 |
| 现场授权 | 姓名、时间、范围 | 区分只读预检、启动遥测和远程动作验收。 |

控制站必须已经完成：

```text
1. 以 HTTPS :443 和 MQTT/mTLS :8883 部署在本地局域网。
2. 为本车签发独立身份：issue-robot LR-01。
3. 导出本车交付包：export-robot LR-01。
4. 同步 MQTT 用户到 EMQX：sync-emqx LR-01。
5. 在平台中存在对应机器人记录，并确认本车使用的地图 ID。
```

车端不能自行签发可信证书，也不能用共享账号、普通 MQTT 或关闭 TLS 代替上述步骤。

### 当前 `LR-01` 控制站交接参数

截至当前交付版本，`LR-01` 应使用下列已经在控制站登记的参数。它们替换仓库旧本地开发
样例中的 `192.168.8.10` 和 `MAP-F18C1F27DC2E`；旧值不可用于本次控制站。

| 项目 | 当前值 |
| --- | --- |
| 控制站 HTTPS | `https://lr-control.local` |
| 控制站 MQTT/mTLS | `lr-control.local:8883` |
| 控制站 MQTT 证书 SAN | `lr-control.local`、`10.193.5.215` |
| 机器人 ID | `LR-01` |
| 已批准地图 ID | `MAP-A7E3270A2942` |
| 地图边界 | `-4.441 3.879 -1.751 5.209` |
| 初始安全状态 | 控制站 `observe`；车端远程动作开关已移除（前端操作即现场确认） |

车端没有 `/etc/lr-platform/platform.env` 是正常的：该文件及控制站服务只存在于控制站小主机。
车端只需要本车交付包和 `/etc/lr-robot/` 私有配置，且只能主动连接控制站的 `8883/tcp`。

## 3. 确认工作区，且保留现场状态

先确定车端代码布局。仓库完整克隆时，工作区是 `LR/Library_robot`；如果仅部署了
车端子目录，则该目录本身是工作区。使用下面的只读探测，不要假定固定绝对路径：

```bash
REPO_ROOT="$(git -C "$PWD" rev-parse --show-toplevel)"
if [ -d "$REPO_ROOT/Library_robot/src" ]; then
  ROBOT_WORKSPACE="$REPO_ROOT/Library_robot"
  BOOKARM_ROOT="$REPO_ROOT/lingzu-bookarm-py"
elif [ -d "$REPO_ROOT/src" ]; then
  ROBOT_WORKSPACE="$REPO_ROOT"
  BOOKARM_ROOT="$(dirname "$REPO_ROOT")/lingzu-bookarm-py"
else
  echo '找不到 ROS 2 车端工作区，停止。' >&2
  exit 1
fi

printf 'robot workspace: %s\nbookarm root: %s\n' "$ROBOT_WORKSPACE" "$BOOKARM_ROOT"
git -C "$REPO_ROOT" status --short
git -C "$REPO_ROOT" branch --show-current
git -C "$REPO_ROOT" rev-parse --short HEAD
```

若 Git 工作区已有现场修改，先列出并保留。禁止使用 `git reset --hard`、`git checkout --`
或未经确认的 `git pull` 覆盖现场配置。只有维护人员明确确认目标提交和本地改动处理方式
后才更新代码。

## 4. 阶段 A：只读预检

本阶段不得启动、重启或停止可能影响硬件的服务：

```bash
uname -a
timedatectl status
test -r /opt/ros/jazzy/setup.bash && echo 'ROS Jazzy exists'
id boreas
systemctl status lr-robot-runtime.service --no-pager || true
systemctl status lr-bookarm-runtime.service --no-pager || true
```

检查控制站解析和路由。DNS 名称解析失败时，优先修复现场 DNS；只有部署人员确认控制站
证书包含该静态 IP 的 SAN 时才允许使用 IP：

```bash
STATION_HOST='由部署人员提供的控制站主机名或 IP'
getent ahosts "$STATION_HOST"
STATION_IP="$(getent ahostsv4 "$STATION_HOST" | awk 'NR == 1 { print $1 }')"
test -n "$STATION_IP" && ip route get "$STATION_IP"
```

系统时间明显错误时先由现场网络或 NTP 修复。错误时间会让 TLS 正确地拒绝证书，不能靠
跳过证书验证解决。

仅查看当前地图与 Gateway 模板：

```bash
cd "$ROBOT_WORKSPACE"
find maps src -maxdepth 4 -type f \( -name '*.yaml' -o -name '*.pgm' -o -name '*.png' \) | sort
sed -n '1,220p' src/robot_decision/config/edge_gateway.control-station.yaml.example
sed -n '1,220p' src/robot_decision/config/auto_localization.yaml
```

地图边界必须根据真实地图 YAML 计算：

```text
min_x = origin[0]
max_x = origin[0] + image_width_px * resolution
min_y = origin[1]
max_y = origin[1] + image_height_px * resolution
```

不得凭经验扩大边界。平台地图 ID、`active_map_id`、`allowed_map_ids` 和 Nav2 实际
加载的地图必须一致。

## 5. 阶段 B：编译和非动作接入

### 5.1 构建车端软件

只有在 ROS Jazzy 可用时执行。编译不会发出移动命令：

```bash
cd "$ROBOT_WORKSPACE"
source /opt/ros/jazzy/setup.bash
rosdep install --from-paths src --ignore-src -r -y
colcon build \
  --packages-select mobile_robot_nav_bringup dual_laser_merger robot_decision \
  --symlink-install
```

如果该车需要机械臂流程，还需确认 `lingzu-bookarm-py` 存在于工作区同级目录，其 Python
环境、CAN 接口名和设备权限均由现场负责人确认。不要因为导入失败而改变 CAN 参数、设备
权限或使机械臂运动。

### 5.2 验证控制站 MQTT/TLS

该验证只建立 TLS 握手，不会发送机器人命令：

```bash
ROBOT_ID='LR-01'
STATION_HOST='lr-control.local'
DELIVERY_PACKAGE="/etc/lr-robot/staging/$ROBOT_ID"

python3 - "$STATION_HOST" "$DELIVERY_PACKAGE" <<'PY'
import socket
import ssl
import sys
from pathlib import Path

host, package_dir = sys.argv[1:]
package = Path(package_dir)
context = ssl.create_default_context(cafile=package / "mqtt/ca.crt")
context.load_cert_chain(package / "mqtt/LR-01.crt", package / "mqtt/LR-01.key")
with socket.create_connection((host, 8883), timeout=8) as raw_socket:
    with context.wrap_socket(raw_socket, server_hostname=host):
        pass
print("MQTT mTLS handshake verified")
PY
```

Python 会同时校验证书链和主机名/IP SAN；失败时只检查地址、时钟、`8883/tcp` 防火墙、
证书 SAN 和 CA，禁止关闭校验或改用明文 MQTT。

也可先使用安装器的非动作预检。它不会写入 `/etc/lr-robot/`、构建或重启任何服务：

```bash
cd "$ROBOT_WORKSPACE"
sudo ./scripts/install-control-station-gateway.sh \
  --robot-id "$ROBOT_ID" \
  --station-host "$STATION_HOST" \
  --delivery-package "$DELIVERY_PACKAGE" \
  --map-id 'MAP-A7E3270A2942' \
  --map-bounds -4.441 3.879 -1.751 5.209 \
  --preflight
```

### 5.3 安装每台车独立的 Gateway 配置

交付包验证通过后，使用已有脚本写入私有目录。下列命令不带 `--restart`，因此不会重启
车载运行时：

```bash
cd "$ROBOT_WORKSPACE"
sudo ./scripts/install-control-station-gateway.sh \
  --robot-id "$ROBOT_ID" \
  --station-host "$STATION_HOST" \
  --delivery-package "$DELIVERY_PACKAGE" \
  --map-id 'MAP-A7E3270A2942' \
  --map-bounds -4.441 3.879 -1.751 5.209
```

不要手工复制 `mqtt-credentials.env` 的内容。

安装后只检查非敏感字段与权限：

```bash
sudo stat -c '%n %U:%G %a' \
  /etc/lr-robot \
  /etc/lr-robot/mqtt \
  /etc/lr-robot/mqtt/ca.crt \
  "/etc/lr-robot/mqtt/$ROBOT_ID.crt" \
  "/etc/lr-robot/mqtt/$ROBOT_ID.key" \
  /etc/lr-robot/edge-gateway.yaml \
  /etc/lr-robot/auto-localization.yaml \
  /etc/lr-robot/runtime.env
sudo grep -E '^(    (robot_id|mqtt_host|mqtt_port|mqtt_tls_enabled|active_map_id):|      - )' \
  /etc/lr-robot/edge-gateway.yaml
```

预期私钥和私有 YAML 仅对 `root:boreas` 可读。旧配置存在时脚本会创建时间戳备份。
安装器会同时生成 `/etc/lr-robot/auto-localization.yaml`，其 `active_map_id` 与 Gateway
一致，避免定位缓存和上报遥测使用不同地图 ID。

不要将工作空间中 `src/robot_decision/config/edge_gateway.yaml` 或
`src/robot_decision/config/auto_localization.yaml` 的本地开发默认地图 ID 当作现场参数。
运行时安装成功后，应只读取 `/etc/lr-robot/edge-gateway.yaml` 和
`/etc/lr-robot/auto-localization.yaml`；两份文件中的 `active_map_id` 必须均为
`MAP-A7E3270A2942`，且 Gateway 的 `allowed_map_ids` 必须包含该值。

### 5.4 车端地图登记与自动切换

控制站导入地图会创建新的不可变 `MAP-...` 版本。单机交付已弃用车端地图自动同步：
安装器不再执行首次同步，也不会安装 `lr-robot-map-sync.timer`（现网该 timer 已禁用，
历史脚本仅保留作参考）。车端地图改为控制站本机直注册——由现场维护人员用
`register-robot-map.sh` 把已审核的地图登记到本车私有地图库；建图代理 `/mapping/save`
保存新地图时也走同一注册路径自动登记。登记只让地图变为“可选择”，**不会自动切换
正在运行的地图**。

以下命令只复制并校验地图文件、计算边界及写入本地登记表；不会启动 ROS、重启服务或移动
任何机构。首次启用前，先登记当前正在使用的地图；`--map-yaml` 必须是车端已经审核过的
YAML，且同目录有其引用的 PGM：

```bash
cd "$ROBOT_WORKSPACE"
sudo ./scripts/register-robot-map.sh \
  --map-id 'MAP-A7E3270A2942' \
  --map-yaml '/由现场维护人员确认/当前地图.yaml'

# 新场景地图同样在车端本机显式登记。
sudo ./scripts/register-robot-map.sh \
  --map-id 'MAP-新地图ID' \
  --map-yaml '/由现场维护人员确认/新场景地图.yaml'
```

登记文件为 `/etc/lr-robot/map-registry.yaml`，地图副本位于
`/var/lib/lr-robot/maps/MAP-.../`。不用手工编辑登记表，不要以另一个机器人的地图或路径替代。
注册脚本会从 YAML 的 `origin`、PGM 尺寸和 `resolution` 计算实际边界。需要覆盖已登记同 ID
的地图时，先完成审核后再显式加 `--replace`；同 ID 内容变化会影响已有坐标，通常应在控制站
导入为新的地图版本 ID。

登记新地图不会删除旧地图、覆盖同 ID 内容、重启 ROS 或执行机械动作。
同一 `MAP-ID` 的文件哈希出现变化会被拒绝，必须在控制站新建地图版本。Gateway 遥测会回传
`availableMapIds`。控制站“地图建模”页面只会向上报已登记目标地图且处于待命状态的机器人开放
切换。切换时车端必须满足：无导航/手动租约/取还书/机构测试、四轮反馈新鲜且静止、Nav2 的
`/map_server/load_map` 服务可用。成功后会清除旧地图位姿，触发新的全局定位，并要求在新地图上
重新确认初始位置；导航、巡航、取还书和手动控制在定位稳定前保持拒绝。

首次接入还要求当前运行地图已用 `register-robot-map.sh` 登记到本车注册表；安装器
会在重启前验证该 `active_map_id` 位于本车注册表，缺失时停止而不重启运行时。

注册完成但尚未得到“启动并只上报遥测”授权时，到此停止。获得授权后重启车端运行时，使新的
Gateway 和地图登记表生效；不要为地图切换直接发布 `/cmd_vel` 或重启服务来绕过上述检查。

## 6. 阶段 C：经授权后启动并验收遥测

以下步骤会启动或重启底盘、雷达、IMU、导航、自动定位和 Gateway。只有现场负责人明确
确认以下全部条件后才能执行：

```text
[ ] 物理急停可用，且现场人员知道其位置。
[ ] 机器人、机械臂和升降台的运动范围已清场。
[ ] 底盘、雷达、IMU、编码器、限位与碰撞防护已完成基础检查。
[ ] 现场人员允许本次“启动并只上报遥测”。
[ ] 控制站保持 LR_OPERATION_MODE=observe。
```

取得授权后，才可安装或更新 systemd 运行时并启动：

```bash
cd "$ROBOT_WORKSPACE"
sudo ./scripts/install-robot-runtime-autostart.sh
sudo systemctl restart lr-robot-runtime.service
sudo systemctl status lr-robot-runtime.service --no-pager
sudo journalctl -u lr-robot-runtime.service -n 200 --no-pager
```

此时只能确认下列事实，不创建导航、手动控制、取书、归零或机械臂任务：

```bash
cd "$ROBOT_WORKSPACE"
source /opt/ros/jazzy/setup.bash
source install/setup.bash
ros2 node list
ros2 action list | grep -F navigate_to_pose
ros2 topic echo /amcl_pose --once
sudo journalctl -u lr-robot-runtime.service -n 250 --no-pager
```

预期 Gateway 已连接 MQTT 并订阅本车命令主题，控制站“机器人管理”显示该车在线，并显示
真实位置、定位状态和底盘反馈。

## 7. 阶段 D：远程动作验收，仅限明确书面授权

这不是普通接入步骤。遥测验收完成后，必须由现场负责人、设备安全负责人和控制站维护人员
共同确认：

```text
[ ] 物理急停、底盘限速、碰撞保护、机械臂限位和升降台原点/限位均已验证。
[ ] 机器人实际位置已确认，AMCL 稳定，地图 ID 和地图边界与网页一致。
[ ] 首次测试区清场，目标是低速、短距离、可视范围内的空旷路线。
[ ] 有人员可随时按下实体急停；机器人不载书，不在人员密集区。
[ ] 控制站维护人员将 LR_OPERATION_MODE 改为 control 并重启控制站。
[ ] 控制站维护人员将 LR_OPERATION_MODE 改为 control 并重启控制站。
[ ] 现场负责人确认急停、清场、定位、限速与监护条件后重启本车运行时。
```

车端远程动作开关已于 2026-09-26 移除：控制站发出的命令经现场确认后即由本车执行。
取书、还书、机械臂归零和升降台动作需要独立机构验收，不能以
导航成功替代。

申请网页手动控制租约还要求 Gateway 实时上报 `chassisReady: true` 与 `nav2Ready: true`。
其中 `nav2Ready` 表示导航生命周期已启动，`velocity_smoother` 和 `collision_monitor` 组成的
安全速度链可用；它不是地图切换成功的替代证明。若状态为 false，先确认车端当前加载地图、
初始位置确认及导航生命周期，禁止直发 `/cmd_vel` 规避该条件。

## 8. 安全排查顺序

| 现象 | 首先检查 | 禁止做法 |
| --- | --- | --- |
| TLS 校验失败 | 主机名/IP 是否在证书 SAN、时钟、CA、网络和防火墙 | 关闭 TLS 或改为明文 MQTT。 |
| MQTT 被拒绝 | `robot_id`、证书 CN、该车密码、控制站 `sync-emqx LR-xx` | 复制另一台车的账号或证书。 |
| 平台显示离线 | `lr-robot-runtime.service` 日志、DNS、端口、Gateway 参数路径 | 暴露 ROS/MQTT 端口。 |
| 已连接但无位置 | `/amcl_pose`、地图 ID、定位状态、轮速和编码器 | 手推机器人验证里程计或伪造坐标。 |
| 导航被拒绝 | 地图 ID、边界、定位、操作模式和 Gateway 安全状态 | 放宽边界或绕过 Gateway。 |
| 点动已接受但底盘不动 | 先运行 `scripts/diagnose-motion-chain.sh`，核对 `/cmd_vel_nav -> /cmd_vel_smoothed -> /cmd_vel` 的发布/订阅、`velocity_smoother` 与 `collision_monitor` 生命周期、实体急停和底盘驱动日志 | 将 Gateway 改为直发 `/cmd_vel` 或停用碰撞保护。 |
| 机械臂不可用 | `lr-bookarm-runtime.service`、本机 `127.0.0.1:18080`、CAN/设备路径 | 让 BookArm API 监听 `0.0.0.0`。 |

### 点动速度链只读诊断

平台显示 `manual_jog_accepted` 只代表 Gateway 已按固定限值把速度发至 `/cmd_vel_nav`；
它不代表避障链和底盘驱动已实际执行。未获得新的现场动作授权时，在车端工作区运行：

```bash
cd "$ROBOT_WORKSPACE"
./scripts/diagnose-motion-chain.sh
```

该脚本只读取节点、生命周期和话题发布/订阅关系，不会发布 ROS 消息。预期
`velocity_smoother`、`collision_monitor` 处于 `active`，且三段速度话题分别有正确的
上下游端点。若现场负责人已单独授权一次低速、短促点动，可在操作者执行该点动的同时运行：

```bash
./scripts/diagnose-motion-chain.sh --watch
```

脚本仍只观察消息。判读规则：`/cmd_vel_nav` 有非零速度而 `/cmd_vel_smoothed` 无输出，检查
`velocity_smoother`；前两段有输出而 `/cmd_vel` 为零，检查 `collision_monitor`、雷达、TF 和
碰撞状态；`/cmd_vel` 有非零速度但轮速不变，立即停止测试并由现场维护人员检查实体急停、
底盘驱动使能、电机供电和驱动日志。不得为排障绕过碰撞保护。

## 9. 相关文件

- `scripts/install-control-station-gateway.sh`：校验并安装单车 mTLS/Gateway 配置。
- `scripts/register-robot-map.sh`：离线登记一张经过审核的车端本地 ROS 地图。
- `scripts/install-robot-runtime-autostart.sh`：安装车端 systemd 运行时。
- `scripts/install-bookarm-runtime.sh`：安装仅监听 `127.0.0.1` 的本机机械臂服务。
- `docs/交付启动说明.md`：面向部署人员的车端启动说明。
- `src/robot_decision/config/edge_gateway.control-station.yaml.example`：Gateway 参数示例。
