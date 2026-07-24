# UAV 051 安全任务飞行模块设计

日期：2026-07-24  
目标仓库：`Xiong-z-x/TYI_E100_Firmware`  
目标设备：UAV 051 Jetson Nano + MicoAir743v2 PX4 1.16.2 + Livox MID360

## 1. 目标

在不改变当前已验证手动 POSCTL 飞行链路的前提下，将
`mission_control(1).zip` 的完整任务逻辑接入现有 ROS1/Catkin/Docker 架构：

1. 进入 OFFBOARD；
2. 自动解锁；
3. 垂直起飞到相对高度 1.2 m；
4. 悬停 3 s；
5. 飞行边长 1.0 m 的方形；
6. 返回起飞点上方并悬停 3 s；
7. 垂直降落；
8. 普通上锁。

模块必须默认完全停用。模块未被显式调用时，不得启动任务节点、发布 setpoint、
切换模式、解锁或改变 PX4 参数。

## 2. 不在本次范围内

- 不改变 FAST-LIO、高频里程计、`fastlio_to_mavros` 或 MAVROS 核心算法。
- 不改变当前 MID360 网络、QGC 单播、飞控 UID/SYSID 或遥控器映射。
- 不自动执行真实飞行。
- 不使用 PX4 强制上锁参数 `21196`。
- 不把任务节点加入 `uav_base_bringup/base_stack.launch`。
- 不以 Nano 上电、容器启动或 QGC 连接作为自动起飞条件。

## 3. 选定架构

采用“同一 `flight-core` 镜像中的独立 Catkin 包”：

- 包路径：`workspace/src/mission_control/`
- 配置路径：`configs/mission-control/mission_control.yaml`
- 操作入口：`scripts/mission`
- 持久日志：`logs/mission-control/`
- ROS Master、MAVROS 和定位数据沿用现有 `flight-core`。

不新增常驻任务容器。`mission_control` 仅被编译进镜像，运行时必须由
`scripts/mission` 显式启动。

## 4. 版本与回滚

当前已验证手动飞行基线为 Nano 仓库提交：

```text
53de4c7a4d13ca66af4e6560be57fdff1359a8f7
```

实施前创建：

```text
tag: manual-flight-good-20260723
branch: feature/mission-control-safe
```

新镜像使用独立标签。回滚时切回上述基线提交并恢复旧 `flight-core` 镜像，不删除
任何飞行日志或设备配置。

## 5. 操作接口

### 5.1 第一阶段：Kill 保持状态下的只读预检

Windows 通过 SSH 执行：

```powershell
ssh -i C:\nano\ssh\nano_ed25519 -o UserKnownHostsFile=C:\nano\ssh\known_hosts -o StrictHostKeyChecking=yes tfboys_nano@192.168.0.108 "cd /home/tfboys_nano/TYI_E100_Firmware && ./scripts/mission check"
```

成功输出必须包含：

```text
PRECHECK PASS — SAFE TO RELEASE KILL
```

该命令不得创建 setpoint 发布者、调用模式服务或调用解锁服务。

### 5.2 第二阶段：解除 Kill 后显式启动

```powershell
ssh -i C:\nano\ssh\nano_ed25519 -o UserKnownHostsFile=C:\nano\ssh\known_hosts -o StrictHostKeyChecking=yes tfboys_nano@192.168.0.108 "cd /home/tfboys_nano/TYI_E100_Firmware && ./scripts/mission test_flight --confirm-uav-051"
```

第二阶段重新执行快速门控。只有确认字符串与全部门控同时满足时，才允许预发布
setpoint、请求 OFFBOARD 和解锁。

`scripts/mission` 使用文件锁保证只有一个任务实例运行。

## 6. 身份与输入基线

必须匹配：

- PX4 UID：`3761439192332449336`
- MAV_SYS_ID：`51`
- 飞控组件 ID：`1`
- MAVROS FCU：`/dev/ttyTHS0:921600`
- MID360 序列号：`147MDM5T0020051`
- MID360 当前有线地址：`192.168.2.205`
- Nano 有线地址：`192.168.2.50/24`
- RC Arm 映射：CH6
- RC Kill 映射：CH7
- RC Flight Mode 映射：CH5

上述设备身份必须来自配置并在运行时复核，不根据设备编号推导 IP。

## 7. 两阶段安全门

### 7.1 Kill 状态预检

预检必须同时满足：

1. MAVROS 状态与飞控信息新鲜；
2. UID、SYSID、组件 ID 精确匹配；
3. `connected=true`、`armed=false`、`manual_input=true`；
4. 当前模式为 `POSCTL`；
5. `landed_state=ON_GROUND`；
6. RC 数据新鲜、通道数不少于 7；
7. CH6 Arm 为低位；
8. CH7 Kill 为高位；
9. 电池数据新鲜且剩余量不低于 30%；
10. EKF 姿态、水平/垂直速度、相对水平位置及绝对高度有效；
11. 无 GPS glitch、无加速度计错误；
12. `/clock` 持续前进且未倒退；
13. MID360、IMU、FAST-LIO、Vision Pose、PX4 local odom 和 RC 频率达到门限；
14. 里程计时间戳新鲜、连续且数值有限；
15. 静止窗口内位移、速度和航向变化未超过门限；
16. 目标 setpoint 话题没有其他发布者；
17. PX4 OFFBOARD/RC failsafe 参数与第 8 节一致。

### 7.2 解除 Kill 后快速门控

启动命令必须重新确认：

1. 第一阶段结果未过期；
2. CH7 Kill 已解除；
3. CH6 Arm 仍为低位；
4. 飞控仍未解锁且仍为 POSCTL；
5. UID、RC、电池、EKF、里程计和 failsafe 参数仍有效；
6. 没有竞争 setpoint 发布者；
7. 命令包含精确确认字符串 `--confirm-uav-051`。

PX4 原生 pre-arm 检查仍是最终裁决。任务程序不得绕过 PX4 的解锁拒绝。

## 8. PX4 failsafe 基线

运行时必须读取并验证：

| 参数 | 期望值 | 含义 |
| --- | ---: | --- |
| `MAV_SYS_ID` | 51 | UAV 051 唯一系统 ID |
| `COM_OF_LOSS_T` | 1.0 | OFFBOARD 丢失超时 |
| `COM_OBL_RC_ACT` | 0 | OFFBOARD 丢失后进入 Position |
| `COM_RC_LOSS_T` | 0.5 | RC 丢失超时 |
| `NAV_RCL_ACT` | 3 | RC 丢失后 Land |
| `COM_RCL_EXCEPT` | 0 | OFFBOARD 不豁免 RC 丢失 |
| `COM_RC_OVERRIDE` | 3 | 自动模式和 OFFBOARD 均允许摇杆接管 |
| `COM_RC_STICK_OV` | 30.0 | 摇杆接管门限 30% |
| `RC_MAP_ARM_SW` | 6 | CH6 Arm |
| `RC_MAP_KILL_SW` | 7 | CH7 Kill |
| `RC_MAP_FLTMODE` | 5 | CH5 Flight Mode |
| `EKF2_EV_CTRL` | 15 | 外部视觉融合 |
| `EKF2_GPS_CTRL` | 0 | 不使用 GPS |

模块只读验证这些参数，不自动修改。

## 9. 任务状态机

```text
IDLE
  -> PRECHECK
  -> PRESTREAM
  -> OFFBOARD
  -> ARMING
  -> TAKEOFF
  -> HOVER_BEFORE_SQUARE
  -> SQUARE_LEG_1
  -> SQUARE_LEG_2
  -> SQUARE_LEG_3
  -> SQUARE_LEG_4
  -> RETURN_TO_ORIGIN
  -> HOVER_AFTER_SQUARE
  -> LANDING
  -> DISARMING
  -> COMPLETE
```

所有状态都有超时、成功条件、退出原因和日志记录。

## 10. 轨迹

进入 OFFBOARD 前，在当前位置和当前航向以 20 Hz 预发布 3 s。

轨迹基于起飞原点构造，不基于每段结束时的实际位置累加：

```text
P0 = (x0,     y0,     z0 + 1.2)
P1 = (x0+1.0, y0,     z0 + 1.2)
P2 = (x0+1.0, y0+1.0, z0 + 1.2)
P3 = (x0,     y0+1.0, z0 + 1.2)
P4 = (x0,     y0,     z0 + 1.2)
```

全程保持起飞时航向。坐标使用 MAVROS 的 ROS ENU 语义。

## 11. 人工接管

人工控制优先级最高。进入 OFFBOARD 后若发生以下任一事件：

- 飞控模式不再是 OFFBOARD；
- RC 模式开关切回 POSCTL；
- 摇杆超过 PX4 接管门限；
- Kill 被触发；
- 飞控已经解除武装；

任务立即停止继续轨迹，不请求重新进入 OFFBOARD，不重新解锁，不尝试覆盖人工模式。

## 12. 失败收尾

### 12.1 解锁前失败

直接退出，不改变模式，不解锁，不发布飞行 setpoint。

### 12.2 普通任务失败且控制链可靠

顺序如下：

1. 停止后续方形任务；
2. 保持当前可靠位置；
3. 执行受控下降；
4. 若 OFFBOARD 下降未完成，请求 `AUTO.LAND`；
5. 等待稳定落地；
6. 仅请求普通上锁；
7. 记录失败阶段、降落结果和最终武装状态。

### 12.3 MAVROS、定位或飞控通信失效

停止任务逻辑和 setpoint 发布，让 PX4 根据第 8 节的配置处理 OFFBOARD/RC
failsafe。程序不得使用过期定位继续飞行。

### 12.4 进程退出

SIGINT/SIGTERM 只设置退出请求，由主状态机判断：

- 未解锁：直接退出；
- 已解锁且控制链可靠：进入受控失败降落；
- 控制链不可靠：停止 setpoint，交给 PX4 failsafe。

进程崩溃时 setpoint 自然停止，由 PX4 OFFBOARD loss failsafe 接管。

## 13. 日志

日志写入：

```text
logs/mission-control/
```

每次执行至少记录：

- 飞控身份；
- 预检每一项的通过/失败原因；
- 状态机状态和时间；
- 飞控模式与武装状态；
- RC Arm/Kill/Mode 通道；
- 当前位置、速度、航向、目标点；
- EKF 标志、电池、数据新鲜度；
- 服务请求及 PX4 返回值；
- 人工接管或失败原因；
- 最终模式、落地状态和武装状态。

日志不得记录密码、私钥或其他认证材料。

## 14. 测试

### 14.1 单元测试

覆盖：

- 错误 UID/SYSID；
- Kill、Arm 或模式状态错误；
- RC、状态、电池、EKF、里程计过期；
- 电池低于 30%；
- EKF 标志错误；
- failsafe 参数不匹配；
- 竞争 setpoint 发布者；
- 参数边界；
- 方形航点和返回原点；
- 人工接管优先；
- 普通失败进入降落；
- 链路失效停止 setpoint；
- 禁止强制上锁。

### 14.2 隔离 ROS 集成测试

Fake MAVROS 使用测试命名空间，不能连接真实 `/mavros`。验证：

- 默认和 `check` 模式不发布 setpoint；
- `check` 不调用模式或解锁服务；
- 正常服务调用顺序；
- OFFBOARD/arming 拒绝；
- 中途 POSCTL 接管；
- 普通失败降落；
- 数据失效停止 setpoint。

### 14.3 构建与静态检查

- Catkin 测试全部通过；
- ARM64 `flight-core` 镜像构建退出码为 0；
- Compose 配置可解析；
- Shell 脚本语法检查通过；
- C++ 编译启用 `-Wall -Wextra -Wpedantic`；
- 不存在默认启动 mission 节点的 launch/compose 修改。

### 14.4 Nano 默认关闭验证

部署后保持 Kill：

- 三个基础容器 healthy；
- 手动链路节点和频率不退化；
- `mission_control` 节点不存在；
- setpoint 发布者为 0；
- `scripts/mission check` 只读通过；
- 未切换 OFFBOARD、未解锁、未转动电机。

### 14.5 拆桨地面测试

真实飞行前必须由现场人员确认拆桨，再验证：

- CH6 低位与 MAVROS 解锁服务是否冲突；
- OFFBOARD 进入/退出；
- 普通解锁和普通上锁；
- 模式开关与摇杆接管；
- Kill 最高优先级；
- 失败收尾状态机。

拆桨测试通过不等于授权真实飞行。

## 15. 完成标准

本次实现完成必须同时满足：

1. 手动 POSCTL 基线可一条命令回滚；
2. 模块默认关闭且无 setpoint 发布者；
3. 全部单元和 Fake MAVROS 测试通过；
4. ARM64 镜像构建成功；
5. Nano 上三基础服务 healthy；
6. Kill 状态下只读预检通过；
7. 没有执行真实 `test_flight`。

真实起飞属于后续单独的现场授权和检查点。

## 16. 依据

- PX4 v1.16 Offboard：
  `https://docs.px4.io/v1.16/en/flight_modes/offboard`
- PX4 v1.16 参数：
  `https://docs.px4.io/v1.16/en/advanced_config/parameter_reference`
- PX4 v1.16 Safety：
  `https://docs.px4.io/v1.16/en/config/safety`
- PX4 MAVROS C++ Offboard 示例：
  `https://docs.px4.io/v1.16/en/ros/mavros_offboard_cpp.html`
