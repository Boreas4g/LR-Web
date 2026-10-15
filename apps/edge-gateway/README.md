# 机器人边缘网关

此目录预留给部署在每台机器人上的 Python 服务。它连接中心 MQTT，并在本机转换为：

- `/home/boreas/Library_robot` 中的 Nav2 Action、ROS Topic、TF 与设备健康状态。
- `/home/boreas/project/lingzu_arm/lingzu-bookarm-py` 中的机械臂、夹爪、升降台与 RealSense 控制。

Gateway 应作为 systemd 服务运行，并具备命令过期检查、幂等处理、控制租约、断网停车和本地安全状态机。
