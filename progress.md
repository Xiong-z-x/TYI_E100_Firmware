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

## 2026-07-25

- 验证 Windows `C:\nano` 下旧镜像拉取缓存和失败下载残留均已不存在；未清理
  模型资产和项目数据。
- 建立隔离的 YOLO11 与 SpeciesNet 评测环境，未覆盖系统 Python。
- 完成 Ultralytics 官方 `yolo11s-seg.pt` 基线：15 个目标中仅 3 个以不低于
  0.8 的置信度输出赛题同名类别，均为象；证明 COCO 标签空间不足。
- 修复首次下载被截断的 MegaDetector 权重；完整权重 280,767,041 bytes，
  SHA-256 为
  `FE3E90E4B1955821AB7C1F88B446DC0C8CB25E109FDD1872916A55305294A5EF`。
- 完成 Google SpeciesNet v4.0.3a + MegaDetector v5a 基线：官方整图 15/15
  目标全部检出，IoU 0.5 下 precision/recall 均为 1.0，最低检测置信度
  0.8884。
- SpeciesNet 对象、虎分类可靠；狼、猴、孔雀仍未全部达到目标类别 0.8 阈值。
- 按当前决策停止在预训练基线阶段：没有构造数据集、没有微调、没有替换 Nano
  上正在运行的 YOLOE/TensorRT 服务，也没有修改飞行链路。
- 后续决策改为替换 YOLOE：完成 MegaDetector v5a + SpeciesNet v4.0.3a 的
  双 TensorRT 运行时、五类分类学概率聚合、拒识证据输出和 Windows 实时查看器。
- 导出并校验静态 ONNX：MegaDetector `1x3x1280x1280`，SpeciesNet
  `1x480x480x3`；两个文件均通过 `onnx.checker`。
- 官方整图基线达到 15/15 检测、15/15 闭集分类正确且全部通过 0.8 门槛。
- 构造 `nuedc-2025-h-closed-set-v2`：train 1800、val 360、test 600；
  训练姿态和地貌区域与保留集合隔离，并加入 unknown 负类。
- 预训练模型在 600 张 v2 test 上达到 59.0%，unknown 100% 拒识；结果记录在
  `C:\nano\state\vision-baselines\speciesnet-closed-set\synthetic-test-v2-report.json`。
- 生成 AutoDL 输入 CSV、MegaDetector 结果文件、两阶段训练命令和官方
  `speciesnet_timm.pt`；官方 split 算法复核为严格 1800/360，test 不入训练。
- Windows 静态编译和视觉测试通过：`19 passed`。飞行容器、相机服务和飞控接口
  未被视觉代码修改。
- Nano `develop` 的 49 个本地提交已先完整并入远端，再快进到
  `de6782c`；队友未跟踪的 `orin_telemetry_bridge.py` 保持原样。
- MegaDetector 1280 FP16 TensorRT 引擎构建成功，大小约 271 MiB，SHA-256
  `A342A4DDE7D0BC6A14D1D2DE51F693A8877F72C7FA64088C57FBD1DD607FFD57`。
- SpeciesNet 原始 ONNX 首次构建因 TensorRT 8.5 的 Squeeze axes 常量限制失败；
  已将常量折叠步骤写入导出工具，折叠前后 ONNX Runtime 输出逐元素一致。
- 折叠后的 SpeciesNet ONNX 在 Nano TensorRT 8.5.2 上构建成功；分类引擎
  SHA-256 为
  `55C70841F9FD1585288D3820C6B3E28AC7E3DEE619718824BE7CB41A758A7E4F`。
- 新 `vision-gateway:2.0.0` 已部署：官方整图板端 15/15 检出、15/15 分类
  正确；实时相机约 224--233 ms/frame，处理能力约 4.1 FPS，按 2 FPS 运行，
  连续 685 帧零失败。
- 视觉容器实测约 1.718 GiB，限制 2.734 GiB；板端可用内存约 2.9 GiB。
  `flight-core`、`control-gateway`、`pointcloud-gateway` 和
  `vision-gateway` 同时保持 healthy。
- 删除板端旧 YOLOE 权重、旧合成测试输出、Ultralytics 运行缓存、旧
  `vision-gateway:1.0.0-yoloe26s-jp5` 镜像及重复上游镜像标签；保留用于可重复
  构建的 `tyi/ultralytics:jetpack5-20260724`。
- Windows 按严格白名单清理旧 v1/冒烟数据集、旧 YOLO 权重、重复 ONNX/PT、
  pip 缓存、崩溃转储和可重建临时清单，实际释放约 2.03 GiB；WSL、最终 v2
  数据集、模型源、最终 ONNX、AutoDL 包和飞控固件均保留。
- AutoDL 离线包：
  `C:\nano\autodl\speciesnet-closed-set-v2-autodl-20260725.tar.gz`，
  320,365,314 bytes，SHA-256
  `2FBD5F64072F1A2F2F4BED9052299DBF1968F766D3FA68C9E078BDC74FC33005`。
- Windows 旧实时查看器直接播放 2 FPS 推理标注流，造成肉眼卡顿；已改为后台
  持续读取并丢弃过期的 15 FPS 原始相机帧，同时异步轮询识别 JSON 后叠加框。
  显示刷新率与 AI 推理频率现已解耦。
- UVC 相机没有 `focus_auto` 或 `focus_absolute` 控件，属于固定焦距，不能通过
  软件重新对焦。将硬件 `sharpness` 从默认 2 调到 4 后，同场景 Laplacian
  方差从 19.30 提升到 24.47，未见明显边缘伪影；该设置已写入相机 systemd
  启动前置命令。
- 用户实际观察认为 `sharpness=4` 画面更模糊；指标不能替代人的最终视觉判断。
  已立即恢复相机原始 `sharpness=2`，并删除 systemd 中的强制锐度设置。低延迟
  查看器只改变显示链路，不改变原始相机帧，因此继续保留。
