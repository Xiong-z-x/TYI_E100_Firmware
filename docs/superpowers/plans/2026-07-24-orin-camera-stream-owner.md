# Orin Camera Stream Owner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Orin Nano UVC 相机迁移为仓库管理的 systemd 单所有者服务，同时保持 RK3588 使用的 8090 MJPEG 接口。

**Architecture:** 一个主机 GStreamer 进程独占 UVC 图像节点，将最新 JPEG
缓存在内存并通过线程化 HTTP 服务扇出。识别、RK3588 和可选媒体网关只消费
HTTP，不直接访问 `/dev/video*`。

**Tech Stack:** Python 3、PyGObject/GStreamer 1.0、systemd、Bash、unittest

---

### Task 1: 为 HTTP 与帧健康语义建立失败测试

**Files:**
- Create: `tests/camera/test_orin_camera_stream.py`
- Create: `camera/__init__.py`

- [x] 编写测试，覆盖帧发布序列、健康端点的新鲜/过期状态、快照 JPEG 和
  MJPEG multipart 帧。
- [x] 运行
  `python -m unittest discover -s tests/camera -p "test_*.py" -v`。
- [x] 确认因 `camera.orin_camera_stream` 不存在而失败。

### Task 2: 实现最小单所有者服务

**Files:**
- Create: `camera/orin_camera_stream.py`

- [x] 实现 `FrameBuffer`，记录 sequence、最新帧和单调时钟时间。
- [x] 实现不依赖硬件的 HTTP handler 和 `CameraHttpServer`。
- [x] 保持 `/healthz`、`/snapshot.jpg`、`/stream.mjpg` 路径兼容。
- [x] 实现 GStreamer MJPEG appsink pipeline 和 bus 错误退出。
- [x] 运行 Task 1 测试并确认全部通过。
- [x] 运行 `python -m py_compile camera/orin_camera_stream.py`。

### Task 3: 添加可回退 systemd 部署

**Files:**
- Create: `camera/orin-camera-stream.service`
- Create: `scripts/install_camera_stream.sh`
- Modify: `configs/media-gateway/config.json`
- Modify: `.env`

- [x] unit 使用仓库脚本、固定 by-id 图像节点、1280x720@60 采集和 15 fps
  输出。
- [x] 安装脚本验证用户、设备路径、Python GI/GStreamer，备份旧部署后原子
  安装 unit。
- [x] 把 `media-gateway` 上游改为
  `http://127.0.0.1:8090/stream.mjpg`，更新过期设备别名。
- [x] 用 `bash -n scripts/install_camera_stream.sh`、
  `systemd-analyze verify camera/orin-camera-stream.service` 和 JSON 解析做
  静态验证。

### Task 4: 部署到 Nano

**Files:**
- Modify on device: `/etc/systemd/system/orin-camera-stream.service`
- Backup on device: `/var/backups/tyi-camera/<timestamp>/`

- [x] 提交并推送 feature 分支。
- [x] 将提交同步到 Nano 的 `develop`，保留未跟踪 telemetry 文件。
- [x] 执行安装脚本；仅相机流会短暂中断。
- [x] 验证 unit 的 `ExecStart` 已指向仓库，旧 `/opt` 进程已退出。

### Task 5: 现场验收与记录

**Files:**
- Modify: `PROJECT_DEVICE_CONFIG.md`
- Modify: `progress.md`
- Modify: `task_plan.md`

- [x] 核对 `fuser /dev/video0` 只有一个服务 PID。
- [x] 验证健康、快照、两个并发 MJPEG 客户端和 unit 重启恢复。
- [x] 检查 1280x720 快照平均亮度及帧内容。
- [x] 核对飞行三个容器健康和关键 ROS 数据仍存在。
- [x] 记录备份路径、部署提交、服务状态、端点和 RK3588 接入地址。
- [x] 提交并推送最终证据。
