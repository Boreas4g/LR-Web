# 车队证书与凭据维护

> 如需按实际设备顺序完成控制站、机器人和桌面端部署，请先阅读 [统一部署与操作手册](./OPERATIONS.zh-CN.md)。本文提供 PKI 专项参考。

生产环境中，控制站、Control API 和每台机器人使用不同的 MQTT 客户端证书及独立密码。根 CA 私钥只保留在控制站的 PKI 维护目录，绝不复制到机器人、桌面端或导出包；创建后还应保留离线加密备份。

脚本入口：`scripts/manage-fleet-pki.sh`。所有命令都应由控制站维护人员通过 `sudo` 执行；默认维护目录为 `/var/lib/lr-platform/pki`，目录和私钥权限均为仅 root 可读。

## 首次部署

准备机器人清单。名称不能包含逗号：

```csv
robot_id,name
LR-01,智巡一号
LR-02,智巡二号
```

建议将实际清单保存在 root 可读的维护目录，不要把客户名称和凭据提交到 Git。

在控制站项目目录执行：

```bash
cd /home/boreas/project/LR/LR-Web

# 仅第一次执行：创建 10 年维护根 CA。立即把 ca.key 离线加密备份。
sudo ./scripts/manage-fleet-pki.sh init-ca

# 站点 DNS 必须是机器人实际连接 mqtt_host；第二个参数可选，为控制站局域网 IP。
sudo ./scripts/manage-fleet-pki.sh issue-station control-station.example 192.168.10.10

# 批量为机器人签发客户端证书和独立 Broker 密码。
sudo ./scripts/manage-fleet-pki.sh batch /root/fleet/robots.csv

# 对证书链、用途、私钥配对、即将过期状态和账号清单做本地校验。
sudo ./scripts/manage-fleet-pki.sh verify

# 输出控制站运行文件。不会包含 ca.key。
sudo ./scripts/manage-fleet-pki.sh export-station /etc/lr-platform
```

以上导出会生成：

```text
/etc/lr-platform/
  mqtt-certs/{ca.crt,server.crt,server.key,control-api.crt,control-api.key}
  web-certs/{web.crt,web.key}
  client-ca.crt
  emqx-users.csv
  platform-pki.env
```

`platform-pki.env` 自动设置证书目录、EMQX 用户文件及 Control API 的 MQTT 账号密码。平台的启动、停止、更新和 systemd 安装脚本会在生产模式自动加载它；维护人员只需在 `/etc/lr-platform/platform.env` 填写非 PKI 的配置，例如 CORS、数据库密码、EMQX Dashboard 管理员账号和随机节点 cookie。

`web.crt` 既包含控制站 DNS，也包含传入的 IP SAN；`server.crt` 另包含 Docker 内部 DNS `emqx`。因此网页、Control API 与机器人都能进行主机名校验，不需要关闭 TLS 验证。

### 修正控制站 DNS 或 IP

若首次签发时漏填 IP、控制站地址改变，或需要增加新的证书 SAN，不要删除 PKI 目录、更不要重新执行 `init-ca`。使用 `rotate-station` 重签**控制站叶子证书**即可；根 CA 与所有机器人证书保持不变：

```bash
# 例如网页和桌面端将通过 192.168.8.10 访问控制站。
sudo ./scripts/manage-fleet-pki.sh rotate-station lr-control-station 192.168.8.10
sudo ./scripts/manage-fleet-pki.sh export-station /etc/lr-platform
sudo systemctl restart lr-platform.service
```

旧控制站叶子凭据会归档在 `/var/lib/lr-platform/pki/archive/station/`。若机器人继续使用 `lr-control-station` 作为 `mqtt_host`，其配置无需修改；若切换为 IP，则机器人也必须改用已写入 SAN 的 `192.168.8.10`，并在维护窗口重启 Gateway。

## 交付一台机器人

```bash
cd /home/boreas/project/LR/LR-Web
sudo ./scripts/manage-fleet-pki.sh export-robot LR-01
```

默认交付包在 `/var/lib/lr-platform/pki/exports/robots/LR-01`：

```text
mqtt/{ca.crt,LR-01.crt,LR-01.key}
mqtt-credentials.env
```

交付包不含根 CA 私钥，但包含机器人私钥和密码；只能通过受控加密介质或 SSH/SCP 传给对应机器人，传输完成后清理临时副本。在机器人上安装证书：

```bash
sudo install -d -o root -g boreas -m 0750 /etc/lr-robot/mqtt
sudo install -o root -g boreas -m 0644 mqtt/ca.crt /etc/lr-robot/mqtt/ca.crt
sudo install -o root -g boreas -m 0644 mqtt/LR-01.crt /etc/lr-robot/mqtt/LR-01.crt
sudo install -o root -g boreas -m 0640 mqtt/LR-01.key /etc/lr-robot/mqtt/LR-01.key
```

从交付包的 `mqtt-credentials.env` 读取 `mqtt_username` 和 `mqtt_password`，写入该车受保护的 `/etc/lr-robot/edge-gateway.yaml`。同时确认以下字段与签发时一致：

```yaml
robot_id: "LR-01"
mqtt_host: "control-station.example"
mqtt_port: 8883
mqtt_tls_enabled: true
mqtt_ca_file: "/etc/lr-robot/mqtt/ca.crt"
mqtt_client_cert_file: "/etc/lr-robot/mqtt/LR-01.crt"
mqtt_client_key_file: "/etc/lr-robot/mqtt/LR-01.key"
mqtt_username: "LR-01"
```

完成后重启车载服务并在调度页面确认该车上线：

```bash
sudo systemctl restart lr-robot-runtime.service
```

证书签发只解决传输身份认证，不会自动在业务数据库创建机器人。管理员还应在网页的“系统设置 -> 机器人管理”添加同一 `LR-01` ID，完成名称、地图和 Gateway 信息配置；Control API 会忽略未注册机器人上传的遥测，避免陌生设备出现在调度页面。

## 增加与轮换机器人

新增车只需要签发、导出、安装，然后将新增账号同步到已运行的 EMQX：

```bash
sudo ./scripts/manage-fleet-pki.sh issue-robot LR-03 智巡三号
sudo ./scripts/manage-fleet-pki.sh export-robot LR-03
sudo ./scripts/manage-fleet-pki.sh sync-emqx LR-03
```

`sync-emqx` 只访问控制站本机 `127.0.0.1:18083` 的 EMQX Dashboard API；它使用 `/etc/lr-platform/platform.env` 中的 `LR_EMQX_DASHBOARD_USERNAME` 和 `LR_EMQX_DASHBOARD_PASSWORD`。生产 Compose 将 Dashboard 明确发布到 `127.0.0.1`，没有向局域网开放。首次部署不需要此命令，因为 `emqx-users.csv` 会在容器首次启动时导入。

如果控制站复用了已有 EMQX 数据卷，或不确定首次导入是否完成，在平台启动后执行一次 `sudo ./scripts/manage-fleet-pki.sh sync-emqx all`，然后重启平台服务，使 Control API 以已同步账号重新建立 MQTT 连接。

轮换某台车时，按以下顺序操作，车只会在同步密码与重启之间短暂离线：

```bash
# 1. 生成新凭据，旧私钥自动归档在 PKI 目录；此时在线车不受影响。
sudo ./scripts/manage-fleet-pki.sh rotate-robot LR-01

# 2. 导出并先把新 ca.crt、LR-01.crt、LR-01.key 和 yaml 密码部署到车上，暂不重启。
sudo ./scripts/manage-fleet-pki.sh export-robot LR-01

# 3. 在维护窗口切换 Broker 密码，随后立即重启该车载服务并确认上线。
sudo ./scripts/manage-fleet-pki.sh sync-emqx LR-01
```

轮换不会修改其他车的凭据。EMQX 对机器人还要求账号密码，更新密码会使遗失的旧证书无法再连接；旧交付包与归档文件应按现场安全制度销毁或加密保管。

## 日常检查与恢复

```bash
# 每次批量操作或更新前执行。
sudo ./scripts/manage-fleet-pki.sh verify

# 查看完整帮助。
sudo ./scripts/manage-fleet-pki.sh help
```

根 CA 丢失后不能为现有信任链签发新证书，因此必须在创建后离线加密备份 `/var/lib/lr-platform/pki/ca/ca.key` 与 `ca.crt`。不要备份到机器人电脑或用户桌面端。若根 CA 泄露，需要建立新 CA、重新签发控制站和全部机器人凭据，并在受控维护窗口整体切换。
