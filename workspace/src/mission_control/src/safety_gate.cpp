#include "mission_control/safety_gate.h"

#include <cmath>
#include <sstream>

namespace mission_control {
namespace {

void require(bool condition, const std::string& message,
             std::vector<std::string>* failures) {
  if (!condition) {
    failures->push_back(message);
  }
}

}  // namespace

SafetyConfig SafetyConfig::uav051Defaults() {
  SafetyConfig config;
  config.expected_uid = 3761439192332449336ULL;
  config.expected_sysid = 51;
  config.expected_compid = 1;
  config.expected_px4_params = {
      {"MAV_SYS_ID", 51.0},
      {"COM_OF_LOSS_T", 1.0},
      {"COM_OBL_RC_ACT", 0.0},
      {"COM_RC_LOSS_T", 0.5},
      {"NAV_RCL_ACT", 3.0},
      {"COM_RCL_EXCEPT", 0.0},
      {"COM_RC_OVERRIDE", 3.0},
      {"COM_RC_STICK_OV", 30.0},
      {"RC_MAP_ARM_SW", 6.0},
      {"RC_MAP_KILL_SW", 7.0},
      {"RC_MAP_FLTMODE", 5.0},
      {"EKF2_EV_CTRL", 15.0},
      {"EKF2_GPS_CTRL", 0.0},
  };
  return config;
}

SafetyGate::SafetyGate(const SafetyConfig& config) : config_(config) {}

GateResult SafetyGate::evaluate(const SafetySnapshot& snapshot,
                                GatePhase phase) const {
  GateResult result;
  std::vector<std::string>& failures = result.failures;

  require(snapshot.uid == config_.expected_uid, "flight-controller UID mismatch",
          &failures);
  require(snapshot.sysid == config_.expected_sysid, "MAVLink SYSID mismatch",
          &failures);
  require(snapshot.compid == config_.expected_compid, "MAVLink COMPID mismatch",
          &failures);
  require(snapshot.connected, "MAVROS is not connected", &failures);
  require(!snapshot.armed, "vehicle is already armed", &failures);
  require(snapshot.manual_input, "manual RC input is unavailable", &failures);
  require(snapshot.mode == config_.required_mode, "vehicle is not in POSCTL",
          &failures);
  require(snapshot.on_ground, "vehicle is not confirmed on ground", &failures);

  require(snapshot.rc_fresh, "RC input is stale", &failures);
  require(snapshot.rc_channel_count >= config_.min_rc_channels,
          "RC channel count is insufficient", &failures);
  require(!snapshot.arm_switch_engaged, "RC arm switch must remain low",
          &failures);
  if (phase == GatePhase::KillEngaged) {
    require(snapshot.kill_switch_engaged,
            "RC Kill must be engaged during first-stage check", &failures);
  } else {
    require(!snapshot.kill_switch_engaged,
            "RC Kill must be released before test_flight", &failures);
  }

  require(snapshot.battery_fresh, "battery state is stale", &failures);
  require(std::isfinite(snapshot.battery_remaining) &&
              snapshot.battery_remaining >= config_.min_battery_remaining,
          "battery remaining is below mission threshold", &failures);

  require(snapshot.estimator_fresh, "estimator status is stale", &failures);
  require(snapshot.attitude_valid, "estimator attitude is invalid", &failures);
  require(snapshot.velocity_horiz_valid,
          "estimator horizontal velocity is invalid", &failures);
  require(snapshot.velocity_vert_valid,
          "estimator vertical velocity is invalid", &failures);
  require(snapshot.position_horiz_rel_valid,
          "estimator relative horizontal position is invalid", &failures);
  require(snapshot.position_vert_abs_valid,
          "estimator absolute vertical position is invalid", &failures);
  require(!snapshot.gps_glitch, "estimator reports GPS glitch", &failures);
  require(!snapshot.accel_error, "estimator reports accelerometer error",
          &failures);

  require(snapshot.clock_valid, "ROS clock is not monotonic and advancing",
          &failures);
  require(snapshot.lidar_hz >= config_.min_lidar_hz,
          "LiDAR topic rate is too low", &failures);
  require(snapshot.imu_hz >= config_.min_imu_hz,
          "Livox IMU topic rate is too low", &failures);
  require(snapshot.fastlio_hz >= config_.min_fastlio_hz,
          "FAST-LIO odometry rate is too low", &failures);
  require(snapshot.vision_pose_hz >= config_.min_vision_pose_hz,
          "MAVROS vision pose rate is too low", &failures);
  require(snapshot.local_odom_hz >= config_.min_local_odom_hz,
          "PX4 local odometry rate is too low", &failures);
  require(snapshot.rc_hz >= config_.min_rc_hz, "RC topic rate is too low",
          &failures);
  require(snapshot.odom_fresh, "local odometry is stale", &failures);
  require(snapshot.odom_stable, "local odometry is not stable", &failures);
  require(snapshot.competing_setpoint_publishers == 0,
          "another node already publishes mission setpoints", &failures);

  for (const auto& expected : config_.expected_px4_params) {
    const auto actual = snapshot.px4_params.find(expected.first);
    if (actual == snapshot.px4_params.end()) {
      failures.push_back("PX4 parameter missing: " + expected.first);
      continue;
    }
    if (!std::isfinite(actual->second) ||
        std::abs(actual->second - expected.second) >
            config_.parameter_tolerance) {
      std::ostringstream message;
      message << "PX4 parameter mismatch: " << expected.first
              << " expected=" << expected.second
              << " actual=" << actual->second;
      failures.push_back(message.str());
    }
  }

  result.passed = failures.empty();
  return result;
}

}  // namespace mission_control
