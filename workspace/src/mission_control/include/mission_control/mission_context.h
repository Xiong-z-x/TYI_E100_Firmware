#pragma once

#include <geometry_msgs/Point.h>
#include <ros/ros.h>

#include <fstream>
#include <string>

#include "mission_control/flight_interface.h"

namespace mission_control {

class MissionContext {
public:
  explicit MissionContext(FlightInterface& flight);

  bool waitReady();
  bool boot();
  bool takeoff(double height, double duration_sec);
  void moveTo(double x, double y, double z, double duration_sec, bool relative = false, double yaw = 0.0, bool use_yaw = false);
  void hover(double duration_sec);
  bool land(double duration_sec, double floor_height, bool disarm = true);
  void lock();
  std::string statusText() const;

private:
  void loadParameters();
  void openMissionLog();
  void cleanupMissionLogs(const std::string& log_dir, int keep_count);
  void logSample(const std::string& phase, const geometry_msgs::Point& target);
  bool waitUntilNear(const geometry_msgs::Point& target, double xy_tolerance, double z_tolerance, double speed_tolerance, double stable_sec, double timeout_sec);
  bool waitGroundContactWhileHolding(const geometry_msgs::Point& target, double ground_z, double z_tolerance, double vertical_speed_tolerance, double stable_sec, double timeout_sec);
  bool disarmWhileHolding(const geometry_msgs::Point& target, double ground_z, double timeout_sec);
  void rampTo(const geometry_msgs::Point& target, double duration_sec, double yaw = 0.0, bool use_yaw = false);

  FlightInterface& flight_;
  geometry_msgs::Point takeoff_origin_;
  geometry_msgs::Point last_target_;
  bool has_takeoff_origin_{false};
  bool has_last_target_{false};
  std::ofstream mission_log_;

  double wait_ready_timeout_sec_{12.0};
  double takeoff_climb_rate_mps_{0.30};
  double takeoff_xy_tolerance_m_{0.20};
  double takeoff_z_tolerance_m_{0.12};
  double takeoff_speed_tolerance_mps_{0.25};
  double takeoff_stable_sec_{1.2};
  double takeoff_timeout_sec_{12.0};
  double landing_descent_rate_mps_{0.25};
  double landing_floor_height_m_{-0.03};
  double landing_z_tolerance_m_{0.06};
  double landing_vertical_speed_tolerance_mps_{0.10};
  double landing_stable_sec_{1.0};
  double landing_timeout_sec_{10.0};
  double disarm_timeout_sec_{12.0};
  double force_disarm_delay_sec_{1.5};
};

}  // namespace mission_control
