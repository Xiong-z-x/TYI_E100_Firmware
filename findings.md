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

## 2026-07-25 预训练模型结论

- 先前 YOLOE 合成集在 `confidence=0.12` 时为零召回，不是权重来源不公开，
  而是开放词汇视觉提示与当前打印动物任务不匹配；它不能作为最终精度方案。
- YOLO11s-seg 的 COCO 权重具有可用的轻量分割骨干，但缺少虎、狼、猴、孔雀
  的目标标签，直接部署会产生高置信度错误类别。
- SpeciesNet 的优势来自野生动物专用分类器与 MegaDetector 组合。官方整图检测
  已达到 15/15，证明通用动物定位不需要从零训练。
- SpeciesNet 仍是自然相机陷阱数据域；赛题是打印图、俯视、缩放和复杂地貌背景。
  象和虎可直接识别，狼存在犬属混淆，猴的物种分布过细导致置信度分散，孔雀多
  回退到科/属级别。因此它是强基线，不是当前可直接验收的五分类最终模型。
- 0.8 阈值应施加在最终五类闭集分数上。MegaDetector 的动物框分数与
  SpeciesNet 的物种分类分数不是同一标定空间，不能简单用同一个阈值解释。
- 下一步如果实拍验证仍复现狼、猴、孔雀不足，最有效路线是保留 MegaDetector
  或 YOLO11 分割定位能力，再训练五类闭集轻量分类/分割头；本轮按用户要求暂不
  生成数据和微调。

## 2026-07-25 SpeciesNet 闭集替换结论

- 直接取 SpeciesNet 原生 top-1 不是正确的赛题决策空间。狼的概率分散在犬科
  不同属种，猴分散在多个非人灵长类家族，孔雀分散在雉科和孔雀属；将 2498
  维 softmax 概率按分类学映射到赛题五类后，官方 15 个目标全部正确。
- 接受门槛采用四条件同时满足：MegaDetector `>=0.20`、五类条件概率
  `>=0.80`、目标类原生概率质量 `>=0.45`、第一/第二名差值 `>=0.30`。
  这能防止“目标五类总质量很低但在五类内部相对最大”的伪高置信度。
- 官方整图的 15 个目标全部通过：象和虎接近 1.0；狼闭集置信度
  0.9944--0.9985，猴 0.9665--0.9936，孔雀 0.9901--0.9957。
- v2 合成测试集使用 MegaDetector 紧裁剪空间而不是整场景空间，包含任意旋转、
  透视、缩放、模糊、噪声、色彩、JPEG 和地貌变化。训练姿态/地貌区域与验证、
  测试隔离；总计 2760 张。
- 未微调预训练模型在 600 张独立合成测试上的闭集准确率为 59.0%；unknown
  拒识 100%。主要损失来自保守拒识：象 44%、猴 34%、孔雀 36%、虎 86%、
  狼 54%，仅出现 4 个猴到狼的跨类误判。
- 因此正确优化方向是保留 0.8 最终门槛，在 AutoDL 上微调提高目标类概率质量，
  并补充 Nano 实拍 MegaDetector 假阳性框作为 hard negative；不应继续降低
  阈值换取表面召回。
- Nano 的 JetPack 5 系统 Python 3.8 与当前 SpeciesNet Python 包的 Python
  版本要求不匹配；部署采用 ONNX 转 TensorRT，不修改系统 Python 和飞行环境。
- AutoDL 包固定使用官方 `agentmorris/speciesnet-fine-tuning` 提交
  `70e96884ce9981caca1ddc40bcebe59b0d011946`，并内置官方转换后的
  `speciesnet_timm.pt`，避免训练服务器再次联网下载。
- 原始 FX 分类器 ONNX 能通过 `onnx.checker`，但 JetPack 5 的 TensorRT
  8.5.2 解析 `Squeeze` 时拒绝由 `Identity` 转发的 axes 常量，报错
  `Squeeze axes input must be an initializer`。这不是权重、显存或分类逻辑问题。
- 按 TensorRT 官方错误提示使用 Polygraphy 折叠常量：节点从 1067 降到 866，
  初始化器从 506 变为 637；原始/折叠 ONNX 的随机输入输出最大绝对差为 `0.0`。
  折叠文件 SHA-256 为
  `71E77ABD7D5D87088C74AF222EAAB52A17696ACD36F52C18FAABD3F438B45751`。
- 板端官方整图验证不是 Windows 推断结果的转述：Nano 上的双 TensorRT 引擎
  实际输出 15 个接受框、0 个拒绝框，IoU 0.5 下 15/15 匹配，15/15 分类
  正确，检测 precision/recall 和匹配分类准确率均为 `1.0`。
- 实时链路不是把 15 个裁剪逐张串行当作帧率：普通相机帧无检测时端到端推理
  约 224--233 ms，约 4.1 FPS 处理余量；运行策略主动限制为 2 FPS，给飞行、
  FAST-LIO 和地面通信留出 CPU/GPU/内存余量。
- 预训练合成压力测试 59.0% 与官方整图 100% 不矛盾：前者包含强旋转、模糊、
  透视、色偏和压缩，并使用严格 0.8 拒识门槛。它揭示的是实战域适配召回不足，
  因此最终 AutoDL 微调必须加入真实镜头、真实打印尺寸/高度和实时假阳性框。
- 视觉窗口卡顿不是 Wi-Fi 或 OpenCV 绘制性能不足。`vision-gateway` 只在每次
  2 FPS 推理完成后发布一张新标注 JPEG，因此旧查看器最多只能得到 2 张不同画面。
  正确边界是用 8090 的 15 FPS 原始流做显示，用 8765 JSON 做低频识别叠加。
- 当前 USB 相机只暴露亮度、色彩、曝光、增益和锐度等 UVC 控件，没有任何焦距
  控件。近距离显示器测试模糊时，应优先增加相机与屏幕距离；提升分辨率或锐度
  只能增加采样/边缘清晰度，不能修复超出固定焦平面的光学失焦。
- `sharpness=4` 虽提高静态边缘方差，但用户实际显示观察更模糊，已否决该参数。
  当前重新采用相机默认 `sharpness=2`；后续画质调整必须以真实目标距离的并排
  图像和识别结果为准，不能只依据单一清晰度指标。
- 用户随后确认该次模糊源于镜头硬件焦距调整，并要求恢复 `sharpness=4`。此次
  重新启用锐度4，同时把原始显示流提高到30 FPS；这项显示改动与2 FPS模型推理
  相互独立。
