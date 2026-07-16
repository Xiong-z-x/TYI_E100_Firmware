# mission_control 附件分析

分析日期：2026-07-16

## 当前处理状态

- 附件 `mission_control(1).zip` 已解压到本项目的 `mission_control/` 目录。
- 当前仅完成静态审查，没有把它复制到 `workspace/src/`，没有加入 Docker 构建，也没有启动 ROS、MAVROS 或飞行任务。
- 当前 Nano 未连接雷达和飞控，因此没有做任何真实飞行动作。
- 当前源码仓库的 `workspace/src/` 只有 `fast_lio`、`fastlio_to_mavros`、`livox_ros_driver2`、`mavlink`、`mavros` 和 `uav_base_bringup`，不包含该包；现有 ARM64 镜像不会编译它。

## 这是什么程序

这是一个 ROS1 Noetic/Catkin 的高层任务控制包，直接通过 MAVROS 控制 PX4 风格飞控。它不是单纯的“起飞按钮”，而是包含完整的参考任务：进入 OFFBOARD、解锁、爬升、按方形轨迹移动、悬停、下降和上锁。

主要入口：

- `mission_control_node`：提供私有服务 `~boot`；请求 `true` 会进入 OFFBOARD 并解锁，请求 `false` 会执行上锁。
- `mission_main`：按 `_mission` 和 `_auto_start` 参数运行任务。`status`、`hold` 不主动解锁；`test_flight`、`takeoff_move_land`、`takeoff_hover_land` 会执行参考飞行任务。

参考任务的默认参数：起飞高度 1.2 m，方形边长 1.0 m，每段移动 3 s，悬停 3 s，下降 3 s，位置设定值 20 Hz。相关配置见 `mission_control/config/mission_control.yaml`。

## 关键控制链

1. `mission_control/src/flight_interface.cpp:77-84` 的 `boot()` 等待 MAVROS/里程计就绪，连续发布当前位置 3 s，然后请求 `OFFBOARD` 并调用 `/mavros/cmd/arming` 解锁。
2. `mission_control/src/subtasks.cpp:55-68` 的 `test_flight` 调用 `boot()`，随后执行起飞、四段相对移动、悬停和着陆。
3. `mission_control/src/mission_context.cpp:244-270` 着陆后反复请求普通上锁；满足高度/速度或飞控 landed 状态后，延迟 1.5 s 允许使用 PX4 强制上锁命令。
4. 设定值话题为 `/mavros/setpoint_raw/local`，状态为 `/mavros/state`，里程计为 `/mavros/local_position/odom`，与当前项目的 MAVROS 接口命名一致。

## 必须先修复的安全问题

结论：当前版本只能作为“待审查的任务控制原型”，不能直接接入真实飞行。

1. **解锁前安全门不足**：`boot()` 只检查 MAVROS 和里程计是否有数据，没有检查飞控当前模式、`landed_state`、电池、EKF/定位质量、RC/急停、地理围栏、时间同步或速度/姿态状态。
2. **起飞失败后的状态不安全**：`mission_context.cpp:63-68` 起飞判定失败后只保持目标点 5 s 并返回失败，没有统一的降落、切换安全模式和上锁收尾；程序随后退出，飞控可能仍处于 OFFBOARD/已解锁状态。
3. **强制上锁风险高**：`flight_interface.cpp:65-73` 使用 MAV_CMD_COMPONENT_ARM_DISARM 和 PX4 强制参数 `21196`。虽然着陆判定后才调用，但当前判定依赖单一里程计高度/速度或 landed 状态，不能替代独立的接触/安全确认。
4. **进程退出保护不足**：没有看到节点退出、ROS 中断、通信丢失时的统一“保持/降落/切安全模式/上锁”策略；只依赖飞控自身 failsafe。
5. **任务参数缺少边界校验**：起飞高度、移动距离、持续时间可以通过 ROS 参数覆盖，但没有统一的正值、最大高度、最大水平范围和速度约束校验。
6. **文档入口与仓库不一致**：`README.md` 示例调用 `scripts/mission`，但当前仓库 `scripts/` 下没有该文件；直接照 README 执行会失败。

## 当前建议

- 在雷达和飞控接入前，只允许先做编译和静态/仿真测试；不要执行 `test_flight`，也不要调用 `mission_control/boot true`。
- 后续应在独立分支把包放入 `workspace/src/mission_control`，再重新构建 ARM64 镜像，验证 catkin 依赖和安装路径。
- 首个运行版本应增加显式 `arming_allowed`/物理确认门、飞控状态和 landed 检查、参数范围检查、异常统一降落/安全模式、节点退出保护，并修正 `scripts/mission` 入口。
- 只有 `status` 在无飞控/无雷达的仿真或完整 MAVROS 就绪环境中验证通过后，才进入受控的地面测试；真实起飞需要另行确认测试区域、飞控状态和急停方案。

