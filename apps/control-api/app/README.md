# 中心控制 API 源码目录

此目录保存 FastAPI 中心控制服务的源码，负责认证授权、PostgreSQL 业务模型、调度状态机、审计、WebSocket 推送、MQTT 命令发布与 Gateway 回执消费。

中心 API 不得直接导入 ROS 2、CAN 或串口驱动；硬件控制必须保留在机器人本机的 Edge Gateway 中。
