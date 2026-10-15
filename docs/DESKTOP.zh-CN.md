# 智巡机器人调度桌面端

> 操作员交付、控制站部署和日常控制的完整流程见 [统一部署与操作手册](./OPERATIONS.zh-CN.md)。本文提供桌面端专项参考。

桌面端是现有控制站网页的受限桌面壳，不包含 ROS、MQTT、机器人证书、串口、CAN 或底盘控制代码。首次启动会自动列出同一局域网内的一体机控制站；操作员选择名称后成功加载的就是控制站正在提供的原网页，因此登录、地图、实时状态、手动控制、手柄、键盘和巡航的界面及功能完全一致。

这样做的好处是：控制站更新网页后，所有桌面端下次刷新即使用新版本，不需要逐台重新安装；机器人控制边界也仍在车载 Edge Gateway 内。

## 给操作员安装

Linux 控制客户端优先使用 deb 包：

```bash
sudo apt install ./LR-Robot-Dispatch-1.0.0-amd64.deb
```

安装后从应用菜单打开“智巡机器人调度”，或执行：

```bash
lr-robot-dispatch-desktop
```

优先在“发现到的控制站”中点击维护人员命名的控制站。若所在网段/VPN 不支持多播，才由维护人员在下方输入 `https://control-station.example` 或 `https://192.168.10.10`。桌面端会访问 `/health` 验证控制站证书和唯一设备身份后才进入系统；选项广播本身不构成信任。

局域网控制站必须使用 HTTPS。`http://localhost:8000` 和 `http://127.0.0.1:8000` 仅允许本机开发调试；它们不能用于远程操作员电脑。控制站证书无效、过期或不受信任时，程序会拒绝连接，不会提供“忽略证书错误”的选项。

控制站采用本平台内部 CA 时，维护人员需随桌面端交付控制站导出的 `client-ca.crt`。先完全退出桌面端，再在每台 Linux 操作员电脑执行一次：

```bash
sudo apt install libnss3-tools
sudo /path/to/LR-Web/scripts/install-desktop-ca.sh /path/to/client-ca.crt
```

该脚本只接受 CA 证书并写入系统信任库和当前操作员的 Chromium/Electron 信任库；它不接受私钥。若缺少 `certutil`，请先安装 `libnss3-tools` 后重试。Windows 客户端应由管理员把同一 `client-ca.crt` 导入“本地计算机 -> 受信任的根证书颁发机构”。

也会生成 AppImage 便携包，但它依赖系统 FUSE，且部分新版 Ubuntu 的 AppArmor 会阻止其安全沙箱。出现此类提示时请安装 deb 包，不要使用 `--no-sandbox` 绕过安全限制。

## 维护人员构建

在项目根目录、使用 Node.js 22：

```bash
npm run desktop:test
npm run desktop:build
```

Linux 输出位于 `release/desktop/`：

```text
LR-Robot-Dispatch-<版本>-amd64.deb
LR-Robot-Dispatch-<版本>-x86_64.AppImage
```

Windows 安装包配置已包含；在 Windows 构建机执行：

```bash
npm run desktop:build:win
```

开发调试可使用：

```bash
npm run desktop:dev
```

该命令要求 Electron 安全沙箱可用。若 Ubuntu 禁止普通用户 namespace，脚本会明确提示如何为开发期的 `chrome-sandbox` 配置 root/4755 权限；不会使用 `--no-sandbox`。

## 安全边界

- 桌面端关闭 Node.js 注入，开启上下文隔离、Chromium 沙箱和 Web 安全策略。
- 仅允许加载已配置控制站的同源页面；外部链接交给系统浏览器。
- 拒绝网页权限申请、webview 和任意新窗口；同源新窗口改为当前窗口打开。
- 地址、登录会话和缓存仅保存在当前操作员系统账户的应用数据目录；不会保存 MQTT/ROS 凭据。
- 局域网发现只接收 `239.255.77.77:39777` 的候选广播；选择后必须通过 HTTPS 证书和控制站唯一 ID 校验，跨网段或禁用多播时可由维护人员输入地址。
- 菜单的“配置控制站”可随时返回地址配置页；“打开日志目录”用于维护排障。
