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
  nh.param("wait_ready_timeout_sec", wait_ready_timeout_sec_, wait_ready_timeout_sec_);
  nh.param("takeoff/climb_rate_mps", takeoff_climb_rate_mps_, takeoff_climb_rate_mps_);
  nh.param("takeoff/xy_tolerance_m", takeoff_xy_tolerance_m_, takeoff_xy_tolerance_m_);
  nh.param("takeoff/z_tolerance_m", takeoff_z_tolerance_m_, takeoff_z_tolerance_m_);
  nh.param("takeoff/speed_tolerance_mps", takeoff_speed_tolerance_mps_, takeoff_speed_tolerance_mps_);
  nh.param("takeoff/stable_sec", takeoff_stable_sec_, takeoff_stable_sec_);
  nh.param("takeoff/timeout_sec", takeoff_timeout_sec_, takeoff_timeout_sec_);
  nh.param("landing/descent_rate_mps", landing_descent_rate_mps_, landing_descent_rate_mps_);
  nh.param("landing/floor_height_m", landing_floor_height_m_, landing_floor_height_m_);
  nh.param("landing/z_tolerance_m", landing_z_tolerance_m_, landing_z_tolerance_m_);
  nh.param("landing/vertical_speed_tolerance_mps", landing_vertical_speed_tolerance_mps_, landing_vertical_speed_tolerance_mps_);
  nh.param("landing/stable_sec", landing_stable_sec_, landing_stable_sec_);
  nh.param("landing/timeout_sec", landing_timeout_sec_, landing_timeout_sec_);
  nh.param("landing/disarm_timeout_sec", disarm_timeout_sec_, disarm_timeout_sec_);
  nh.param("landing/force_disarm_delay_sec", force_disarm_delay_sec_, force_disarm_delay_sec_);
}

bool MissionContext::waitReady() {
  return flight_.waitReady(ros::Duration(wait_ready_timeout_sec_));
}

bool MissionContext::boot() {
  openMissionLog();
  const bool booted = flight_.boot();
  if (booted) {
    takeoff_origin_ = flight_.currentPosition();
    last_target_ = takeoff_origin_;
    has_takeoff_origin_ = true;
    has_last_target_ = true;
  }
  return booted;
}

bool MissionContext::takeoff(double height, double duration_sec) {
  if (!has_takeoff_origin_) {
    takeoff_origin_ = flight_.currentPosition();
    has_takeoff_origin_ = true;
  }
  geometry_msgs::Point target = takeoff_origin_;
  target.z = takeoff_origin_.z + height;
  ROS_INFO("mission takeoff height=%.2f target=(%.2f, %.2f, %.2f)", height, target.x, target.y, target.z);
  rampTo(target, std::max(duration_sec, height / takeoff_climb_rate_mps_));
  last_target_ = target;
  has_last_target_ = true;
  if (!waitUntilNear(target, takeoff_xy_tolerance_m_, takeoff_z_tolerance_m_, takeoff_speed_tolerance_mps_, takeoff_stable_sec_, takeoff_timeout_sec_)) {
    ROS_ERROR("takeoff target not reached within tolerance; hold target and abort mission sequence");
    flight_.holdPosition(target, ros::Duration(5.0));
    return false;
  }
  return true;
}

void MissionContext::moveTo(double x, double y, double z, double duration_sec, bool relative, double yaw, bool use_yaw) {
  geometry_msgs::Point target;
  if (relative) {
    target = flight_.currentPosition();
    target.x += x;
    target.y += y;
    target.z += z;
  } else {
    target.x = x;
    target.y = y;
    target.z = z;
  }
  ROS_INFO("mission move_to target=(%.2f, %.2f, %.2f) duration=%.2f", target.x, target.y, target.z, duration_sec);
  rampTo(target, duration_sec, yaw, use_yaw);
  last_target_ = target;
  has_last_target_ = true;
}

void MissionContext::hover(double duration_sec) {
  const geometry_msgs::Point point = has_last_target_ ? last_target_ : flight_.currentPosition();
  ROS_INFO("mission hover duration=%.2f", duration_sec);
  flight_.holdPosition(point, ros::Duration(duration_sec));
  logSample("hover_end", point);
}

bool MissionContext::land(double duration_sec, double floor_height, bool disarm) {
  geometry_msgs::Point target = has_takeoff_origin_ ? takeoff_origin_ : flight_.currentPosition();
  const double ground_z = has_takeoff_origin_ ? takeoff_origin_.z : target.z;
  const double floor_offset = std::isfinite(floor_height) ? floor_height : landing_floor_height_m_;
  target.z = has_takeoff_origin_ ? takeoff_origin_.z + floor_offset : floor_offset;
  ROS_INFO("mission land target=(%.2f, %.2f, %.2f) floor_offset=%.2f duration=%.2f", target.x, target.y, target.z, floor_height, duration_sec);
  const double descent = std::max(0.0, flight_.currentPosition().z - target.z);
  rampTo(target, std::max(duration_sec, descent / landing_descent_rate_mps_));
  last_target_ = target;
  has_last_target_ = true;
  const bool ground_contact = waitGroundContactWhileHolding(target, ground_z, landing_z_tolerance_m_, landing_vertical_speed_tolerance_mps_, landing_stable_sec_, landing_timeout_sec_);
  if (!ground_contact) {
    ROS_ERROR("vehicle did not reach ground-contact criteria after controlled descent");
    return false;
  }

  if (disarm) {
    return disarmWhileHolding(target, ground_z, disarm_timeout_sec_);
  }
  return true;
}

void MissionContext::openMissionLog() {
  const std::string log_dir = "/tmp/mission_control_logs";
  mkdir(log_dir.c_str(), 0755);
  cleanupMissionLogs(log_dir, 5);

  char timestamp[32];
  std::time_t now = std::time(nullptr);
  std::strftime(timestamp, sizeof(timestamp), "%Y%m%d_%H%M%S", std::localtime(&now));
  const std::string path = log_dir + "/mission_" + timestamp + ".csv";
  mission_log_.open(path.c_str(), std::ios::out | std::ios::trunc);
  if (mission_log_.is_open()) {
    mission_log_ << "wall_time,phase,pos_x,pos_y,pos_z,vel_x,vel_y,vel_z,target_x,target_y,target_z\n";
    ROS_INFO("mission log: %s", path.c_str());
  }
}

void MissionContext::cleanupMissionLogs(const std::string& log_dir, int keep_count) {
  DIR* dir = opendir(log_dir.c_str());
  if (dir == nullptr) {
    return;
  }
  std::vector<std::string> paths;
  while (dirent* entry = readdir(dir)) {
    const std::string name(entry->d_name);
    if (name.find("mission_") == 0 && name.rfind(".csv") == name.size() - 4) {
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

void MissionContext::logSample(const std::string& phase, const geometry_msgs::Point& target) {
  if (!mission_log_.is_open()) {
    return;
  }
  const geometry_msgs::Point current = flight_.currentPosition();
  const geometry_msgs::Vector3 velocity = flight_.currentVelocity();
  mission_log_ << std::fixed << std::setprecision(6)
               << ros::WallTime::now().toSec() << ',' << phase << ','
               << current.x << ',' << current.y << ',' << current.z << ','
               << velocity.x << ',' << velocity.y << ',' << velocity.z << ','
               << target.x << ',' << target.y << ',' << target.z << '\n';
}

bool MissionContext::waitUntilNear(const geometry_msgs::Point& target, double xy_tolerance, double z_tolerance, double speed_tolerance, double stable_sec, double timeout_sec) {
  const ros::WallTime deadline = ros::WallTime::now() + ros::WallDuration(timeout_sec);
  ros::WallTime stable_since;
  bool stable_started = false;
  ros::Rate rate(flight_.rateHz());

  while (ros::ok() && ros::WallTime::now() < deadline) {
    const geometry_msgs::Point current = flight_.currentPosition();
    const double dx = current.x - target.x;
    const double dy = current.y - target.y;
    const double dz = current.z - target.z;
    const geometry_msgs::Vector3 velocity = flight_.currentVelocity();
    const double speed = std::sqrt(velocity.x * velocity.x + velocity.y * velocity.y + velocity.z * velocity.z);
    const bool near = std::sqrt(dx * dx + dy * dy) <= xy_tolerance &&
                      std::abs(dz) <= z_tolerance &&
                      speed <= speed_tolerance;
    flight_.publishPositionTarget(target, 0.0, false);
    logSample("wait_target", target);
    ros::spinOnce();

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

  const geometry_msgs::Point current = flight_.currentPosition();
  ROS_WARN("target wait timeout: current=(%.2f, %.2f, %.2f) target=(%.2f, %.2f, %.2f)", current.x, current.y, current.z, target.x, target.y, target.z);
  return false;
}

bool MissionContext::waitGroundContactWhileHolding(const geometry_msgs::Point& target, double ground_z, double z_tolerance, double vertical_speed_tolerance, double stable_sec, double timeout_sec) {
  const ros::WallTime deadline = ros::WallTime::now() + ros::WallDuration(timeout_sec);
  ros::WallTime stable_since;
  bool stable_started = false;
  ros::Rate rate(flight_.rateHz());

  while (ros::ok() && ros::WallTime::now() < deadline) {
    const geometry_msgs::Point current = flight_.currentPosition();
    const geometry_msgs::Vector3 velocity = flight_.currentVelocity();
    const bool near_ground = current.z <= ground_z + z_tolerance &&
                             std::abs(velocity.z) <= vertical_speed_tolerance;
    flight_.publishPositionTarget(target, 0.0, false);
    logSample("ground_contact", target);
    ros::spinOnce();

    if (near_ground || flight_.landed()) {
      if (!stable_started) {
        stable_started = true;
        stable_since = ros::WallTime::now();
      }
      if ((ros::WallTime::now() - stable_since).toSec() >= stable_sec) {
        ROS_INFO("ground contact criteria stable for %.2fs", stable_sec);
        return true;
      }
    } else {
      stable_started = false;
    }
    rate.sleep();
  }
  const geometry_msgs::Point current = flight_.currentPosition();
  const geometry_msgs::Vector3 velocity = flight_.currentVelocity();
  ROS_WARN("ground contact timeout: current_z=%.3f ground_z=%.3f target_z=%.3f vel_z=%.3f landed=%d", current.z, ground_z, target.z, velocity.z, flight_.landed());
  return false;
}

bool MissionContext::disarmWhileHolding(const geometry_msgs::Point& target, double ground_z, double timeout_sec) {
  const ros::WallTime deadline = ros::WallTime::now() + ros::WallDuration(timeout_sec);
  const ros::WallTime force_allowed_after = ros::WallTime::now() + ros::WallDuration(force_disarm_delay_sec_);
  ros::WallTime last_disarm_request = ros::WallTime(0);
  ros::WallTime last_force_request = ros::WallTime(0);
  ros::Rate rate(flight_.rateHz());

  while (ros::ok() && ros::WallTime::now() < deadline) {
    flight_.publishPositionTarget(target, 0.0, false);
    logSample("disarm", target);
    ros::spinOnce();
    if (!flight_.armed()) {
      ROS_INFO("vehicle disarmed while ground target was held");
      return true;
    }
    if ((ros::WallTime::now() - last_disarm_request).toSec() >= 0.25) {
      flight_.arm(false);
      last_disarm_request = ros::WallTime::now();
    }
    const geometry_msgs::Point current = flight_.currentPosition();
    const geometry_msgs::Vector3 velocity = flight_.currentVelocity();
    const bool safe_to_force = (current.z <= ground_z + 0.045 || flight_.landed()) &&
                               std::abs(velocity.z) <= 0.10;
    if (ros::WallTime::now() >= force_allowed_after && safe_to_force &&
        (ros::WallTime::now() - last_force_request).toSec() >= 0.5) {
      flight_.forceDisarm();
      last_force_request = ros::WallTime::now();
    }
    rate.sleep();
  }
  ROS_ERROR("failed to disarm while holding ground target; %s", statusText().c_str());
  return false;
}

void MissionContext::lock() {
  flight_.lock();
}

std::string MissionContext::statusText() const {
  return flight_.statusText();
}

void MissionContext::rampTo(const geometry_msgs::Point& target, double duration_sec, double yaw, bool use_yaw) {
  const geometry_msgs::Point start = flight_.currentPosition();
  const ros::WallTime start_time = ros::WallTime::now();
  const double duration = std::max(0.1, duration_sec);
  ros::Rate rate(flight_.rateHz());
  while (ros::ok()) {
    const double elapsed = (ros::WallTime::now() - start_time).toSec();
    const double ratio = std::min(1.0, elapsed / duration);
    const geometry_msgs::Point point = TrajectoryGenerator::interpolate(start, target, ratio);
    flight_.publishPositionTarget(point, yaw, use_yaw);
    logSample("ramp", point);
    ros::spinOnce();
    if (ratio >= 1.0) {
      return;
    }
    rate.sleep();
  }
}

}  // namespace mission_control
