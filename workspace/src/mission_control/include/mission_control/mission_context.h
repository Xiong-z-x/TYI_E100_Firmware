#pragma once

#include <fstream>
#include <string>

#include <geometry_msgs/Point.h>
#include <ros/ros.h>

#include "mission_control/flight_interface.h"
#include "mission_control/mission_policy.h"

namespace mission_control {

class MissionContext {
public:
  explicit MissionContext(FlightInterface& flight);

  bool waitReady();
  bool boot();
  bool takeoff(double height, double duration_sec);
  bool moveTo(double x, double y, double z, double duration_sec,
              bool relative = false);
  bool moveToPoint(const geometry_msgs::Point& target, double duration_sec);
  bool hover(double duration_sec);
  bool land(bool disarm = true);
  bool recoverFromFailure();
  void lock();
  void setMissionYaw(double yaw);

  geometry_msgs::Point takeoffOrigin() const { return takeoff_origin_; }
  double initialYaw() const { return initial_yaw_; }
  FailureAction failureAction() const { return failure_action_; }
  std::string statusText() const;

private:
  void loadParameters();
  void openMissionLog();
  void cleanupMissionLogs(const std::string& log_dir, int keep_count);
  void logSample(const std::string& phase,
                 const geometry_msgs::Point& target);
  bool checkContinuation(const std::string& phase);
  bool failTask(const std::string& reason);
  bool waitUntilNear(const geometry_msgs::Point& target,
                     double xy_tolerance, double z_tolerance,
                     double speed_tolerance, double stable_sec,
                     double timeout_sec);
  bool rampTo(const geometry_msgs::Point& target, double duration_sec);

  FlightInterface& flight_;
  MissionPolicy policy_;
  geometry_msgs::Point takeoff_origin_;
  geometry_msgs::Point last_target_;
  bool has_takeoff_origin_{false};
  bool has_last_target_{false};
  bool mission_active_{false};
  bool recovering_{false};
  double initial_yaw_{0.0};
  double commanded_yaw_{0.0};
  FailureAction failure_action_{FailureAction::Continue};
  std::ofstream mission_log_;
  std::string mission_log_dir_{"/opt/uav/logs/mission-control"};

  double wait_ready_timeout_sec_{12.0};
  double takeoff_climb_rate_mps_{0.30};
  double takeoff_xy_tolerance_m_{0.20};
  double takeoff_z_tolerance_m_{0.12};
  double takeoff_speed_tolerance_mps_{0.25};
  double takeoff_stable_sec_{1.2};
  double takeoff_timeout_sec_{12.0};
  double navigation_xy_tolerance_m_{0.25};
  double navigation_z_tolerance_m_{0.15};
  double navigation_speed_tolerance_mps_{0.35};
  double navigation_stable_sec_{0.5};
  double navigation_timeout_sec_{5.0};
  double landing_stable_sec_{1.0};
  double disarm_timeout_sec_{12.0};
  double auto_land_wait_sec_{30.0};
};

}  // namespace mission_control
