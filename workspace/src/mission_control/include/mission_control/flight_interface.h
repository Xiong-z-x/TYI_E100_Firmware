#pragma once

#include <geometry_msgs/Point.h>
#include <geometry_msgs/Vector3.h>
#include <mavros_msgs/ExtendedState.h>
#include <mavros_msgs/PositionTarget.h>
#include <mavros_msgs/State.h>
#include <mavros_msgs/CommandBool.h>
#include <mavros_msgs/CommandLong.h>
#include <mavros_msgs/SetMode.h>
#include <nav_msgs/Odometry.h>
#include <ros/ros.h>

namespace mission_control {

class FlightInterface {
public:
  explicit FlightInterface(ros::NodeHandle& nh);

  bool waitReady(const ros::Duration& timeout);
  bool requestOffboard();
  bool arm(bool value);
  bool forceDisarm();
  bool boot();
  void lock();
  bool waitLanded(const ros::Duration& timeout, const ros::Duration& stable_duration);
  bool disarmUntilLocked(const ros::Duration& timeout);

  geometry_msgs::Point currentPosition() const;
  geometry_msgs::Vector3 currentVelocity() const;
  std::string statusText() const;
  void publishPositionTarget(const geometry_msgs::Point& point, double yaw, bool use_yaw);
  void holdPosition(const geometry_msgs::Point& point, const ros::Duration& duration, double yaw = 0.0, bool use_yaw = false);

  double rateHz() const { return rate_hz_; }
  bool ready() const;
  bool armed() const { return has_state_ && state_.armed; }
  bool landed() const;

private:
  bool odomValid() const;
  void stateCallback(const mavros_msgs::State::ConstPtr& msg);
  void extendedStateCallback(const mavros_msgs::ExtendedState::ConstPtr& msg);
  void odomCallback(const nav_msgs::Odometry::ConstPtr& msg);

  ros::NodeHandle nh_;
  ros::Publisher setpoint_pub_;
  ros::Subscriber state_sub_;
  ros::Subscriber extended_state_sub_;
  ros::Subscriber odom_sub_;
  ros::ServiceClient set_mode_client_;
  ros::ServiceClient arming_client_;
  ros::ServiceClient command_client_;

  mavros_msgs::State state_;
  mavros_msgs::ExtendedState extended_state_;
  nav_msgs::Odometry odom_;
  bool has_state_{false};
  bool has_extended_state_{false};
  bool has_odom_{false};

  double rate_hz_{20.0};
  double prestream_sec_{2.0};
  double wait_ready_timeout_sec_{8.0};
  double odom_max_abs_position_m_{20.0};
  double odom_max_abs_speed_mps_{4.0};
  int setpoint_queue_size_{50};
  int subscriber_queue_size_{20};
  std::string setpoint_topic_;
  std::string state_topic_;
  std::string extended_state_topic_;
  std::string odom_topic_;
  std::string set_mode_service_;
  std::string arming_service_;
  std::string command_service_;
};

}  // namespace mission_control
