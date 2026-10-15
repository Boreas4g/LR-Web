# D435 相机 SDK

本目录提供图书机械臂使用的 Intel RealSense D435 直接相机封装。它在独立工作线程中独占相机，采集和坐标计算不依赖 GUI 或 Web 服务。

## 功能

- 启动、预热和关闭指定序列号的 D435 RGB-D 流；默认彩色/深度均为 `1280x720 @ 30 FPS`，可将深度对齐到彩色图。
- 获取同步的 BGR 彩色图、米单位深度图、相机内参、时间戳和帧号。
- 将像素反投影为相机坐标系三维点；点击处无有效深度时，在附近逐级扩大范围搜索最近有效深度。
- 由有效深度生成相机坐标系点云，可限制最大深度和采样步长。
- 基于 OpenCV 特征匹配和单应性矩阵定位书脊模板；优先使用 SIFT，当前 OpenCV 不支持时自动改用 ORB。特征检测限定在画面中部横向识别区（ROI）内，排除画面边缘的包装书脊图等干扰；候选单应矩阵依次通过几何校验、光度 NCC 校验（弱 NCC 候选需多尺度位置共识才放行），SIFT 全部失败时再由多尺度相关匹配兜底。
- 读取 `4x4` 相机到机械臂基座外参，将书脊目标点从相机坐标系转换到基座坐标系，并返回置信度、像素坐标和采集 ID。

## 主要接口

| 模块 | 职责 |
| --- | --- |
| `d435_device.py` | `D435Device` 管理 RealSense 管线；`D435Frame` 提供 `point_at_pixel()` 和 `point_cloud()`。 |
| `matcher.py` | `BookSpineMatcher` 加载书脊模板、匹配图像并选择抓取像素。 |
| `transform.py` | `CameraToBaseTransform` 校验并应用 `p_base = T_base_camera @ p_camera`。 |
| `service.py` | `D435BookSpineLocator.locate_book()` 串行完成预热、采集、匹配、深度反投影和坐标转换。 |

## 配置与前提

- 安装 `pyrealsense2`、`numpy` 和 `opencv-python`，并确保操作系统已授权访问 RealSense USB 设备。
- 在 `D435BookSpineLocatorConfig` 中设置实际相机序列号、流参数、最大深度和采集超时；`grasp_point_from_top_ratio` 可选 `1/6`、`1/5`、`1/4`、`1/3`、`2/5`、`1/2` 或 `2/3`，表示抓取点距书脊顶部的比例，默认 `1/6`。默认序列号为 `262322073961`。
- 将 `config/calibration/camera_to_base.example.json` 复制为部署配置，并用实机标定得到的外参替换其中矩阵。未加载有效外参时，服务不会执行定位。
- 书脊模板应是具有足够纹理特征的图片；匹配失败、深度无效或目标超过 `max_depth_m` 时，服务会在 `max_attempts` 次采集后返回错误。

相机坐标单位为米，采用 RealSense 约定：`x` 向图像右侧、`y` 向下、`z` 向相机前方。坐标转换或视觉定位成功不代表机械臂运动安全；执行实际抓取前仍须复核外参、工作空间和碰撞风险。

## 自检脚本

从仓库根目录运行 `python3 bookarm/scripts/cam/camera_test.py`，可依次测试采集、反投影、点云、书脊模板匹配、外参变换和端到端定位。脚本只访问相机，**不会**发送机械臂运动指令；诊断图像、点云和 JSON 报告保存到 `bookarm/recordings/camera_test/`。
