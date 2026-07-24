#include <functional>
#include <string>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

#include "mission_control/safety_gate.h"

namespace mission_control {
namespace {

SafetySnapshot makeValidSnapshot(bool kill_engaged) {
  SafetySnapshot snapshot;
  snapshot.uid = 3761439192332449336ULL;
  snapshot.sysid = 51;
  snapshot.compid = 1;
  snapshot.connected = true;
  snapshot.armed = false;
  snapshot.manual_input = true;
  snapshot.mode = "POSCTL";
  snapshot.on_ground = true;
  snapshot.rc_fresh = true;
  snapshot.rc_channel_count = 18;
  snapshot.arm_switch_engaged = false;
  snapshot.kill_switch_engaged = kill_engaged;
  snapshot.battery_fresh = true;
  snapshot.battery_remaining = 0.80;
  snapshot.estimator_fresh = true;
  snapshot.attitude_valid = true;
  snapshot.velocity_horiz_valid = true;
  snapshot.velocity_vert_valid = true;
  snapshot.position_horiz_rel_valid = true;
  snapshot.position_vert_abs_valid = true;
  snapshot.gps_glitch = false;
  snapshot.accel_error = false;
  snapshot.clock_valid = true;
  snapshot.lidar_hz = 20.0;
  snapshot.imu_hz = 200.0;
  snapshot.fastlio_hz = 20.0;
  snapshot.vision_pose_hz = 200.0;
  snapshot.local_odom_hz = 20.0;
  snapshot.rc_hz = 20.0;
  snapshot.odom_fresh = true;
  snapshot.odom_stable = true;
  snapshot.competing_setpoint_publishers = 0;
  snapshot.px4_params = SafetyConfig::uav051Defaults().expected_px4_params;
  return snapshot;
}

TEST(SafetyGateTest, AcceptsVerifiedUav051WhileKillIsEngaged) {
  const SafetyConfig config = SafetyConfig::uav051Defaults();
  const GateResult result =
      SafetyGate(config).evaluate(makeValidSnapshot(true), GatePhase::KillEngaged);

  EXPECT_TRUE(result.passed);
  EXPECT_TRUE(result.failures.empty());
}

TEST(SafetyGateTest, AcceptsVerifiedUav051AfterKillRelease) {
  const SafetyConfig config = SafetyConfig::uav051Defaults();
  const GateResult result =
      SafetyGate(config).evaluate(makeValidSnapshot(false), GatePhase::KillReleased);

  EXPECT_TRUE(result.passed);
  EXPECT_TRUE(result.failures.empty());
}

TEST(SafetyGateTest, KeepsRcFailsafeEnabledWithOneSecondDropoutTolerance) {
  const SafetyConfig config = SafetyConfig::uav051Defaults();

  EXPECT_DOUBLE_EQ(1.0, config.expected_px4_params.at("COM_RC_LOSS_T"));
  EXPECT_DOUBLE_EQ(3.0, config.expected_px4_params.at("NAV_RCL_ACT"));
  EXPECT_DOUBLE_EQ(0.0, config.expected_px4_params.at("COM_RCL_EXCEPT"));
}

TEST(SafetyGateTest, ReportsEveryIndependentFailure) {
  const SafetyConfig config = SafetyConfig::uav051Defaults();
  SafetySnapshot snapshot = makeValidSnapshot(true);
  snapshot.uid = 1;
  snapshot.sysid = 1;
  snapshot.compid = 2;
  snapshot.connected = false;
  snapshot.armed = true;
  snapshot.manual_input = false;
  snapshot.mode = "OFFBOARD";
  snapshot.on_ground = false;
  snapshot.rc_fresh = false;
  snapshot.rc_channel_count = 4;
  snapshot.arm_switch_engaged = true;
  snapshot.kill_switch_engaged = false;
  snapshot.battery_fresh = false;
  snapshot.battery_remaining = 0.10;
  snapshot.estimator_fresh = false;
  snapshot.attitude_valid = false;
  snapshot.velocity_horiz_valid = false;
  snapshot.velocity_vert_valid = false;
  snapshot.position_horiz_rel_valid = false;
  snapshot.position_vert_abs_valid = false;
  snapshot.gps_glitch = true;
  snapshot.accel_error = true;
  snapshot.clock_valid = false;
  snapshot.lidar_hz = 1.0;
  snapshot.imu_hz = 1.0;
  snapshot.fastlio_hz = 1.0;
  snapshot.vision_pose_hz = 1.0;
  snapshot.local_odom_hz = 1.0;
  snapshot.rc_hz = 1.0;
  snapshot.odom_fresh = false;
  snapshot.odom_stable = false;
  snapshot.competing_setpoint_publishers = 1;
  snapshot.px4_params["COM_OF_LOSS_T"] = 99.0;

  const GateResult result =
      SafetyGate(config).evaluate(snapshot, GatePhase::KillEngaged);

  EXPECT_FALSE(result.passed);
  EXPECT_GE(result.failures.size(), 25U);
}

TEST(SafetyGateTest, RejectsEachCriticalCondition) {
  using Mutation = std::pair<std::string, std::function<void(SafetySnapshot&)>>;
  const std::vector<Mutation> cases{
      {"wrong_uid", [](SafetySnapshot& s) { s.uid = 7; }},
      {"wrong_sysid", [](SafetySnapshot& s) { s.sysid = 1; }},
      {"armed", [](SafetySnapshot& s) { s.armed = true; }},
      {"wrong_mode", [](SafetySnapshot& s) { s.mode = "ALTCTL"; }},
      {"stale_rc", [](SafetySnapshot& s) { s.rc_fresh = false; }},
      {"arm_switch", [](SafetySnapshot& s) { s.arm_switch_engaged = true; }},
      {"low_battery", [](SafetySnapshot& s) { s.battery_remaining = 0.1; }},
      {"invalid_estimator", [](SafetySnapshot& s) { s.attitude_valid = false; }},
      {"stale_odom", [](SafetySnapshot& s) { s.odom_fresh = false; }},
      {"unstable_odom", [](SafetySnapshot& s) { s.odom_stable = false; }},
      {"competing_publisher",
       [](SafetySnapshot& s) { s.competing_setpoint_publishers = 1; }},
      {"failsafe_mismatch",
       [](SafetySnapshot& s) { s.px4_params["NAV_RCL_ACT"] = 2.0; }},
  };

  const SafetyConfig config = SafetyConfig::uav051Defaults();
  for (const Mutation& test_case : cases) {
    SafetySnapshot snapshot = makeValidSnapshot(true);
    test_case.second(snapshot);
    const GateResult result =
        SafetyGate(config).evaluate(snapshot, GatePhase::KillEngaged);
    EXPECT_FALSE(result.passed) << test_case.first;
    EXPECT_FALSE(result.failures.empty()) << test_case.first;
  }
}

TEST(SafetyGateTest, RequiresTheExpectedKillStateForEachPhase) {
  const SafetyConfig config = SafetyConfig::uav051Defaults();

  EXPECT_FALSE(
      SafetyGate(config)
          .evaluate(makeValidSnapshot(false), GatePhase::KillEngaged)
          .passed);
  EXPECT_FALSE(
      SafetyGate(config)
          .evaluate(makeValidSnapshot(true), GatePhase::KillReleased)
          .passed);
}

}  // namespace
}  // namespace mission_control

int main(int argc, char** argv) {
  testing::InitGoogleTest(&argc, argv);
  return RUN_ALL_TESTS();
}
