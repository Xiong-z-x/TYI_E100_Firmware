#include "mission_control/flight_interface.h"

#include <cmath>
#include <sstream>

namespace mission_control {

FlightInterface::FlightInterface(ros::NodeHandle& nh) : nh_(nh) {
  nh_.param("rate_hz", rate_hz_, 20.0);
  nh_.param("prestream_sec", prestream_sec_, 2.0);
  nh_.param("wait_ready_timeout_sec", wait_ready_timeout_sec_, 8.0);
  nh_.param("odom_max_abs_position_m", odom_max_abs_position_m_, 20.0);
  nh_.param("odom_max_abs_speed_mps", odom_max_abs_speed_mps_, 4.0);
  nh_.param("setpoint_queue_size", setpoint_queue_size_, 50);
  nh_.param("subscriber_queue_size", subscriber_queue_size_, 20);
  nh_.param<std::string>("setpoint_topic", setpoint_topic_, "/mavros/setpoint_raw/local");
  nh_.param<std::string>("state_topic", state_topic_, "/mavros/state");
  nh_.param<std::string>("extended_state_topic", extended_state_topic_, "/mavros/extended_state");
  nh_.param<std::string>("odom_topic", odom_topic_, "/mavros/local_position/odom");
  nh_.param<std::string>("set_mode_service", set_mode_service_, "/mavros/set_mode");
  nh_.param<std::string>("arming_service", arming_service_, "/mavros/cmd/arming");
  nh_.param<std::string>("command_service", command_service_, "/mavros/cmd/command");

  setpoint_pub_ = nh_.advertise<mavros_msgs::PositionTarget>(setpoint_topic_, setpoint_queue_size_);
  state_sub_ = nh_.subscribe(state_topic_, subscriber_queue_size_, &FlightInterface::stateCallback, this);
  extended_state_sub_ = nh_.subscribe(extended_state_topic_, subscriber_queue_size_, &FlightInterface::extendedStateCallback, this);
  odom_sub_ = nh_.subscribe(odom_topic_, subscriber_queue_size_, &FlightInterface::odomCallback, this);
  set_mode_client_ = nh_.serviceClient<mavros_msgs::SetMode>(set_mode_service_);
  arming_client_ = nh_.serviceClient<mavros_msgs::CommandBool>(arming_service_);
  command_client_ = nh_.serviceClient<mavros_msgs::CommandLong>(command_service_);
}

bool FlightInterface::waitReady(const ros::Duration& timeout) {
  const ros::WallTime deadline = ros::WallTime::now() + ros::WallDuration(timeout.toSec());
  ros::Rate rate(rate_hz_);
  while (ros::ok() && ros::WallTime::now() < deadline) {
    ros::spinOnce();
    if (ready()) {
      ROS_INFO_STREAM("flight ready: " << statusText());
      return true;
    }
    rate.sleep();
  }
  ROS_ERROR_STREAM("flight stack is not ready: " << statusText());
  return false;
}

bool FlightInterface::requestOffboard() {
  mavros_msgs::SetMode request;
  request.request.base_mode = 0;
  request.request.custom_mode = "OFFBOARD";
  const bool called = set_mode_client_.call(request);
  ROS_INFO("OFFBOARD request called=%d mode_sent=%d", called, request.response.mode_sent);
  return called && request.response.mode_sent;
}

bool FlightInterface::arm(bool value) {
  mavros_msgs::CommandBool request;
  request.request.value = value;
  const bool called = arming_client_.call(request);
  ROS_INFO("arm request value=%d called=%d success=%d result=%u", value, called, request.response.success, request.response.result);
  return called && request.response.success;
}

bool FlightInterface::forceDisarm() {
  mavros_msgs::CommandLong request;
  request.request.broadcast = false;
  request.request.command = 400;  // MAV_CMD_COMPONENT_ARM_DISARM
  request.request.confirmation = 0;
  request.request.param1 = 0.0;   // disarm
  request.request.param2 = 21196.0;  // PX4 force disarm magic value
  const bool called = command_client_.call(request);
  ROS_WARN("force disarm request called=%d success=%d result=%u", called, request.response.success, request.response.result);
  return called && request.response.success;
}

bool FlightInterface::boot() {
  if (!waitReady(ros::Duration(wait_ready_timeout_sec_))) {
    ROS_ERROR("flight stack is not ready");
    return false;
  }
  const geometry_msgs::Point hold_point = currentPosition();
  holdPosition(hold_point, ros::Duration(prestream_sec_));
  return requestOffboard() && arm(true);
}

void FlightInterface::lock() {
  arm(false);
}

bool FlightInterface::waitLanded(const ros::Duration& timeout, const ros::Duration& stable_duration) {
  const ros::WallTime deadline = ros::WallTime::now() + ros::WallDuration(timeout.toSec());
  ros::WallTime stable_since;
  bool stable_started = false;
  ros::Rate rate(rate_hz_);

  while (ros::ok() && ros::WallTime::now() < deadline) {
    ros::spinOnce();
    const bool on_ground = has_extended_state_ &&
                           extended_state_.landed_state == mavros_msgs::ExtendedState::LANDED_STATE_ON_GROUND;
    if (on_ground) {
      if (!stable_started) {
        stable_started = true;
        stable_since = ros::WallTime::now();
      }
      if ((ros::WallTime::now() - stable_since).toSec() >= stable_duration.toSec()) {
        ROS_INFO("landed state stable for %.2fs", stable_duration.toSec());
        return true;
      }
    } else {
      stable_started = false;
    }
    rate.sleep();
  }

  ROS_WARN("timed out waiting for stable landed state; has_extended_state=%d landed_state=%u",
           has_extended_state_, extended_state_.landed_state);
  return false;
}

bool FlightInterface::disarmUntilLocked(const ros::Duration& timeout) {
  const ros::WallTime deadline = ros::WallTime::now() + ros::WallDuration(timeout.toSec());
  ros::Rate rate(2.0);
  while (ros::ok() && ros::WallTime::now() < deadline) {
    ros::spinOnce();
    if (has_state_ && !state_.armed) {
      ROS_INFO("vehicle disarmed");
      return true;
    }
    arm(false);
    rate.sleep();
  }
  ROS_ERROR("failed to disarm before timeout; %s", statusText().c_str());
  return false;
}

geometry_msgs::Point FlightInterface::currentPosition() const {
  return odom_.pose.pose.position;
}

geometry_msgs::Vector3 FlightInterface::currentVelocity() const {
  return odom_.twist.twist.linear;
}

bool FlightInterface::landed() const {
  return has_extended_state_ && extended_state_.landed_state == mavros_msgs::ExtendedState::LANDED_STATE_ON_GROUND;
}

bool FlightInterface::ready() const {
  return has_state_ && state_.connected && has_odom_ && odomValid();
}

bool FlightInterface::odomValid() const {
  if (!has_odom_) {
    return false;
  }
  const geometry_msgs::Point point = currentPosition();
  const geometry_msgs::Vector3 velocity = currentVelocity();
  return std::isfinite(point.x) && std::isfinite(point.y) && std::isfinite(point.z) &&
         std::isfinite(velocity.x) && std::isfinite(velocity.y) && std::isfinite(velocity.z) &&
         std::abs(point.x) <= odom_max_abs_position_m_ &&
         std::abs(point.y) <= odom_max_abs_position_m_ &&
         std::abs(point.z) <= odom_max_abs_position_m_ &&
         std::abs(velocity.x) <= odom_max_abs_speed_mps_ &&
         std::abs(velocity.y) <= odom_max_abs_speed_mps_ &&
         std::abs(velocity.z) <= odom_max_abs_speed_mps_;
}

std::string FlightInterface::statusText() const {
  std::ostringstream out;
  out << "connected=" << static_cast<int>(state_.connected)
      << " armed=" << static_cast<int>(state_.armed)
      << " mode=" << state_.mode;
  if (has_odom_) {
    const geometry_msgs::Point point = currentPosition();
    out << " pos=(" << point.x << ", " << point.y << ", " << point.z << ")"
        << " odom_valid=" << static_cast<int>(odomValid());
  } else {
    out << " has_odom=0";
  }
  return out.str();
}

void FlightInterface::publishPositionTarget(const geometry_msgs::Point& point, double yaw, bool use_yaw) {
  mavros_msgs::PositionTarget msg;
  msg.header.stamp = ros::Time::now();
  msg.coordinate_frame = mavros_msgs::PositionTarget::FRAME_LOCAL_NED;
  msg.type_mask = mavros_msgs::PositionTarget::IGNORE_VX |
                  mavros_msgs::PositionTarget::IGNORE_VY |
                  mavros_msgs::PositionTarget::IGNORE_VZ |
                  mavros_msgs::PositionTarget::IGNORE_AFX |
                  mavros_msgs::PositionTarget::IGNORE_AFY |
                  mavros_msgs::PositionTarget::IGNORE_AFZ |
                  mavros_msgs::PositionTarget::IGNORE_YAW_RATE;
  if (use_yaw) {
    msg.yaw = yaw;
  } else {
    msg.type_mask |= mavros_msgs::PositionTarget::IGNORE_YAW;
  }
  msg.position = point;
  setpoint_pub_.publish(msg);
}

void FlightInterface::holdPosition(const geometry_msgs::Point& point, const ros::Duration& duration, double yaw, bool use_yaw) {
  const ros::WallTime deadline = ros::WallTime::now() + ros::WallDuration(duration.toSec());
  ros::Rate rate(rate_hz_);
  while (ros::ok() && ros::WallTime::now() < deadline) {
    publishPositionTarget(point, yaw, use_yaw);
    ros::spinOnce();
    rate.sleep();
  }
}

void FlightInterface::stateCallback(const mavros_msgs::State::ConstPtr& msg) {
  state_ = *msg;
  has_state_ = true;
}

void FlightInterface::extendedStateCallback(const mavros_msgs::ExtendedState::ConstPtr& msg) {
  extended_state_ = *msg;
  has_extended_state_ = true;
}

void FlightInterface::odomCallback(const nav_msgs::Odometry::ConstPtr& msg) {
  odom_ = *msg;
  has_odom_ = true;
}

}  // namespace mission_control
