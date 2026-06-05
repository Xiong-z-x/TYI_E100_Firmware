# APP 开发文档索引

本文用于给移动端开发同学快速定位必须阅读的文档，以及每份文档分别解决什么问题。

## 建议阅读顺序

1. `docs/zh_CN/系统架构与数据链路.md`
2. `docs/zh_CN/APP接入协议.md`
3. `docs/app/control_gateway_openapi.yaml`
4. `docs/zh_CN/TYI_VLN_语言导航与测距链路说明.md`，仅在启用视觉/VLM 扩展时必读
5. `docs/app/mobile_app_reference.md`

## 每份文档的用途

### `docs/zh_CN/系统架构与数据链路.md`

用于从工程视角理解当前完整链路：

- 容器与进程级模块划分
- 飞行定位、视频、点云、语言/点选导航、APP 接入数据流
- 配置项来源、重复配置、硬编码默认值和需要确认的链路点
- 当前默认 compose 与 media / vision / planner 可选 profile 的关系

APP 开发前必须先看这份文档，避免把 APP 设计成直接访问 ROS 或直接控制媒体容器。涉及跨模块联调、链路排查、配置改动和新成员接手项目时，也应先阅读这份文档。

### `docs/zh_CN/APP接入协议.md`

用于理解 APP 的接入时序：

- 如何做局域网发现
- 如何发起 pairing challenge
- 如何完成 pairing
- 如何保存与刷新令牌
- 如何建立 SSE 实时状态流
- 如何申请 WHEP 视频会话

APP 的网络层、鉴权层、播放器初始化逻辑应直接参考这份文档。

### `docs/app/control_gateway_openapi.yaml`

这是 APP 联调的主协议文件。

重点接口：

- `POST /v1/pairing/challenge`
- `POST /v1/pairing/complete`
- `POST /v1/session/refresh`
- `POST /v1/session/logout`
- `GET /v1/device/state`
- `GET /v1/health/detail`
- `GET /v1/stream/events`
- `GET /v1/logs/sources`
- `GET /v1/logs/recent`
- `POST /v1/webrtc/sessions`
- `GET /v1/pairing/clients`
- `DELETE /v1/pairing/clients/{clientId}`

APP 联调、Mock 数据生成、接口封装、错误码处理应以这份文件为准。

### `docs/zh_CN/TYI_VLN_语言导航与测距链路说明.md`

用于理解语言导航和点选导航功能当前到底如何工作：

- 实时视频和旁路截图如何统一
- VLM grounding 如何返回坐标
- 点选像素如何通过 RGBD snapshot 恢复为三维目标锚点
- 深度如何和 grounding 坐标对齐
- `control-gateway` 如何做可靠性判断与安全拒绝
- APP 页面应该展示哪些字段，哪些逻辑必须交给服务端
- 点选预览后为什么要发布冻结的 `goalWorldM`，而不是确认飞行时重新按旧像素测距

凡是涉及自然语言目标识别、点选导航、测距、红点/框叠加、导航预览、透明目标误判排查，都应优先阅读这份文档。

### `docs/app/mobile_app_reference.md`

这是英文版的移动端参考说明，适合接口封装、状态机和跨端实现时对照阅读。

## APP 开发时的强约束

- 不要直接访问 ROS 话题。
- 不要直接与 `media-gateway` 做业务鉴权。
- 视频主链路使用 `whepUrl`。
- 实时状态与日志使用 `SSE`。
- 所有业务入口统一经过 `control-gateway`。
- `refreshToken` 是轮换语义，刷新后旧令牌应立即丢弃。
- `logout` 后当前客户端的访问令牌和刷新令牌都要视为失效。

## 当前推荐对接范围

第一阶段建议 APP 先完成以下能力：

- 局域网发现
- 配对与登录
- 实时视频查看
- 状态与健康展示
- 日志查看
- 已配对客户端管理

以下能力当前不建议在 APP 首版直接做：

- 飞控类直接控制
- 航线规划
- 多机协同
- 绕过 `control-gateway` 的媒体或遥测接入
