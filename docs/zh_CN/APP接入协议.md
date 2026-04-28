# APP 接入协议

## 接入顺序

1. 通过 UDP `19001` 发送 `discover` 做局域网发现。
2. 调用 `POST /v1/pairing/challenge` 获取 `nonce`、`clientId`、`deviceFingerprint`、`supportedProofs`。
3. 使用共享码，或使用设备密钥计算 `HMAC-SHA256(nonce:clientId)`，调用 `POST /v1/pairing/complete`。
4. 保存 `accessToken` 与 `refreshToken`。
5. 调用 `GET /v1/device/state` 获取首帧状态快照。
6. 调用 `GET /v1/stream/events?topics=state,health,alarm,log` 建立 SSE 实时通道。
7. 调用 `POST /v1/webrtc/sessions` 申请媒体会话，优先使用返回的 `whepUrl` 播放。

## 鉴权

- `accessToken` 用于所有受保护接口。
- `refreshToken` 仅用于 `POST /v1/session/refresh` 与 `POST /v1/session/logout`。
- `refresh` 为轮换语义。服务端会签发一组新的 `accessToken` / `refreshToken`，并使同一客户端旧令牌失效。
- `logout` 为客户端会话失效语义。服务端会撤销当前客户端已签发的访问与刷新令牌。
- `viewer` 可查看状态、日志、视频。
- `operator` 预留给后续调试或控制能力。
- `maintainer` 额外可读取已配对客户端列表。

## 已配对客户端管理

- `GET /v1/pairing/clients` 用于读取当前已配对客户端列表。
- `DELETE /v1/pairing/clients/{clientId}` 用于解绑一个客户端，并同时撤销该客户端对应的访问令牌与刷新令牌。
- 上述接口要求 `maintainer` 角色。
- 网关会定期清理过期 access token、refresh token 和长期未使用的配对客户端，避免开发期反复安装 App 后状态文件无限增长。
- `GET /healthz` 会返回 `pairedClientCount`、`pairedClientLimit`、`refreshTokenCount`，APP 或现场工具可用这些字段判断是否接近配对容量上限。

## 配对与量产安全配置

当前配对参数由 `.env` 注入 `control-gateway`，默认保持开发环境兼容：

- `CONTROL_GATEWAY_PAIRING_MODE`：当前现场为 `development`，量产前应切换为受控模式。
- `CONTROL_GATEWAY_NONCE_TTL_SEC`：pairing challenge 有效期。
- `CONTROL_GATEWAY_ACCESS_TOKEN_TTL_SEC`：access token 有效期。
- `CONTROL_GATEWAY_REFRESH_TOKEN_TTL_SEC`：refresh token 有效期。
- `CONTROL_GATEWAY_MAX_PAIRED_CLIENTS`：最大保留配对客户端数量。
- `CONTROL_GATEWAY_STALE_CLIENT_TTL_SEC`：长期未使用客户端的保留时间。
- `CONTROL_GATEWAY_REQUIRE_PRODUCTION_SECURITY`：设为 `true` 后，若仍使用开发配对模式、默认共享码或默认点云共享密钥，`control-gateway` 会拒绝启动。

量产切换顺序应为：先轮换 `CONTROL_GATEWAY_SHARED_CODE` 和 `POINTCLOUD_GATEWAY_SHARED_SECRET`，确认 App 可重新配对和点云 ticket 正常，再开启 `CONTROL_GATEWAY_REQUIRE_PRODUCTION_SECURITY=true`。不要在文档、日志或聊天中明文传播共享码、token 或共享密钥。

## 实时推送

SSE 端点：

- `GET /v1/stream/events`

可订阅主题：

- `state`
- `health`
- `alarm`
- `log`

连接建立后，服务端会先推送一次完整 `snapshot`，之后按事件增量推送，并定期发送 `heartbeat` 保活。

## 健康判定

APP 重点关注以下字段：

- `health.ok`
- `health.flightStateStale`
- `health.mediaStateStale`
- `health.systemStateStale`
- `media.onlineProfiles`
- `media.paths[*].ready`
- `media.paths[*].online`

如果 `health.ok=false` 但 `media.ok=true`，通常表示飞行状态或系统状态已过期，不代表视频一定不可看。

## 运行模式与防误起飞

板端提供两个运行模式：

- `dev`：默认模式。允许 APP 和调试工具使用视频、点云、状态、语言识别、点选测距、目标预览和重投影；禁止发布 planner goal，planner setpoint 只允许进入 shadow topic。
- `flight`：正常飞行模式。只有显式设置 `TYI_OPERATION_MODE=flight` 后，`ground-and-plan`、`pixel-and-plan`、`publish-goal`、`hold-position` 才允许发布 planner goal。若还要让 planner 真实驱动 PX4，需要在受控飞行测试中把 `TYI_PLANNER_CMD_OUTPUT_TOPIC` 从 shadow topic 切到真实 MAVROS setpoint topic。

调试阶段不要把 `TYI_OPERATION_MODE` 设为 `flight`。APP 应读取 `GET /v1/discovery/self` 或 `GET /v1/device/state` 中的 `operationMode`，并在 `dev` 下隐藏或禁用“开始飞行”类按钮。

## 语言/点选导航与动态目标显示

APP 发起语言导航时，应把识别、测距和飞行目标都交给 `control-gateway`：

- 仅预览识别结果：`POST /v1/navigation/ground-query`
- 识别并生成规划目标：`POST /v1/navigation/ground-and-plan`

APP 发起点选导航时，也应把测距、快照恢复和飞行目标计算交给 `control-gateway`：

- 点选预览测距：`POST /v1/navigation/pixel-query`
- 发布已确认目标：`POST /v1/navigation/publish-goal`
- 实时红点重投影：`POST /v1/navigation/project-goal`

动态飞行场景下，APP 不应把送检帧或点选帧的二维像素长期固定画在实时视频上。服务端会基于同一 RealSense RGBD 快照完成目标深度查询、observation snapshot 和三维锚定；APP 应优先使用服务端返回的目标锚点、距离、相对位姿和重投影结果进行显示。若目标暂时离开当前视野，应显示“目标暂不在视野内”，而不是继续显示旧红点。

点选导航的确认飞行路径必须保持预览和发布一致：`pixel-query` 成功后，APP 应保存返回的 `targetWorldM` 和 `goalWorldM`；红点跟踪使用 `targetWorldM` 调用 `project-goal`；点击“开始飞行”时发布同一次预览冻结的 `goalWorldM`。不要在确认飞行时再次用原始点击像素调用 `/v1/navigation/pixel-and-plan` 做二次测距，否则无人机晃动后同一屏幕像素可能已经指向另一个空间点。


## 视频接入

- 低延迟主路径：`whepUrl`
- 调试备用路径：`rtspUrl`
- APP 创建会话前应读取 `GET /v1/device/state` 或 `GET /v1/cameras` 中的 `media.availableProfiles` / `media.selectedProfile`
- `POST /v1/webrtc/sessions` 的 `profile` 应优先使用服务端上报的可用 profile，不要硬编码 `1080p30`

2026-04-25 现场当前可用 profile 为 `480p15`，对应路径 `camera-480p15`。`media-gateway` 已对旧客户端请求的 `1080p30` 做 fallback，但新客户端仍应按能力协商。

当前 `answer` / `ice` 接口仅保留兼容，不是推荐路径。WHEP 模式下，APP 不需要自行处理 SDP / ICE 交换。
