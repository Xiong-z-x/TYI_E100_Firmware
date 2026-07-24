# UAV 051 Safe Mission Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the complete `mission_control(1).zip` square-flight mission into the verified UAV 051 Nano stack while preserving manual POSCTL as the default and adding two-stage preflight, manual takeover, and deterministic failure cleanup.

**Architecture:** Build `mission_control` as an optional ROS1 Catkin package inside the existing `flight-core` image. The package is never launched by the base stack; a host-side `scripts/mission` wrapper provides `check` and explicitly confirmed `test_flight` commands. Safety decisions are separated into pure, unit-tested policy code, while ROS/MAVROS I/O remains in `FlightInterface`.

**Tech Stack:** ROS1 Noetic, Catkin isolated build, C++14, MAVROS, GTest/Rostest, Bash, Docker Compose, Jetson ARM64.

---

### Task 1: Preserve the manual-flight baseline and create an isolated feature worktree

**Files:**
- Reuse: `.gitignore`
- Create branch: `feature/mission-control-safe`
- Create tag: `manual-flight-good-20260723`

- [ ] **Step 1: Fetch the live Nano commit into the Windows repository**

Run from the repository root:

```powershell
$env:GIT_SSH_COMMAND='ssh -i C:\nano\ssh\nano_ed25519 -o UserKnownHostsFile=C:\nano\ssh\known_hosts -o StrictHostKeyChecking=yes'
git fetch ssh://tfboys_nano@192.168.0.108/home/tfboys_nano/TYI_E100_Firmware develop:refs/remotes/nano/develop
```

Expected: `refs/remotes/nano/develop` resolves to
`53de4c7a4d13ca66af4e6560be57fdff1359a8f7`.

- [ ] **Step 2: Create the immutable manual-flight tag**

```powershell
git tag -a manual-flight-good-20260723 53de4c7a4d13ca66af4e6560be57fdff1359a8f7 -m "Verified manual POSCTL flight baseline"
```

Expected: `git rev-parse manual-flight-good-20260723^{}` prints the full baseline SHA.

- [ ] **Step 3: Verify the project-local worktree directory is ignored**

```powershell
git check-ignore .worktrees
```

Expected: `.worktrees` is printed. If it is not ignored, add `/.worktrees/` to
`.gitignore`, commit that isolated fix, then continue.

- [ ] **Step 4: Create the implementation worktree**

```powershell
git worktree add .worktrees/mission-control-safe -b feature/mission-control-safe refs/remotes/nano/develop
```

Expected: the new worktree HEAD is `53de4c7a4d13`.

- [ ] **Step 5: Import the reviewed attachment and approved design**

```powershell
git -C .worktrees/mission-control-safe cherry-pick 1420913 b47dc60
```

Expected: the worktree contains `mission_control/`,
`MISSION_CONTROL_ANALYSIS.md`, and the approved design document.

### Task 2: Move the package into Catkin and establish a failing safety-policy test

**Files:**
- Move: `mission_control/` → `workspace/src/mission_control/`
- Create: `workspace/src/mission_control/include/mission_control/safety_gate.h`
- Create: `workspace/src/mission_control/src/safety_gate.cpp`
- Create: `workspace/src/mission_control/test/test_safety_gate.cpp`
- Modify: `workspace/src/mission_control/CMakeLists.txt`
- Modify: `workspace/src/mission_control/package.xml`

- [ ] **Step 1: Move the reviewed package without changing behavior**

```powershell
git mv mission_control workspace/src/mission_control
```

- [ ] **Step 2: Write the first failing pure-policy test**

Create `test/test_safety_gate.cpp` with a table-driven test that constructs a
fully valid `SafetySnapshot`, then verifies rejection for wrong UID, wrong
SYSID, armed state, wrong mode, stale RC, low battery, invalid estimator,
stale odometry, wrong Kill phase, competing setpoint publisher, and mismatched
PX4 failsafe parameters.

The minimum API exercised by the test is:

```cpp
SafetyConfig config = SafetyConfig::uav051Defaults();
SafetySnapshot snapshot = makeValidSnapshot();
GateResult result = SafetyGate(config).evaluate(snapshot, GatePhase::KillEngaged);
EXPECT_TRUE(result.passed);
EXPECT_TRUE(result.failures.empty());
```

- [ ] **Step 3: Register the test before production implementation**

Add to `CMakeLists.txt`:

```cmake
if(CATKIN_ENABLE_TESTING)
  catkin_add_gtest(test_safety_gate
    test/test_safety_gate.cpp
    src/safety_gate.cpp
  )
  if(TARGET test_safety_gate)
    target_link_libraries(test_safety_gate ${catkin_LIBRARIES})
  endif()
endif()
```

- [ ] **Step 4: Run the package test and verify RED**

Run in a disposable ROS Noetic build environment:

```bash
catkin_make_isolated --install --pkg mission_control --cmake-args -DCMAKE_BUILD_TYPE=Release -DCATKIN_ENABLE_TESTING=ON
```

Expected: compilation fails because `SafetyConfig`, `SafetySnapshot`,
`GateResult`, and `SafetyGate` are not implemented.

- [ ] **Step 5: Implement the minimal pure safety policy**

`safety_gate.h/.cpp` must define:

```cpp
enum class GatePhase { KillEngaged, KillReleased };

struct GateResult {
  bool passed{false};
  std::vector<std::string> failures;
};

class SafetyGate {
public:
  explicit SafetyGate(const SafetyConfig& config);
  GateResult evaluate(const SafetySnapshot& snapshot, GatePhase phase) const;
};
```

The implementation performs no ROS calls and returns every failed predicate,
not only the first failure.

- [ ] **Step 6: Run the test and verify GREEN**

Expected: `test_safety_gate` passes with zero failures.

- [ ] **Step 7: Commit**

```bash
git add workspace/src/mission_control
git commit -m "feat: add UAV 051 mission safety policy"
```

### Task 3: Add read-only MAVROS observation and two-phase preflight

**Files:**
- Modify: `workspace/src/mission_control/include/mission_control/flight_interface.h`
- Modify: `workspace/src/mission_control/src/flight_interface.cpp`
- Create: `workspace/src/mission_control/include/mission_control/preflight.h`
- Create: `workspace/src/mission_control/src/preflight.cpp`
- Create: `workspace/src/mission_control/test/test_preflight.cpp`
- Modify: `workspace/src/mission_control/src/main_mission_node.cpp`
- Modify: `workspace/src/mission_control/CMakeLists.txt`
- Modify: `workspace/src/mission_control/package.xml`

- [ ] **Step 1: Write failing preflight tests**

Use a fake observation provider to verify:

```cpp
EXPECT_EQ(PreflightExit::PassKillEngaged,
          preflight.run(GatePhase::KillEngaged));
EXPECT_FALSE(fake.setpointAdvertised());
EXPECT_EQ(0, fake.modeRequestCount());
EXPECT_EQ(0, fake.armRequestCount());
```

Also verify that a stale first-stage receipt, wrong UID, wrong RC phase, and a
competing publisher fail without side effects.

- [ ] **Step 2: Verify RED**

Expected: test compilation fails because `Preflight` and its observation
interface do not exist.

- [ ] **Step 3: Extend `FlightInterface` in read-only mode**

Subscribe to:

- `/mavros/state`
- `/mavros/extended_state`
- `/mavros/estimator_status`
- `/mavros/rc/in`
- `/mavros/battery`
- `/mavros/local_position/odom`

Call:

- `/mavros/vehicle_info_get`
- `/mavros/param/get`

Track callback arrival using monotonic/wall receipt time. Do not advertise
`/mavros/setpoint_raw/local` in the constructor. Add
`enableSetpointPublisher()` and `disableSetpointPublisher()` so the publisher
exists only after the Kill-released gate passes.

- [ ] **Step 4: Add identity, failsafe, freshness, stability, and publisher checks**

The implementation must check the values in the approved design, query the
configured PX4 parameters read-only, inspect ROS master publishers, and produce
one failure line per failed item.

- [ ] **Step 5: Add first-stage receipt**

On successful `check`, write a receipt under
`/tmp/mission_control/precheck.receipt` containing UID, SYSID, process boot
time, and completion time. Use mode `0600`. `test_flight` accepts only a
matching receipt not older than 120 seconds.

- [ ] **Step 6: Verify GREEN**

Expected: `test_preflight` and `test_safety_gate` pass; check mode never
advertises a setpoint publisher.

- [ ] **Step 7: Commit**

```bash
git add workspace/src/mission_control
git commit -m "feat: add two-stage MAVROS preflight gate"
```

### Task 4: Harden the square mission and failure state machine

**Files:**
- Modify: `workspace/src/mission_control/include/mission_control/flight_interface.h`
- Modify: `workspace/src/mission_control/include/mission_control/mission_context.h`
- Modify: `workspace/src/mission_control/include/mission_control/subtasks.h`
- Modify: `workspace/src/mission_control/src/flight_interface.cpp`
- Modify: `workspace/src/mission_control/src/mission_context.cpp`
- Modify: `workspace/src/mission_control/src/subtasks.cpp`
- Modify: `workspace/src/mission_control/src/main_mission_node.cpp`
- Create: `workspace/src/mission_control/test/test_mission_policy.cpp`
- Modify: `workspace/src/mission_control/CMakeLists.txt`

- [ ] **Step 1: Write failing mission-policy tests**

Tests must prove:

```cpp
EXPECT_EQ(Action::YieldToPilot,
          policy.onModeChanged("OFFBOARD", "POSCTL"));
EXPECT_EQ(Action::ControlledLand,
          policy.onTaskFailure(/*control_reliable=*/true));
EXPECT_EQ(Action::StopSetpoints,
          policy.onTaskFailure(/*control_reliable=*/false));
EXPECT_FALSE(policy.forceDisarmAllowed());
```

Verify that the four square waypoints are anchored to the takeoff origin and
that the final point equals the initial hover point.

- [ ] **Step 2: Verify RED**

Expected: test fails because the policy and anchored-square generator are
missing.

- [ ] **Step 3: Implement the explicit state machine**

Implement the states listed in the approved design. Every loop checks:

- ROS shutdown request;
- current mode and armed state;
- RC Kill state;
- FCU/RC/odom/estimator freshness;
- position and velocity finite bounds;
- stage timeout.

After OFFBOARD entry, any mode transition away from OFFBOARD is treated as
manual takeover and never reversed by the mission node.

- [ ] **Step 4: Replace cumulative relative moves**

Generate `P1..P4` from the captured takeoff origin. Capture and command the
initial yaw for the whole task.

- [ ] **Step 5: Implement failure cleanup**

- Before arming: exit with no control action.
- Reliable control: hold, controlled descent, then `AUTO.LAND` fallback.
- Unreliable control: shut down the setpoint publisher and yield to PX4.
- Stable landing: ordinary `arm(false)` only.
- Delete `forceDisarm()` and all uses of command `400` parameter `21196`.

- [ ] **Step 6: Verify GREEN**

Expected: all pure policy, safety, trajectory, and failure tests pass.

- [ ] **Step 7: Commit**

```bash
git add workspace/src/mission_control
git commit -m "feat: harden autonomous square mission lifecycle"
```

### Task 5: Add the explicit host command, durable logs, and default-off integration

**Files:**
- Create: `scripts/mission`
- Modify: `docker-compose.yml`
- Modify: `workspace/src/mission_control/config/mission_control.yaml`
- Modify: `workspace/src/mission_control/launch/main_mission.launch`
- Modify: `workspace/src/mission_control/README.md`
- Modify: `MISSION_CONTROL_ANALYSIS.md`
- Create: `workspace/src/mission_control/test/test_default_off.py`

- [ ] **Step 1: Write the failing default-off test**

The test parses launch and compose configuration and asserts:

```python
assert "mission_main" not in base_stack_launch
assert "mission_control_node" not in base_stack_launch
assert "test_flight" not in compose_text
```

- [ ] **Step 2: Verify RED against the missing wrapper/integration**

Expected: the test fails because `scripts/mission` and durable log wiring are
not present.

- [ ] **Step 3: Implement `scripts/mission`**

The wrapper must:

- accept only `check` and
  `test_flight --confirm-uav-051`;
- use `flock` to prevent concurrent missions;
- source the installed Catkin overlay inside `flight-core`;
- reject unknown arguments;
- preserve the executable exit code;
- never start `test_flight` without the exact confirmation flag.

- [ ] **Step 4: Add durable logging**

Mount:

```yaml
- ./logs/mission-control:/opt/uav/logs/mission-control
```

Write mission CSV/event logs there. Do not add a mission process to Compose,
entrypoint, or `base_stack.launch`.

- [ ] **Step 5: Verify GREEN and shell syntax**

```bash
bash -n scripts/mission
python3 workspace/src/mission_control/test/test_default_off.py
docker compose -f docker-compose.yml -f docker-compose.build.yml config >/dev/null
```

Expected: all exit 0.

- [ ] **Step 6: Commit**

```bash
git add scripts/mission docker-compose.yml workspace/src/mission_control MISSION_CONTROL_ANALYSIS.md
git commit -m "feat: add explicit default-off mission command"
```

### Task 6: Build and test the ARM64 image without touching the running stack

**Files:**
- Build artifact only; no source mutation expected.

- [ ] **Step 1: Transfer the feature branch to an isolated Nano worktree**

Create a Git bundle from Windows, copy it to Nano, fetch it into the Nano
repository, and add:

```text
/home/tfboys_nano/TYI_E100_Firmware/.worktrees/mission-control-safe
```

- [ ] **Step 2: Run the complete package test suite**

Build with `CATKIN_ENABLE_TESTING=ON`, run package tests, and require zero
failures.

- [ ] **Step 3: Build a uniquely tagged flight-core image**

Use:

```bash
IMAGE_NAME=tyi/tyi_e100:0.1.2-mission-safe \
docker compose -f docker-compose.yml -f docker-compose.build.yml build flight-core
```

Expected: Docker build exit code 0 and the image contains
`mission_main` plus all installed config/launch assets.

- [ ] **Step 4: Prove the image remains default-off**

Start a temporary container without devices/host ROS control and verify no
mission process launches from the image entrypoint configuration. Do not call
`test_flight`.

### Task 7: Deploy default-off to the live Nano and run only the Kill-state check

**Files:**
- Update Nano feature branch and image selection only after all earlier gates pass.
- Append results: `PROJECT_DEVICE_CONFIG.md`
- Append results: `C:\nano\PROJECT_MEMORY.md`

- [ ] **Step 1: Capture the pre-deployment live baseline**

Record container health, branch/commit, radar ping, ROS topic rates, MAVROS
state, UID/SYSID, RC channels, estimator flags, and zero setpoint publishers.

- [ ] **Step 2: Replace only `flight-core` with the verified image**

Keep Kill engaged and aircraft disarmed. Restart `flight-core`, then wait for
all three services to be healthy.

- [ ] **Step 3: Re-run the manual-chain regression gate**

Require radar, FAST-LIO, high-rate odometry, Vision Pose, PX4 local odom,
MAVROS, RC and estimator state to match the pre-deployment baseline.

- [ ] **Step 4: Prove default-off**

Require:

- no `mission_main` or `mission_control_node`;
- no position/velocity/attitude/raw setpoint publishers;
- PX4 still `armed=false`, `POSCTL`;
- Kill remains engaged.

- [ ] **Step 5: Run only the read-only command**

```bash
./scripts/mission check
```

Expected: explicit per-check output and, if all gates pass,
`PRECHECK PASS — SAFE TO RELEASE KILL`. Do not release Kill and do not run
`test_flight`.

- [ ] **Step 6: Record evidence and rollback command**

Document the feature commit, image ID, test counts, check result, and exact
manual-baseline rollback sequence.

