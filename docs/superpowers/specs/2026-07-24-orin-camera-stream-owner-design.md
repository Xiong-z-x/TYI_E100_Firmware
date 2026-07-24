# Orin 相机单所有者设计

## 目标与范围

Jetson Orin Nano 上的 USB UVC 相机只能由一个长期进程直接打开。该进程
统一提供最新 JPEG、MJPEG 连续流和可观测健康状态；RK3588、未来 QR/YOLO
识别模块及可选 `media-gateway` 都作为 HTTP 消费者。

本阶段不实现识别模型、不改蓝牙链路、不启动可选媒体容器，也不改飞行控制
容器。

## 方案比较

1. **仓库管理的主机 systemd 单所有者（采用）**：保留现有 8090 API 和
   GStreamer 采集方式，把实现、unit、安装脚本、测试迁入仓库。设备访问最
   直接，启动早，故障可由 systemd 自动恢复。
2. **保留队友 `/opt` 部署**：运行风险最低，但关键代码与设备配置继续脱离
   仓库，无法复现和回档。
3. **Docker 直接拥有相机**：封装统一，但需要设备映射和容器生命周期协调，
   容易与已有飞行栈部署互相影响，不适合当前最小迁移。

## 架构与数据流

```text
UVC /dev/video0
       |
       | 唯一 V4L2 打开者
       v
orin-camera-stream.service
       |
       +-- /healthz
       +-- /snapshot.jpg
       +-- /stream.mjpg
              |
              +-- RK3588 地面站
              +-- 后续 QR/YOLO 消费者
              +-- 可选 media-gateway -> RTSP/WebRTC
```

服务通过
`/dev/v4l/by-id/usb-HD_Camera_Manufacturer_USB_2.0_Camera-video-index0`
选择图像节点，避免裸 `/dev/video0` 随枚举顺序变化。该相机无唯一序列号；
若未来同一 Nano 同时插入两个相同型号相机，需要改用固定 USB 端口的 by-path
别名。

## 组件

- `camera/orin_camera_stream.py`：单 GStreamer pipeline、线程安全最新帧
  缓冲、HTTP 多客户端扇出、帧新鲜度健康检查。
- `camera/orin-camera-stream.service`：固定用户、video 组、稳定设备别名、
  自动重启。
- `scripts/install_camera_stream.sh`：验证设备和依赖、备份旧 unit、安装新
  unit、重启并验证健康；不删除旧 `/opt` 脚本。
- `tests/camera/test_orin_camera_stream.py`：不接硬件测试帧缓冲、快照、
  MJPEG 和失效健康状态。
- `configs/media-gateway/config.json`：只把 MJPEG 上游改到 8090。

## 接口与兼容性

- `GET /healthz`：有新鲜 JPEG 时返回 200；无帧或帧过期返回 503。JSON
  保留 `status`，增加序列号、帧年龄和采集配置。
- `GET /snapshot.jpg`：返回当前最新 JPEG。
- `GET /stream.mjpg`：保持
  `multipart/x-mixed-replace; boundary=frame`。
- 对外地址保持
  `http://<Nano-IP>:8090/stream.mjpg`，无需修改 RK3588 读取逻辑。

## 故障处理与回退

- 相机启动失败或 GStreamer bus 报错时进程退出，systemd 每 2 秒重启。
- 无新帧时健康检查不得谎报正常。
- 部署脚本在 `/var/backups/tyi-camera/<时间戳>/` 保存旧 unit 和旧脚本。
- 回退时恢复备份 unit，执行 `systemctl daemon-reload` 和
  `systemctl restart orin-camera-stream.service`。

## 验证

1. 单元测试先失败后通过。
2. Python 编译和 systemd unit 静态校验通过。
3. Nano 部署后仅一个 PID 持有 `/dev/video0`。
4. 两个并发客户端持续接收 MJPEG。
5. 重启 unit 后自动恢复且快照不是黑帧。
6. 三个飞行容器保持 healthy，MAVROS/FAST-LIO 只做只读状态核验。

