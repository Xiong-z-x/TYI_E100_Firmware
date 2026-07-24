# mission_control

该包在现有 MAVROS、FAST-LIO、PX4 手动 POSCTL 链路上增加一个默认停用的
OFFBOARD 测试任务。容器启动不会启动任务、发布 setpoint、切换模式或解锁。

## 唯一入口

必须从 Nano 仓库根目录调用：

```bash
./scripts/mission check
./scripts/mission test_flight --confirm-uav-051
```

第一条命令只能在遥控器 Kill 保持开启、Arm 开关保持低位时运行。它会检查：

- 飞控 UID `3761439192332449336`、SYSID `51`、COMPID `1`；
- POSCTL、未解锁、落地、遥控器和电池状态；
- LiDAR、Livox IMU、FAST-LIO、vision pose、PX4 local odometry 和 `/clock`；
- 静止位姿窗口、PX4 failsafe 参数及 setpoint 发布者冲突。

只有输出 `PRECHECK PASS - SAFE TO RELEASE KILL` 后，才可释放 Kill，并在
120 秒内运行第二条命令。预检收据使用一次后即删除。

## test_flight

任务固定保留初始偏航角：

1. 以当前位置预发 setpoint 3 秒；
2. 请求 OFFBOARD，确认后普通解锁；
3. 上升至相对起点 1.2 m，悬停 3 秒；
4. 以起飞点为基准飞行边长 1 m 的闭合正方形；
5. 回到起飞点上方悬停 3 秒；
6. 受控下降、确认落地、普通上锁。

## 中止规则

- RC Kill 或人工退出 OFFBOARD：立即停止任务并停止 setpoint，交还遥控器/PX4；
- MAVROS、飞控通信或定位失效：立即停止 setpoint，交给 PX4 failsafe；
- 普通任务失败且定位和飞控通信仍可靠：先受控降落，必要时请求 PX4
  `AUTO.LAND`，落地后仅使用普通上锁；
- 不包含强制上锁命令，也不包含旧的网络启动服务或任务别名。

任务日志保存到宿主机 `logs/mission-control/mission_*.csv`，最多保留 5 份。
