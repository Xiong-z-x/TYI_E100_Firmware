# TYI_VLN 语言/点选导航与测距链路说明

## 1. 目标与范围

本文说明当前系统为适应动态飞行、机体持续晃动场景所做的识别、点选、测距、目标锚定、实时重投影与接入架构优化。重点是保证 VLM 送检 RGB、点选 RGBD、depth、位姿和目标显示使用一致的数据时刻，避免红点停留在送检帧、测距漂移或飞行目标锁定失败。

本文覆盖：

- 动态飞行下的 RGB/depth/pose 快照一致性设计
- VLM grounding 链路
- 点选导航链路
- RealSense 深度查询和目标三维锚定链路
- 实时画面中的目标重投影显示
- `control-gateway` 的语言/点选导航预览与下发逻辑
- APP 应如何接入、显示与报错
- 2026-04-25 已修复的问题、架构边界与当前约束

本文不覆盖：

- 飞行控制主链的 ROS 启动细节
- WHEP/WebRTC 播放器实现细节
- FastLIO2 / MID360 外参标定细节

## 2. 组件分工

### 2.1 `TYI_VLN`

职责：

- 作为 D435i 的单一相机 owner，持续持有 RealSense RGB/depth pipeline
- 维护 aligned RGBD snapshot cache，用于 VLM grounding、测深、目标三维锚定和后续重投影
- 通过 `/v1/mjpeg` 输出预览 MJPEG，供 `media-gateway` 转成 WHEP/RTSP；该预览只用于显示，不作为测距依据
- 调用 Qwen VLM 做 grounding
- 将 grounding 坐标统一回送检快照的原图像素坐标系
- 使用同一 RGBD 快照里的 depth 对 grounding 结果做深度查询
- 输出相对 `base_link` / `livox_frame` / 机体中心的目标相对位置

当前实现明确禁止 `TYI_VLN` 和 `media-gateway` 同时打开 D435i。识别截图、深度图、目标解算都来自 `TYI_VLN` 持有的同一 RealSense 数据源；实时预览只消费该数据源的显示分支。

### 2.2 `media-gateway`

职责：

- 不直接打开 `/dev/video*` 相机设备
- 读取 `TYI_VLN` 的 `http://127.0.0.1:8765/v1/mjpeg`
- 输出当前可用的 WHEP/RTSP 视频路径
- 对旧 profile 请求做兼容 fallback，避免 APP 因硬编码 profile 直接失败

`media-gateway` 是显示与接入层，不参与目标测距或三维解算。2026-04-25 现场为了降低 Jetson 负载，媒体输出可能为 `camera-480p15`；这只是预览链路的性能配置，不改变识别快照和目标解算语义。

### 2.3 `control-gateway`

职责：

- 作为 APP 的统一业务入口
- 接收自然语言导航请求
- 接收实时视频点选导航请求
- 调用 `TYI_VLN /v1/qwen-ground`
- 调用 `TYI_VLN /v1/query-depth`
- 调用 `pointcloud-gateway /v1/observation-snapshots`，把 RGBD 时间戳与当时 pose/pointcloud 历史对齐
- 判断深度结果是否可靠
- 在可用时生成目标相对位姿与规划目标
- 在不可靠时直接拒绝，避免错误导航

### 2.4 APP

职责：

- 显示实时视频
- 输入自然语言目标
- 点选实时视频中的目标像素
- 调用 `control-gateway` 导航接口
- 展示服务端返回的 grounding 结果、目标距离、目标锚点、实时重投影红点与错误信息

APP 不负责自行截图上传到云端后再本地测距，也不应该自行重新解释 VLM 坐标。点选导航中，APP 只提交用户点击像素和当前源分辨率；测深、快照、目标三维锚定和规划目标生成都由 `control-gateway` 统一完成。红点显示应使用服务端返回的目标锚点或重投影结果，而不是固定贴在发起识别/点选瞬间的屏幕坐标。

## 3. 总体设计原则

当前实现遵循以下原则：

1. 实时视频主链不被语言导航/测距打断。
2. VLM 送检 RGB、depth、用于解算目标位置的快照必须来自同一个 RealSense RGBD bundle。
   点选导航同样必须使用点击时对应的 RGBD/depth 时间戳和 observation snapshot，不能把点击像素与后续时刻的 pose 混用。
3. 目标位置一旦解算成功，应从“图像像素”提升为“相机/机体/地图坐标中的三维锚点”，后续显示和飞行都围绕该锚点更新。
4. 实时画面中的红点应由当前相机姿态反向投影目标锚点得到，不能停留在发起识别时的屏幕坐标。
5. 坐标统一在板端完成，APP 只消费统一后的结果。
6. 对透明/半透明目标，宁可返回“深度不可用”，也不允许误把背景当目标。
7. 所有导航业务入口统一走 `control-gateway`，不让 APP 直接访问 `TYI_VLN` 作为产品链路。
8. 点选导航“开始飞行”必须发布预览阶段已经冻结的 `goalWorldM`，不能再次用旧点击像素重新测距，否则无人机晃动后可能飞向当前画面同一像素处的另一个空间点。

## 4. 动态飞行下的 RGBD 快照一致性设计

### 4.1 为什么必须单相机 owner

早期方案中，语言导航、实时视频、深度查询可能分别打开 RealSense 彩色视频或深度设备，导致：

- 实时画面卡顿或黑屏
- 视频路径重建
- 识别用 RGB、测距 depth、最后解算 pose 不在同一时刻
- 红点停留在送检帧位置，无法随实时画面稳定重投影

因此当前实现明确要求：D435i 只能由 `TYI_VLN` 持有，其他服务不得再直接打开 `/dev/video*`。

### 4.2 当前快照和预览来源

`TYI_VLN` 持续采集 RealSense RGB/depth，并维护两类缓存：

- 预览缓存：color-only 快路径，用于 `/v1/mjpeg` 实时画面
- RGBD 对齐缓存：aligned color/depth bundle，用于 VLM grounding、深度采样和目标三维解算

`media-gateway` 读取：

- `http://127.0.0.1:8765/v1/mjpeg`

再输出 WHEP/RTSP。媒体 profile 只影响 APP 看到的预览流，不改变识别和测距所使用的 RGBD bundle。

### 4.3 深度来源

深度来自 `TYI_VLN` 缓存的 RealSense aligned RGBD bundle，不来自 RTSP，也不使用 LiDAR fallback。

当前默认参数：

- RGB/depth 分辨率：`640x480`
- 预览目标帧率：`REALSENSE_RGBD_STREAM_FPS=15`
- RGBD 对齐缓存帧率：`REALSENSE_RGBD_ALIGN_FPS=4`
- MJPEG 限速：`REALSENSE_MJPEG_MAX_FPS=15`
- JPEG 编码：`REALSENSE_JPEG_QUALITY=75`、`REALSENSE_JPEG_OPTIMIZE=false`、`REALSENSE_JPEG_SUBSAMPLING=2`

识别链路必须使用 RGBD bundle 中的 RGB JPEG 和同 bundle 的 depth。预览画面允许走 color-only 快路径，但不能被用于测距或目标三维解算。

## 5. 识别与测距链路

### 5.1 APP 到板端

推荐产品链路：

1. APP 实时视频页面播放 `control-gateway` 协商得到的 WHEP 视频。
2. 用户输入自然语言，例如 `袋装橘子`。
3. APP 调用：
   - `POST /v1/navigation/ground-query` 仅预览
   - 或 `POST /v1/navigation/ground-and-plan` 预览并生成规划目标
4. `control-gateway` 调用 `TYI_VLN /v1/qwen-ground`。
5. `TYI_VLN` 返回 grounding + depth + relative vector。
6. `control-gateway` 做可靠性判断后返回给 APP。

### 5.2 点选导航链路

点选导航与语言导航共享同一套 RGBD 快照一致性和三维锚定原则。区别是点选导航的二维输入来自 APP 点击位置，而不是 VLM grounding。

推荐产品链路：

1. APP 根据视频显示区域把点击位置换算成源视频像素，提交 `pixel` 和 `sourceResolution`。
2. APP 调用 `POST /v1/navigation/pixel-query` 做预览测距。
3. `control-gateway` 将源视频像素映射到当前 RealSense depth query 分辨率。
4. `control-gateway` 调用 `TYI_VLN /v1/query-depth`，优先使用 `TYI_VLN` 维护的 aligned RGBD bundle，而不是重新打开 RealSense。
5. `control-gateway` 调用 `pointcloud-gateway /v1/observation-snapshots`，使用 depth/RGB 时间戳恢复点击当时的 pose 和点云上下文。
6. `control-gateway` 使用 snapshot pose 将点击目标解算为 `targetWorldM`，并按 standoff 计算 `goalWorldM`。
7. APP 显示红点时调用 `POST /v1/navigation/project-goal`，围绕 `targetWorldM` 做实时反投影。
8. 用户点击“开始飞行”时，APP 发布预览阶段已经冻结的 `goalWorldM`，不再调用 `/v1/navigation/pixel-and-plan` 对旧点击像素二次测距。

`/v1/navigation/pixel-and-plan` 仍可作为服务端一体化接口保留，但在动态飞行 UI 中不建议用于“预览后确认飞行”的二次发布路径。原因是无人机晃动后，旧屏幕像素对应的实时画面内容可能已经变化，二次按像素测距会破坏预览红点、距离和最终飞行目标的一致性。

### 5.3 `TYI_VLN /v1/qwen-ground`

当前内部步骤：

1. 从最新可用的 RealSense aligned RGBD bundle 取 RGB JPEG。
2. 把 JPEG 发给 `QwenVLMClient.ground(...)`。
3. 模型返回 `bbox` / `point`。
4. `qwen_vlm_client.py` 将模型返回坐标统一换算为送检 RGB 快照的原图像素坐标。
5. `serve_depth.py` 在 bbox 内生成采样点：
   - grounding center
   - bbox center / upper_mid / lower_mid / left_mid / right_mid
   - bbox 中下部固定支持点
   - bbox 内容引导采样点
6. `depth_core.py` 在同一 RGBD bundle 的 color/depth 坐标下查询深度。
7. `serve_depth.py` 过滤掉不可信样本，选择最终目标点。
8. 输出：
   - `grounding.point`
   - `grounding.bbox`
   - `depth.selectedSample`
   - `relativeToBaseLinkM`
   - `distanceToBaseLinkM`

### 5.4 `control-gateway` 的二次判断

`control-gateway` 不会盲信 `TYI_VLN` 的每个深度结果，而是会检查：

- 是否存在有效深度
- 目标距离是否超出允许范围
- 深度 spread 是否过大
- 是否命中了“不可靠深度”告警

当前重要安全策略：

- 如果 `TYI_VLN` 返回 `no valid depth samples`，并且 RealSense 没有任何有效深度样本，则禁止继续使用错误的 LiDAR fallback 把目标投到别处。
- 这样可以避免“识别到了橘子袋，但最终飞向白墙”。

## 6. 动态飞行下的目标锚定与重投影

### 6.1 从像素点到三维目标锚点

VLM 返回的 `grounding.point` / `grounding.bbox`，以及点选导航中的 `pixel`，都只描述某一帧 RGB 快照里的二维位置。动态飞行时，如果 APP 继续把这个二维点固定画在实时视频上，就会出现“红点停留在识别/点选那一帧”的问题。

当前链路要求在板端完成以下转换：

1. 使用送检 RGB 所在的同一 RGBD bundle 查询深度。
2. 使用 RealSense 内参把目标像素反投影到 color optical frame。
3. 结合当时的机体位姿，把目标转换为相对 `base_link` / `livox_frame` / 地图坐标的三维锚点。
4. 后续规划、距离显示和红点显示都引用这个三维锚点，而不是引用旧帧二维像素。

点选导航中还有一个额外约束：确认飞行时应复用预览阶段得到的 `goalWorldM`，而不是重新调用 `/pixel-and-plan`。否则预览红点跟随的是快照 A 的三维目标，但飞行目标可能被重新计算成快照 B 的同一像素对应点。

### 6.2 实时画面中的反向投影

无人机继续晃动或飞行时，实时 RGB 画面已经不是送检帧。红点显示必须按当前相机姿态重新投影目标锚点：

1. 读取当前相机/机体位姿。
2. 将目标锚点变换到当前 camera optical frame。
3. 使用相机内参投影为当前 RGB 像素。
4. 如果目标在相机后方、超出视野或位姿过期，应隐藏红点或显示“目标暂不在视野内”，不能继续画旧像素。

### 6.3 当前统一坐标系

当前在 `TYI_VLN` 返回时，grounding 对外统一为：

- `coordinateSpace = pixel`

含义：

- `grounding.point`
- `grounding.bbox`

都已经是送检 RGB 快照原图上的真实像素坐标，可用于服务端深度采样和调试输出。APP 可以用于一次性调试叠加，但实时飞行中的红点锁定应优先使用服务端返回的三维锚点/重投影结果。

### 6.4 本次修复前的根因

2026-04-25 之前的老问题不是摄像头切错，而是 VLM 返回坐标系被错误解释：

- Qwen 实际返回的是 `0..999` 归一化坐标
- 旧代码只在“坐标超出原图宽高”时，才把它当成 `normalized_0_999`
- 对于例如 `x=460,y=600` 这样的值：
  - 它们小于 `1920x1080`
  - 但实际上仍是 `0..999` 坐标
- 结果旧代码把它误当成了原图像素
- 后续深度采样自然落到白墙或别的背景

### 6.5 本次坐标协议修复内容

已在 `docker/realsense-dev/qwen_vlm_client.py` 中修复：

1. 明确引入 `coordinate_mode`，当前默认：`normalized_0_999`
2. Prompt 明确要求模型输出：
   - `coordinateSpace: normalized_0_999`
   - `bbox` / `point` 一律使用 `0..999` 归一化坐标
3. 服务端统一把该坐标反算为原图像素
4. 对外返回：
   - `coordinateSpace = pixel`
   - `sourceCoordinateSpace = normalized_0_999`

这样可以保证：

- 模型侧协议稳定
- 服务端统一换算
- APP 不需要猜测模型坐标语义

## 7. 透明目标的深度策略

透明塑料袋是当前的难点目标。

原因：

- VLM 可以识别塑料袋里的橘子
- 但深度相机可能透过塑料袋看到背景
- 如果直接取 bbox 上沿/边缘深度，容易读到后方白墙

当前策略：

1. 增加 bbox 中下部的固定采样点
2. 增加 bbox 内容引导采样点
3. 过滤掉：
   - 靠 bbox 上沿的点
   - 靠 bbox 边缘的点
   - 明显偏离目标核心区域的点
4. 如果只剩背景点，返回：
   - `no valid depth samples`
   - 并在 warnings 中标记不可信原因

这套策略的目的不是“强行给距离”，而是“只在可信时给距离”。

## 8. APP 接口设计建议

### 8.1 推荐调用接口

语言导航预览目标：

- `POST /v1/navigation/ground-query`

语言导航预览并生成规划目标：

- `POST /v1/navigation/ground-and-plan`

点选导航预览目标：

- `POST /v1/navigation/pixel-query`

发布已经确认的导航目标：

- `POST /v1/navigation/publish-goal`

实时重投影目标红点：

- `POST /v1/navigation/project-goal`

请求字段建议：

- `instruction`
- 可选 `groundingInstruction`
- 可选 `snapshotSource`
- 可选 `depthRadiusPx`
- 可选 `groundingTimeoutSec`

说明：

- 对 APP 而言，应优先调用 `control-gateway`，而不是直接调用 `TYI_VLN`
- `snapshotSource` 当前产品链路建议保持默认，不要由 APP 自行切换底层源
- 点选导航预览后确认飞行时，应把 `pixel-query` 返回的 `goalWorldM` 传给 `publish-goal`
- 点选导航红点应使用 `pixel-query` 返回的 `targetWorldM` 做 `project-goal`，不要长期使用 `selectedPixel`

### 8.2 鉴权与角色

上述语言/点选导航接口要求：

- 已登录
- 具备 `operator` 角色

### 8.3 APP 页面建议

#### 实时视频页

建议显示：

- 主视频画面
- 导航输入框
- 最近一次 grounding 框与目标点
- 由当前位姿反向投影得到的实时红点
- 距离与相对位姿摘要
- 错误/告警提示

#### 交互约束

- 视频主画面只负责显示，不要为了语言导航暂停或重建视频流
- grounding 框和点可以直接使用服务端返回的送检快照像素坐标做调试展示
- 飞行中的红点锁定必须使用服务端三维锚点或服务端重投影结果，不能固定使用送检帧/点选帧像素
- 点选导航确认飞行不能再次按旧点击像素测距，应发布预览结果中的 `goalWorldM`
- 不要在客户端重新根据自己的截图做二次坐标换算

#### 建议展示的调试字段

在开发阶段建议保留：

- `snapshotSource`
- `requestedSnapshotSource`
- `grounding.coordinateSpace`
- `grounding.sourceCoordinateSpace`
- `depth.validSampleCount`
- `depth.rawValidSampleCount`
- `depth.warnings`
- `depth.error`
- `observationSnapshotId`
- `observationStampSec`
- `targetWorldM`
- `goalWorldM`
- `distanceToBaseLinkM`

### 8.4 典型错误码与展示策略

APP 应直接展示以下错误，不要吞掉：

- `NAVIGATION_TARGET_NOT_FOUND`
- `NAVIGATION_DEPTH_FAILED`
- `NAVIGATION_TARGET_DEPTH_UNAVAILABLE`
- `ODOM_STALE`
- `PLANNER_UNAVAILABLE`

其中：

- `target depth is unavailable because no valid RealSense depth samples were found for the grounded target`
  表示目标识别可能成功，但当前无法给出可信深度
- `target depth is unreliable and lidar projection fallback could not find a safe point`
  表示服务端拒绝了不安全 fallback

## 9. 当前已验证的工作结果

2026-04-25 现场已验证：

- `TYI_VLN` 单独持有 D435i，`media-gateway` 不再直接打开相机
- `TYI_VLN` 使用 RealSense RGBD 快照做 grounding 和测深
- 识别结果从送检帧二维像素提升为目标三维锚点
- 实时画面红点可随当前相机姿态反向投影，而不是停留在识别瞬间
- 点选导航已复用同一套 RGBD snapshot + observation snapshot + `targetWorldM` 实时反投影链路
- TYI_Dev 点选导航确认飞行已改为发布预览阶段冻结的 `goalWorldM`，避免无人机晃动后按旧像素二次测距
- 修复后返回示例：
  - `point ≈ (884, 643)`
  - `bbox ≈ (838,565)-(932,700)`
  - `sourceCoordinateSpace = normalized_0_999`
  - `distanceToBaseLinkM ≈ 3.01m`

这说明当前：

- VLM 送检 RGB 与服务端测深 depth 已经统一到同一 RGBD bundle
- 动态飞行时的目标显示和规划目标应围绕三维锚点更新
- 旧的“坐标偏到白墙”和“红点停留在旧帧”的架构问题已经被当前方案覆盖

## 10. 维护建议

1. 如果后续更换模型，优先保持 `normalized_0_999` 协议不变。
2. 如果 APP 端叠加框再次偏移，先检查是否错误使用了客户端自己的截图尺寸做二次换算。
3. 如果红点再次停留在识别瞬间，优先检查 APP 是否仍使用送检帧像素，而不是三维锚点/重投影结果。
4. 如果点选导航预览正确但开始飞行偏移，优先检查 APP 是否在确认飞行时重新调用 `/pixel-and-plan`，而不是发布预览阶段冻结的 `goalWorldM`。
5. 如果测距漂移，优先检查 `TYI_VLN /healthz` 中 `cachedRgbdBundle`、`latestBundleAgeSec`、`latestPreviewAgeSec` 是否正常，以及识别/点选请求是否确实使用 RealSense snapshot。
6. 如果透明目标误测回潮，优先检查 bbox 采样点是否重新放宽到上沿/边缘。
7. 产品链路里不要重新引入多进程直接打开 RealSense 的实现，否则可能再次造成黑屏、低帧率或 RGB/depth/pose 不一致。
8. 为了维持现场帧率，优先保留 color-only 预览快路径和低频 RGBD 对齐缓存；如果需要 15/30 FPS 稳定预览，应改为单 owner 进程直接 H264/GStreamer 输出，而不是增加 Python MJPEG 读者。

## 11. 相关文件

核心实现：

- `docker/realsense-dev/serve_depth.py`
- `docker/realsense-dev/qwen_vlm_client.py`
- `docker/realsense-dev/depth_core.py`
- `docker/control-gateway/app.py`
- `docker/pointcloud-gateway/app.py`
- `TYI_Dev/Features/PointCloud/PointCloudView.swift`
- `docker-compose.realsense.yml`

相关文档：

- `docs/zh_CN/realsense_d435i_planner_integration.md`
- `docs/zh_CN/APP接入协议.md`
- `docs/app/control_gateway_openapi.yaml`
- `docs/app/mobile_app_reference.md`
