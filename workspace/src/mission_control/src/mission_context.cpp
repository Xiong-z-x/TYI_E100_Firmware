#include "mission_control/mission_context.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <ctime>
#include <dirent.h>
#include <iomanip>
#include <sys/stat.h>
#include <vector>

#include "mission_control/trajectory_generator.h"

namespace mission_control {

MissionContext::MissionContext(FlightInterface& flight) : flight_(flight) {
  loadParameters();
}

void MissionContext::loadParameters() {
  ros::NodeHandle nh("~");
  nh.param("wait_ready_timeout_sec", wait_ready_timeout_sec_,
           wait_ready_timeout_sec_);
  nh.param("mission_log_dir", mission_log_dir_, mission_log_dir_);
  nh.param("takeoff/climb_rate_mps", takeoff_climb_rate_mps_,
           takeoff_climb_rate_mps_);
  nh.param("takeoff/xy_tolerance_m", takeoff_xy_tolerance_m_,
           takeoff_xy_tolerance_m_);
  nh.param("takeoff/z_tolerance_m", takeoff_z_tolerance_m_,
           takeoff_z_tolerance_m_);
  nh.param("takeoff/speed_tolerance_mps", takeoff_speed_tolerance_mps_,
           takeoff_speed_tolerance_mps_);
  nh.param("takeoff/stable_sec", takeoff_stable_sec_,
           takeoff_stable_sec_);
  nh.param("takeoff/timeout_sec", takeoff_timeout_sec_,
           takeoff_timeout_sec_);
  nh.param("landing/descent_rate_mps", landing_descent_rate_mps_,
           landing_descent_rate_mps_);
  nh.param("landing/floor_height_m", landing_floor_height_m_,
           landing_floor_height_m_);
  nh.param("landing/z_tolerance_m", landing_z_tolerance_m_,
           landing_z_tolerance_m_);
  nh.param("landing/vertical_speed_tolerance_mps",
           landing_vertical_speed_tolerance_mps_,
           landing_vertical_speed_tolerance_mps_);
  nh.param("landing/stable_sec", landing_stable_sec_,
           landing_stable_sec_);
  nh.param("landing/timeout_sec", landing_timeout_sec_,
           landing_timeout_sec_);
  nh.param("landing/disarm_timeout_sec", disarm_timeout_sec_,
           disarm_timeout_sec_);
  nh.param("landing/auto_land_wait_sec", auto_land_wait_sec_,
           auto_land_wait_sec_);
}

bool MissionContext::waitReady() {
  return flight_.waitReady(ros::Duration(wait_ready_timeout_sec_));
}

bool MissionContext::boot() {
  openMissionLog();
  if (!flight_.boot()) {
    return failTask("OFFBOARD/arm transition failed");
  }

  takeoff_origin_ = flight_.currentPosition();
  last_target_ = takeoff_origin_;
  initial_yaw_ = flight_.currentYaw();
  has_takeoff_origin_ = true;
  has_last_target_ = true;
  mission_active_ = true;
  failure_action_ = FailureAction::Continue;
  ROS_INFO("mission origin=(%.3f, %.3f, %.3f) yaw=%.3f",
           takeoff_origin_.x, takeoff_origin_.y, takeoff_origin_.z,
           initial_yaw_);
  return true;
}

bool MissionContext::takeoff(double height, double duration_sec) {
  if (!has_takeoff_origin_ || !std::isfinite(height) || height <= 0.0) {
    return failTask("invalid takeoff origin or height");
  }
  geometry_msgs::Point target = takeoff_origin_;
  target.z += height;
  ROS_INFO("mission takeoff target=(%.2f, %.2f, %.2f)", target.x,
           target.y, target.z);
  if (!rampTo(target,
              std::max(duration_sec, height / takeoff_climb_rate_mps_))) {
    return false;
  }
  last_target_ = target;
  has_last_target_ = true;
  if (!waitUntilNear(target, takeoff_xy_tolerance_m_,
                     takeoff_z_tolerance_m_,
                     takeoff_speed_tolerance_mps_, takeoff_stable_sec_,
                     takeoff_timeout_sec_)) {
    if (failure_action_ != FailureAction::Continue) {
      return false;
    }
    return failTask("takeoff target tolerance timed out");
  }
  return true;
}

bool MissionContext::moveTo(double x, double y, double z,
                            double duration_sec, bool relative) {
  geometry_msgs::Point target;
  if (relative) {
    target = has_last_target_ ? last_target_ : flight_.currentPosition();
    target.x += x;
    target.y += y;
    target.z += z;
  } else {
    target.x = x;
    target.y = y;
    target.z = z;
  }
  return moveToPoint(target, duration_sec);
}

bool MissionContext::moveToPoint(const geometry_msgs::Point& target,
                                 double duration_sec) {
  ROS_INFO("mission move target=(%.2f, %.2f, %.2f) duration=%.2f",
           target.x, target.y, target.z, duration_sec);
  if (!rampTo(target, duration_sec)) {
    return false;
  }
  last_target_ = target;
  has_last_target_ = true;
  return true;
}

bool MissionContext::hover(double duration_sec) {
  const geometry_msgs::Point target =
      has_last_target_ ? last_target_ : flight_.currentPosition();
  const ros::WallTime deadline =
      ros::WallTime::now() + ros::WallDuration(std::max(0.0, duration_sec));
  ros::WallRate rate(flight_.rateHz());
  while (ros::ok() && ros::WallTime::now() < deadline) {
    if (!checkContinuation("hover")) {
      return false;
    }
    flight_.publishPositionTarget(target, initial_yaw_, true);
    logSample("hover", target);
    rate.sleep();
  }
  return ros::ok();
}

bool MissionContext::land(double duration_sec, double floor_height,
                          bool disarm) {
  geometry_msgs::Point target =
      has_takeoff_origin_ ? takeoff_origin_ : flight_.currentPosition();
  const double ground_z =
      has_takeoff_origin_ ? takeoff_origin_.z : target.z;
  const double floor_offset =
      std::isfinite(floor_height) ? floor_height : landing_floor_height_m_;
  target.z = ground_z + floor_offset;

  ROS_INFO("mission controlled landing target=(%.2f, %.2f, %.2f)",
           target.x, target.y, target.z);
  const double descent =
      std::max(0.0, flight_.currentPosition().z - target.z);
  if (!rampTo(target,
              std::max(duration_sec,
                       descent / landing_descent_rate_mps_))) {
    return false;
  }
  last_target_ = target;
  has_last_target_ = true;
  if (!waitGroundContactWhileHolding(
          target, ground_z, landing_z_tolerance_m_,
          landing_vertical_speed_tolerance_mps_, landing_stable_sec_,
          landing_timeout_sec_)) {
    if (failure_action_ == FailureAction::Continue) {
      return failTask("ground-contact criteria timed out");
    }
    return false;
  }
  if (disarm && !disarmWhileHolding(target, disarm_timeout_sec_)) {
    if (failure_action_ == FailureAction::Continue) {
      return failTask("normal disarm timed out");
    }
    return false;
  }
  mission_active_ = false;
  return true;
}

bool MissionContext::recoverFromFailure() {
  ros::spinOnce();
  if (!flight_.armed()) {
    flight_.disableSetpointPublisher();
    return true;
  }
  if (failure_action_ == FailureAction::YieldToPilot ||
      failure_action_ == FailureAction::StopSetpoints) {
    flight_.disableSetpointPublisher();
    return false;
  }

  if (!flight_.controlReliable() || !flight_.inOffboard() ||
      flight_.killEngaged()) {
    flight_.disableSetpointPublisher();
    return false;
  }

  recovering_ = true;
  failure_action_ = FailureAction::ControlledLand;
  ROS_WARN("ordinary mission failure: attempting controlled landing");
  if (land(3.0, landing_floor_height_m_, true)) {
    flight_.disableSetpointPublisher();
    recovering_ = false;
    return true;
  }
  recovering_ = false;

  if (failure_action_ == FailureAction::YieldToPilot ||
      failure_action_ == FailureAction::StopSetpoints ||
      !flight_.controlReliable() || flight_.killEngaged()) {
    flight_.disableSetpointPublisher();
    return false;
  }

  ROS_WARN("controlled landing incomplete; requesting PX4 AUTO.LAND");
  if (!flight_.requestMode("AUTO.LAND")) {
    flight_.disableSetpointPublisher();
    return false;
  }
  flight_.disableSetpointPublisher();
  if (!flight_.waitLanded(ros::Duration(auto_land_wait_sec_),
                          ros::Duration(landing_stable_sec_))) {
    ROS_ERROR("PX4 AUTO.LAND did not confirm landed state");
    return false;
  }
  return flight_.disarmUntilLocked(ros::Duration(disarm_timeout_sec_));
}

void MissionContext::openMissionLog() {
  mkdir(mission_log_dir_.c_str(), 0755);
  cleanupMissionLogs(mission_log_dir_, 5);

  char timestamp[32];
  const std::time_t now = std::time(nullptr);
  std::strftime(timestamp, sizeof(timestamp), "%Y%m%d_%H%M%S",
                std::localtime(&now));
  const std::string path =
      mission_log_dir_ + "/mission_" + timestamp + ".csv";
  mission_log_.open(path.c_str(), std::ios::out | std::ios::trunc);
  if (mission_log_.is_open()) {
    mission_log_
        << "wall_time,phase,pos_x,pos_y,pos_z,vel_x,vel_y,vel_z,"
           "target_x,target_y,target_z,mode,armed,kill\n";
    ROS_INFO("mission log: %s", path.c_str());
  } else {
    ROS_WARN("mission log could not be opened: %s", path.c_str());
  }
}

void MissionContext::cleanupMissionLogs(const std::string& log_dir,
                                        int keep_count) {
  DIR* dir = opendir(log_dir.c_str());
  if (dir == nullptr) {
    return;
  }
  std::vector<std::string> paths;
  while (dirent* entry = readdir(dir)) {
    const std::string name(entry->d_name);
    if (name.find("mission_") == 0 && name.size() >= 4 &&
        name.rfind(".csv") == name.size() - 4) {
      paths.push_back(log_dir + "/" + name);
    }
  }
  closedir(dir);
  std::sort(paths.begin(), paths.end());
  while (static_cast<int>(paths.size()) > keep_count) {
    std::remove(paths.front().c_str());
    paths.erase(paths.begin());
  }
}

void MissionContext::logSample(const std::string& phase,
                               const geometry_msgs::Point& target) {
  if (!mission_log_.is_open()) {
    return;
  }
  const geometry_msgs::Point current = flight_.currentPosition();
  const geometry_msgs::Vector3 velocity = flight_.currentVelocity();
  mission_log_ << std::fixed << std::setprecision(6)
               << ros::WallTime::now().toSec() << ',' << phase << ','
               << current.x << ',' << current.y << ',' << current.z << ','
               << velocity.x << ',' << velocity.y << ',' << velocity.z
               << ',' << target.x << ',' << target.y << ',' << target.z
               << ',' << flight_.mode() << ',' << flight_.armed() << ','
               << flight_.killEngaged() << '\n';
  mission_log_.flush();
}

bool MissionContext::checkContinuation(const std::string& phase) {
  ros::spinOnce();
  if (flight_.killEngaged()) {
    failure_action_ = FailureAction::YieldToPilot;
    ROS_ERROR("mission stopped during %s: RC Kill engaged", phase.c_str());
    flight_.disableSetpointPublisher();
    return false;
  }
  if (mission_active_ && !flight_.inOffboard()) {
    failure_action_ =
        policy_.onModeChanged("OFFBOARD", flight_.mode());
    ROS_WARN("mission yielded during %s: mode changed to %s",
             phase.c_str(), flight_.mode().c_str());
    flight_.disableSetpointPublisher();
    return false;
  }
  if (!flight_.controlReliable()) {
    failure_action_ = FailureAction::StopSetpoints;
    ROS_ERROR("mission stopped during %s: FCU/position data unreliable",
              phase.c_str());
    flight_.disableSetpointPublisher();
    return false;
  }
  return true;
}

bool MissionContext::failTask(const std::string& reason) {
  failure_action_ = policy_.onTaskFailure(
      flight_.controlReliable() && flight_.inOffboard() &&
      !flight_.killEngaged());
  ROS_ERROR("mission task failure: %s; action=%d", reason.c_str(),
            static_cast<int>(failure_action_));
  if (failure_action_ == FailureAction::StopSetpoints) {
    flight_.disableSetpointPublisher();
  }
  return false;
}

bool MissionContext::waitUntilNear(
    const geometry_msgs::Point& target, double xy_tolerance,
    double z_tolerance, double speed_tolerance, double stable_sec,
    double timeout_sec) {
  const ros::WallTime deadline =
      ros::WallTime::now() + ros::WallDuration(timeout_sec);
  ros::WallTime stable_since;
  bool stable_started = false;
  ros::WallRate rate(flight_.rateHz());

  while (ros::ok() && ros::WallTime::now() < deadline) {
    if (!checkContinuation("target wait")) {
      return false;
    }
    const geometry_msgs::Point current = flight_.currentPosition();
    const geometry_msgs::Vector3 velocity = flight_.currentVelocity();
    const double dx = current.x - target.x;
    const double dy = current.y - target.y;
    const double dz = current.z - target.z;
    const double speed =
        std::sqrt(velocity.x * velocity.x + velocity.y * velocity.y +
                  velocity.z * velocity.z);
    const bool near = std::hypot(dx, dy) <= xy_tolerance &&
                      std::abs(dz) <= z_tolerance &&
                      speed <= speed_tolerance;
    flight_.publishPositionTarget(target, initial_yaw_, true);
    logSample("wait_target", target);

    if (near) {
      if (!stable_started) {
        stable_started = true;
        stable_since = ros::WallTime::now();
      }
      if ((ros::WallTime::now() - stable_since).toSec() >= stable_sec) {
        return true;
      }
    } else {
      stable_started = false;
    }
    rate.sleep();
  }
  return false;
}

bool MissionContext::waitGroundContactWhileHolding(
    const geometry_msgs::Point& target, double ground_z,
    double z_tolerance, double vertical_speed_tolerance,
    double stable_sec, double timeout_sec) {
  const ros::WallTime deadline =
      ros::WallTime::now() + ros::WallDuration(timeout_sec);
  ros::WallTime stable_since;
  bool stable_started = false;
  ros::WallRate rate(flight_.rateHz());

  while (ros::ok() && ros::WallTime::now() < deadline) {
    if (!checkContinuation("landing")) {
      return false;
    }
    const geometry_msgs::Point current = flight_.currentPosition();
    const geometry_msgs::Vector3 velocity = flight_.currentVelocity();
    const bool near_ground =
        current.z <= ground_z + z_tolerance &&
        std::abs(velocity.z) <= vertical_speed_tolerance;
    flight_.publishPositionTarget(target, initial_yaw_, true);
    logSample("ground_contact", target);

    if (near_ground || flight_.landed()) {
      if (!stable_started) {
        stable_started = true;
        stable_since = ros::WallTime::now();
      }
      if ((ros::WallTime::now() - stable_since).toSec() >= stable_sec) {
        return true;
      }
    } else {
      stable_started = false;
    }
    rate.sleep();
  }
  return false;
}

bool MissionContext::disarmWhileHolding(
    const geometry_msgs::Point& target, double timeout_sec) {
  const ros::WallTime deadline =
      ros::WallTime::now() + ros::WallDuration(timeout_sec);
  ros::WallTime last_disarm_request;
  ros::WallRate rate(flight_.rateHz());

  while (ros::ok() && ros::WallTime::now() < deadline) {
    ros::spinOnce();
    if (!flight_.armed()) {
      ROS_INFO("vehicle confirmed disarmed");
      return true;
    }
    if (!checkContinuation("normal disarm")) {
      return false;
    }
    flight_.publishPositionTarget(target, initial_yaw_, true);
    logSample("disarm", target);
    if (last_disarm_request.isZero() ||
        (ros::WallTime::now() - last_disarm_request).toSec() >= 0.5) {
      flight_.arm(false);
      last_disarm_request = ros::WallTime::now();
    }
    rate.sleep();
  }
  return false;
}

void MissionContext::lock() {
  flight_.lock();
}

std::string MissionContext::statusText() const {
  return flight_.statusText();
}

bool MissionContext::rampTo(const geometry_msgs::Point& target,
                            double duration_sec) {
  const geometry_msgs::Point start = flight_.currentPosition();
  const ros::WallTime start_time = ros::WallTime::now();
  const double duration = std::max(0.1, duration_sec);
  ros::WallRate rate(flight_.rateHz());
  while (ros::ok()) {
    if (!checkContinuation("trajectory")) {
      return false;
    }
    const double elapsed =
        (ros::WallTime::now() - start_time).toSec();
    const double ratio = std::min(1.0, elapsed / duration);
    const geometry_msgs::Point point =
        TrajectoryGenerator::interpolate(start, target, ratio);
    flight_.publishPositionTarget(point, initial_yaw_, true);
    logSample("ramp", point);
    if (ratio >= 1.0) {
      return true;
    }
    rate.sleep();
  }
  return false;
}

}  // namespace mission_control
