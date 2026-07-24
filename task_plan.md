# 相机单所有者模块任务计划

## 目标

把当前板外 `/opt/orin-ground-sender/orin_camera_stream.py` 迁入
`TYI_E100_Firmware` 管理，由 `orin-camera-stream.service` 唯一打开 UVC
相机，并向 RK3588、后续 QR/YOLO 和可选 `media-gateway` 统一提供 MJPEG。

## 阶段

- [x] 调研现有仓库媒体模块和板端相机服务
- [x] 验证摘掉保护套后的真实画面
- [x] 固化设计与实施计划
- [ ] 测试先行实现仓库内相机服务
- [ ] 备份并迁移 Nano systemd 服务
- [ ] 验证单所有者、并发读取和自动恢复
- [ ] 验证飞行链路无回归
- [ ] 更新关键记录、提交并推送

## 安全边界

- 不启动电机、不解锁、不进入 OFFBOARD。
- 不改蓝牙和 `orin-telemetry-bridge`。
- 保留 Nano 上未跟踪的
  `workspace/src/uav_base_bringup/scripts/orin_telemetry_bridge.py`。
- 只替换 `orin-camera-stream.service` 的采集实现，端口和 URL 不变。
- 部署前备份旧 unit 和旧 `/opt` 脚本。

## 成功证据

- `fuser /dev/video0` 只返回 `orin-camera-stream.service` 的一个 PID。
- `/healthz` 报告最新帧且 HTTP 200。
- `/snapshot.jpg` 是新的 1280x720 非黑帧。
- 两个并发 `/stream.mjpg` 客户端都收到持续字节流。
- `systemctl restart orin-camera-stream` 后服务恢复。
- `flight-core`、`control-gateway`、`pointcloud-gateway` 仍为 healthy。

