# 相机模块调研结论

## 板端事实

- Nano：NVIDIA Orin Nano Developer Kit，L4T R35.6.4。
- UVC：`05a3:9230`，`uvcvideo`。
- 图像节点：`/dev/video0`；元数据节点：`/dev/video1`。
- 稳定别名：
  `/dev/v4l/by-id/usb-HD_Camera_Manufacturer_USB_2.0_Camera-video-index0`。
- 当前服务：`orin-camera-stream.service`，监听 `0.0.0.0:8090`。
- 当前唯一持有者：PID 994，
  `/usr/bin/python3 /opt/orin-ground-sender/orin_camera_stream.py`。
- 当前采集：1280x720 MJPEG 60 fps；HTTP 限速 15 fps。

## 摘套后画面

- 文件：`C:\nano\camera-analysis\camera_snapshot_uncovered_20260724.jpg`
- SHA-256：
  `7A0DB8839A65924D06BA95B28A442708A92297DC1743D831DE9D0B263AD6A09E`
- 尺寸：1280x720。
- 平均亮度：68.84/255；不再是黑帧。

## 仓库可复用模块

- `docker/media-gateway/app.py` 已支持 `sourceMode=mjpeg`。
- 它能从 MJPEG 解码后发布 RTSP/WebRTC，适合作为消费者，不应直接打开
  V4L2。
- `configs/media-gateway/config.json` 已设置 `sourceMode=mjpeg`，但
  `sourceUrl` 仍错误指向不存在的 `http://127.0.0.1:8765/v1/mjpeg`。
- `.env` 的 `MEDIA_CAMERA_PREFERRED_DEVICE` 仍是过期 USB 路径。
- 仓库没有可运行的 `vision-gateway` 实现，也没有现成 YOLO/二维码模块。

## 设计决策

- 保持 `orin-camera-stream.service` 为唯一设备所有者。
- 代码和 unit 迁入仓库；旧 `/opt` 版本只作为备份，不再运行。
- RK3588 和未来识别进程只访问 8090 HTTP API。
- 将 `media-gateway` 的 MJPEG 上游改为 8090，但本阶段不启动该可选容器。

## 网络身份边界

- 当前 WLAN：`192.168.0.108/24`，MAC `70:a6:cc:0d:8f:5e`。
- 当前有线雷达网：`192.168.2.50/24`，MAC `44:49:c0:1d:7f:90`。
- 主机名仍是通用的 `ubuntu`。
- `ubuntu.local` 当前会解析到其他组的 `192.168.0.124`，禁止用它连接本机
  相机流。
- RK3588 当前应使用
  `http://192.168.0.108:8090/stream.mjpg`，IP 变化后必须先按 WLAN MAC
  重新确认。
