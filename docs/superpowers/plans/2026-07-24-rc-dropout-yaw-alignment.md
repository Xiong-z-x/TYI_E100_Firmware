# RC Dropout And Yaw Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve PX4 RC/Kill failsafe authority while tolerating the measured sub-second SBUS dropout and rotate the test-flight square into the takeoff body heading.

**Architecture:** Keep the existing mission state machine and PX4 failsafe actions unchanged. Raise only `COM_RC_LOSS_T` from `0.5` to `1.0`, enforce that value in the preflight gate, and rotate the existing five-point square by the captured initial yaw before publishing position targets.

**Tech Stack:** ROS Noetic, MAVROS, PX4 v1.16.2, C++14, GoogleTest, Docker Compose on Jetson Nano.

---

### Task 1: Lock The Desired Behavior With Failing Tests

**Files:**
- Modify: `workspace/src/mission_control/test/test_mission_policy.cpp`
- Modify: `workspace/src/mission_control/test/test_safety_gate.cpp`

- [x] **Step 1: Add the yaw-aligned square test**

Call `MissionPolicy::squareWaypoints(origin, height, side_length, yaw)` and assert that yaw `pi/2` rotates the first side from map `+X` to map `+Y`, while preserving height and returning to the origin.

- [x] **Step 2: Add the RC-loss contract test**

Assert:

```cpp
EXPECT_DOUBLE_EQ(1.0, config.expected_px4_params.at("COM_RC_LOSS_T"));
EXPECT_DOUBLE_EQ(3.0, config.expected_px4_params.at("NAV_RCL_ACT"));
EXPECT_DOUBLE_EQ(0.0, config.expected_px4_params.at("COM_RCL_EXCEPT"));
```

- [x] **Step 3: Run the focused tests and verify RED**

Run in the ARM64 build environment:

```bash
catkin_make run_tests_mission_control
```

Expected: the yaw-aware API is missing and/or `COM_RC_LOSS_T` is still `0.5`.

### Task 2: Implement The Minimal Mission Changes

**Files:**
- Modify: `workspace/src/mission_control/include/mission_control/mission_policy.h`
- Modify: `workspace/src/mission_control/src/mission_policy.cpp`
- Modify: `workspace/src/mission_control/src/subtasks.cpp`
- Modify: `workspace/src/mission_control/src/safety_gate.cpp`

- [x] **Step 1: Rotate square offsets by initial yaw**

Use:

```cpp
const double cos_yaw = std::cos(yaw);
const double sin_yaw = std::sin(yaw);
const auto rotate = [&](double forward, double left) {
  return LocalPoint{
      origin.x + forward * cos_yaw - left * sin_yaw,
      origin.y + forward * sin_yaw + left * cos_yaw,
      origin.z + height};
};
```

The ordered offsets remain `(0,0)`, `(d,0)`, `(d,d)`, `(0,d)`, `(0,0)`, so the first leg is body-forward and the second is body-left.

- [x] **Step 2: Pass the captured takeoff yaw**

Call:

```cpp
MissionPolicy::squareWaypoints(
    policy_origin, takeoff_height_m_, move_distance_m_, mission.initialYaw());
```

- [x] **Step 3: Raise only the expected RC-loss timeout**

Change `COM_RC_LOSS_T` to `1.0`. Keep `NAV_RCL_ACT=3` and `COM_RCL_EXCEPT=0` unchanged so RC loss still invokes PX4 landing and is not exempted in OFFBOARD.

- [x] **Step 4: Run all mission-control tests and verify GREEN**

Run:

```bash
catkin_make run_tests_mission_control
catkin_test_results
```

Expected: zero failures.

### Task 3: Build, Deploy, And Verify Without Flight

**Files:**
- Modify: `.env`
- Modify: `PROJECT_DEVICE_CONFIG.md`

- [x] **Step 1: Set the next immutable image tag**

Set `IMAGE_NAME=tyi/tyi_e100:0.1.2-mission-safe-r5`.

- [x] **Step 2: Build on the Jetson Nano**

Build the ARM64 mission image from the verified current base and confirm the resulting tag exists.

- [x] **Step 3: Deploy with Kill engaged**

Confirm the vehicle is disarmed, landed, in `POSCTL`, and Kill is engaged. Replace only `flight-core`; do not start a mission.

- [x] **Step 4: Apply and read back the PX4 parameter**

Set `COM_RC_LOSS_T=1.0` through MAVROS, then read back:

```text
COM_RC_LOSS_T=1.0
NAV_RCL_ACT=3
COM_RCL_EXCEPT=0
```

- [x] **Step 5: Run non-flight precheck**

Run `./scripts/mission check` while Kill remains engaged. Expected: precheck passes, no arming, no OFFBOARD transition, and no mission setpoint publisher remains active.

- [x] **Step 6: Record evidence and commit**

Record the ULog root cause, exact parameter delta, test/build results, and rollback tag. Commit and push the feature branch only after verification.
