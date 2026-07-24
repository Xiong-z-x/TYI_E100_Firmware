# 相机模块进度

## 2026-07-24

- 创建隔离分支 `feature/camera-stream-owner`，基线提交 `c86a1f5`。
- 基线 `py_compile` 和媒体配置 JSON 校验通过。
- 审计现有 `/opt` 相机脚本、systemd unit、设备节点和仓库
  `media-gateway`。
- 摘掉保护套后重新采集并人工检查 1280x720 图像，画面正常。
- 完成单所有者迁移设计和实施计划。
- 新增 9 个相机/仓库接线测试；先验证缺失实现和启动失败收尾问题，再实现到
  全部通过。
- 部署分支提交：
  `0354b9f`（设计）、`d6d721b`（服务）、`c58bec9`（部署）。
- 启动失败收尾修复提交：`d4771b6`；Nano 对应 `bc47dfe`。
- Nano `develop` 对应提交：
  `eec8a02`、`2f0fe5b`、`092d344`。
- 旧部署备份：
  `/var/backups/tyi-camera/20260724T135808Z`。
- 新 unit 已从仓库启动；旧 `/opt` 采集脚本不再运行。
- 单一 PID 持有 `/dev/video0`；两个并发客户端均连续收到 MJPEG。
- 手工重启后服务恢复，局域网快照 HTTP 200。
- 三个飞行容器保持 healthy，MAVROS connected、未解锁、在地面。
- LiDAR 20.287 Hz、FAST-LIO 20.296 Hz、vision pose 235.180 Hz。
- 蓝牙服务和 Intel `8087:0029` 设备仍在，未做任何修改。
