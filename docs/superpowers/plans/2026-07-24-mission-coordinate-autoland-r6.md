# Mission Coordinate Navigation And Auto-Land r6 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align the test mission to local coordinate axes, verify every go-to endpoint, and let PX4 Land mode stop the motors after touchdown.

**Architecture:** Keep the existing OFFBOARD takeoff and waypoint publisher. Add one pure yaw-quantization policy, make `moveToPoint` wait for coordinate convergence, then hand normal landing to PX4 `AUTO.LAND` and its existing auto-disarm timer.

**Tech Stack:** ROS Noetic, MAVROS, PX4 v1.16.2, C++14, GoogleTest, Python unittest, ARM64 Docker.

---

### Task 1: Add Focused Regression Tests

**Files:**
- Modify: `workspace/src/mission_control/test/test_mission_policy.cpp`
- Modify: `workspace/src/mission_control/test/test_safety_gate.cpp`
- Modify: `workspace/src/mission_control/test/test_default_off.py`

- [x] Add a test that expects `nearestCardinalYaw(2.899)` to equal `pi`.
- [x] Add a test that expects `COM_DISARM_LAND=2.0` while preserving all
  existing RC/Kill failsafe parameters.
- [x] Add a static safety test that requires normal landing to request
  `AUTO.LAND`, disable the setpoint publisher, and contain no forced-disarm
  command or ground-target hold.
- [x] Run the focused tests in the current ARM64 image and verify they fail for
  the missing r6 behavior.

### Task 2: Implement Coordinate Go-To And Auto-Land

**Files:**
- Modify: `workspace/src/mission_control/include/mission_control/mission_policy.h`
- Modify: `workspace/src/mission_control/src/mission_policy.cpp`
- Modify: `workspace/src/mission_control/include/mission_control/mission_context.h`
- Modify: `workspace/src/mission_control/src/mission_context.cpp`
- Modify: `workspace/src/mission_control/src/subtasks.cpp`
- Modify: `workspace/src/mission_control/src/safety_gate.cpp`
- Modify: `workspace/src/mission_control/config/mission_control.yaml`

- [x] Implement `nearestCardinalYaw` with `round(yaw / (pi/2)) * (pi/2)`.
- [x] Set the commanded mission yaw to that result during the first hover.
- [x] After every coordinate ramp, call `waitUntilNear` with navigation
  tolerance parameters and fail safely on timeout.
- [x] Replace normal ground-target descent with:

```cpp
if (!flight_.requestMode("AUTO.LAND")) {
  return failTask("PX4 AUTO.LAND request failed");
}
mission_active_ = false;
flight_.disableSetpointPublisher();
if (!flight_.waitLanded(ros::Duration(auto_land_wait_sec_),
                        ros::Duration(landing_stable_sec_))) {
  failure_action_ = FailureAction::StopSetpoints;
  return false;
}
return !disarm ||
       flight_.waitDisarmed(ros::Duration(disarm_timeout_sec_));
```

- [x] Require `COM_DISARM_LAND=2.0` in the UAV051 preflight snapshot.
- [x] Run all mission-control tests and compile the complete mission node.

### Task 3: Deploy And Record

**Files:**
- Modify: `.env`
- Modify: `PROJECT_DEVICE_CONFIG.md`

- [ ] Build immutable image
  `tyi/tyi_e100:0.1.2-mission-safe-r6` from the verified base image.
- [ ] Recreate only `flight-core` while the vehicle is disarmed and landed.
- [ ] Read back `COM_DISARM_LAND=2.0` and the unchanged RC/Kill failsafe
  parameters.
- [ ] Run `./scripts/mission check` only when RC Kill is available and engaged;
  otherwise leave no receipt and report that runtime precheck is pending.
- [ ] Verify no mission node and no publisher on
  `/mavros/setpoint_raw/local`.
- [ ] Record ULog ID 8 evidence, commits, image ID, rollback tag, and the
  ground-verification boundary; push the feature branch and r6 tag.
