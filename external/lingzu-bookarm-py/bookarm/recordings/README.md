# recordings/（运行期数据目录，交付包有意留空）

bookarm 服务运行期会在此写入：

- `tuning/{take,return}_profile.json`：流程调参页持久化的运动计划参数（服务启动时自动加载，缺失则用内置默认）。
- `takeout_templates/`：取书模板录像与快照。
- `arm_telemetry/`：关节温度/反馈遥测落盘。
- `runtime/`：运行时状态快照。

新机上由服务自动重建，无需手工创建；如需保留旧机调参成果，可单独拷贝
`recordings/tuning/*.json` 到新机同路径。
