#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace mission_control {

enum class GatePhase {
  KillEngaged,
  KillReleased,
};

struct SafetyConfig {
  std::uint64_t expected_uid{0};
  int expected_sysid{0};
  int expected_compid{0};
  std::string required_mode{"POSCTL"};
  std::size_t min_rc_channels{7};
  double min_battery_remaining{0.30};
  double min_lidar_hz{15.0};
  double min_imu_hz{150.0};
  double min_fastlio_hz{15.0};
  double min_vision_pose_hz{50.0};
  double min_local_odom_hz{10.0};
  double min_rc_hz{10.0};
  double parameter_tolerance{1e-3};
  std::map<std::string, double> expected_px4_params;

  static SafetyConfig uav051Defaults();
};

struct SafetySnapshot {
  std::uint64_t uid{0};
  int sysid{0};
  int compid{0};
  bool connected{false};
  bool armed{false};
  bool manual_input{false};
  std::string mode;
  bool on_ground{false};

  bool rc_fresh{false};
  std::size_t rc_channel_count{0};
  bool arm_switch_engaged{false};
  bool kill_switch_engaged{false};

  bool battery_fresh{false};
  double battery_remaining{-1.0};

  bool estimator_fresh{false};
  bool attitude_valid{false};
  bool velocity_horiz_valid{false};
  bool velocity_vert_valid{false};
  bool position_horiz_rel_valid{false};
  bool position_vert_abs_valid{false};
  bool gps_glitch{false};
  bool accel_error{false};

  bool clock_valid{false};
  double lidar_hz{0.0};
  double imu_hz{0.0};
  double fastlio_hz{0.0};
  double vision_pose_hz{0.0};
  double local_odom_hz{0.0};
  double rc_hz{0.0};

  bool odom_fresh{false};
  bool odom_stable{false};
  int competing_setpoint_publishers{0};
  std::map<std::string, double> px4_params;
};

struct GateResult {
  bool passed{false};
  std::vector<std::string> failures;
};

class SafetyGate {
public:
  explicit SafetyGate(const SafetyConfig& config);

  GateResult evaluate(const SafetySnapshot& snapshot, GatePhase phase) const;

private:
  SafetyConfig config_;
};

}  // namespace mission_control
