#include "mission_control/flight_interface.h"

#include <algorithm>
#include <cmath>
#include <limits>
#include <set>
#include <sstream>
#include <utility>

#include <XmlRpcValue.h>
#include <ros/master.h>
#include <ros/this_node.h>

namespace mission_control {
namespace {

double quaternionYaw(const geometry_msgs::Quaternion& q) {
  const double siny_cosp = 2.0 * (q.w * q.z + q.x * q.y);
  const double cosy_cosp = 1.0 - 2.0 * (q.y * q.y + q.z * q.z);
  return std::atan2(siny_cosp, cosy_cosp);
}

double wrappedAngleDistance(double a, double b) {
  return std::abs(std::atan2(std::sin(a - b), std::cos(a - b)));
}

bool isRealParameter(const std::string& name) {
  static const std::set<std::string> real_parameters{
      "COM_OF_LOSS_T",
      "COM_RC_LOSS_T",
      "COM_RC_STICK_OV",
  };
  return real_parameters.count(name) != 0;
}

}  // namespace

FlightInterface::FlightInterface(ros::NodeHandle& nh) : nh_(nh) {
  nh_.param("rate_hz", rate_hz_, rate_hz_);
  nh_.param("prestream_sec", prestream_sec_, prestream_sec_);
  nh_.param("wait_ready_timeout_sec", wait_ready_timeout_sec_,
            wait_ready_timeout_sec_);
  nh_.param("data_freshness_sec", data_freshness_sec_, data_freshness_sec_);
  nh_.param("battery_freshness_sec", battery_freshness_sec_,
            battery_freshness_sec_);
  nh_.param("rate_window_sec", rate_window_sec_, rate_window_sec_);
  nh_.param("odom_stability_window_sec", odom_stability_window_sec_,
            odom_stability_window_sec_);
  nh_.param("odom_max_abs_position_m", odom_max_abs_position_m_,
            odom_max_abs_position_m_);
  nh_.param("odom_max_abs_speed_mps", odom_max_abs_speed_mps_,
            odom_max_abs_speed_mps_);
  nh_.param("odom_stable_horizontal_range_m",
            odom_stable_horizontal_range_m_,
            odom_stable_horizontal_range_m_);
  nh_.param("odom_stable_vertical_range_m", odom_stable_vertical_range_m_,
            odom_stable_vertical_range_m_);
  nh_.param("odom_stable_yaw_range_rad", odom_stable_yaw_range_rad_,
            odom_stable_yaw_range_rad_);
  nh_.param("odom_stable_speed_mps", odom_stable_speed_mps_,
            odom_stable_speed_mps_);
  nh_.param("setpoint_queue_size", setpoint_queue_size_,
            setpoint_queue_size_);
  nh_.param("subscriber_queue_size", subscriber_queue_size_,
            subscriber_queue_size_);
  nh_.param("arm_channel_index", arm_channel_index_, arm_channel_index_);
  nh_.param("kill_channel_index", kill_channel_index_, kill_channel_index_);
  nh_.param("switch_pwm_threshold", switch_pwm_threshold_,
            switch_pwm_threshold_);

  nh_.param<std::string>("setpoint_topic", setpoint_topic_,
                         "/mavros/setpoint_raw/local");
  nh_.param<std::string>("state_topic", state_topic_, "/mavros/state");
  nh_.param<std::string>("extended_state_topic", extended_state_topic_,
                         "/mavros/extended_state");
  nh_.param<std::string>("estimator_topic", estimator_topic_,
                         "/mavros/estimator_status");
  nh_.param<std::string>("rc_topic", rc_topic_, "/mavros/rc/in");
  nh_.param<std::string>("battery_topic", battery_topic_, "/mavros/battery");
  nh_.param<std::string>("odom_topic", odom_topic_,
                         "/mavros/local_position/odom");
  nh_.param<std::string>("lidar_topic", lidar_topic_, "/livox/lidar");
  nh_.param<std::string>("imu_topic", imu_topic_, "/livox/imu");
  nh_.param<std::string>("fastlio_topic", fastlio_topic_,
                         "/tyi/e100/fastlio2/odom");
  nh_.param<std::string>("vision_topic", vision_topic_,
                         "/mavros/vision_pose/pose");
  nh_.param<std::string>("set_mode_service", set_mode_service_,
                         "/mavros/set_mode");
  nh_.param<std::string>("arming_service", arming_service_,
                         "/mavros/cmd/arming");
  nh_.param<std::string>("vehicle_info_service", vehicle_info_service_,
                         "/mavros/vehicle_info_get");
  nh_.param<std::string>("param_get_service", param_get_service_,
                         "/mavros/param/get");

  state_sub_ = nh_.subscribe(state_topic_, subscriber_queue_size_,
                             &FlightInterface::stateCallback, this);
  extended_state_sub_ =
      nh_.subscribe(extended_state_topic_, subscriber_queue_size_,
                    &FlightInterface::extendedStateCallback, this);
  estimator_sub_ = nh_.subscribe(estimator_topic_, subscriber_queue_size_,
                                 &FlightInterface::estimatorCallback, this);
  rc_sub_ = nh_.subscribe(rc_topic_, subscriber_queue_size_,
                          &FlightInterface::rcCallback, this);
  battery_sub_ = nh_.subscribe(battery_topic_, subscriber_queue_size_,
                               &FlightInterface::batteryCallback, this);
  odom_sub_ = nh_.subscribe(odom_topic_, subscriber_queue_size_,
                            &FlightInterface::odomCallback, this);
  clock_sub_ = nh_.subscribe("/clock", subscriber_queue_size_,
                             &FlightInterface::clockCallback, this);
  lidar_rate_sub_ =
      nh_.subscribe<topic_tools::ShapeShifter>(
          lidar_topic_, subscriber_queue_size_,
          &FlightInterface::lidarRateCallback, this);
  imu_rate_sub_ =
      nh_.subscribe<topic_tools::ShapeShifter>(
          imu_topic_, subscriber_queue_size_,
          &FlightInterface::imuRateCallback, this);
  fastlio_rate_sub_ =
      nh_.subscribe<topic_tools::ShapeShifter>(
          fastlio_topic_, subscriber_queue_size_,
          &FlightInterface::fastlioRateCallback, this);
  vision_rate_sub_ =
      nh_.subscribe<topic_tools::ShapeShifter>(
          vision_topic_, subscriber_queue_size_,
          &FlightInterface::visionRateCallback, this);

  set_mode_client_ =
      nh_.serviceClient<mavros_msgs::SetMode>(set_mode_service_);
  arming_client_ =
      nh_.serviceClient<mavros_msgs::CommandBool>(arming_service_);
  vehicle_info_client_ =
      nh_.serviceClient<mavros_msgs::VehicleInfoGet>(vehicle_info_service_);
  param_get_client_ =
      nh_.serviceClient<mavros_msgs::ParamGet>(param_get_service_);
}

bool FlightInterface::waitReady(const ros::Duration& timeout) {
  const ros::WallTime deadline =
      ros::WallTime::now() + ros::WallDuration(timeout.toSec());
  ros::WallRate rate(rate_hz_);
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

bool FlightInterface::waitForSafetyObservation(
    const ros::Duration& timeout, double minimum_window_sec) {
  const ros::WallTime deadline =
      ros::WallTime::now() + ros::WallDuration(timeout.toSec());
  ros::WallRate rate(std::max(20.0, rate_hz_));
  while (ros::ok() && ros::WallTime::now() < deadline) {
    ros::spinOnce();
    const auto spans_window =
        [minimum_window_sec](const std::deque<ros::WallTime>& arrivals) {
          return arrivals.size() >= 2 &&
                 (arrivals.back() - arrivals.front()).toSec() >=
                     minimum_window_sec;
        };
    const bool basics = has_state_ && has_extended_state_ && has_estimator_ &&
                        has_rc_ && has_battery_ && has_odom_ && has_clock_;
    if (basics && spans_window(lidar_arrivals_) &&
        spans_window(imu_arrivals_) && spans_window(fastlio_arrivals_) &&
        spans_window(vision_arrivals_) &&
        spans_window(local_odom_arrivals_) && spans_window(rc_arrivals_) &&
        spans_window(clock_arrivals_) && odom_samples_.size() >= 10 &&
        (odom_samples_.back().received - odom_samples_.front().received)
                .toSec() >= minimum_window_sec) {
      return true;
    }
    rate.sleep();
  }
  return false;
}

SafetySnapshot FlightInterface::safetySnapshot(const SafetyConfig& config) {
  SafetySnapshot snapshot;
  readVehicleInfo(&snapshot);
  snapshot.px4_params = readPx4Params(config);
  // Service calls above can take longer than the freshness threshold.
  // Process all queued sensor/state messages immediately before evaluating
  // their receive times.
  ros::spinOnce();

  snapshot.connected =
      has_state_ && dataFresh(state_received_, data_freshness_sec_) &&
      state_.connected;
  snapshot.armed = has_state_ && state_.armed;
  snapshot.manual_input =
      has_state_ && dataFresh(state_received_, data_freshness_sec_) &&
      state_.manual_input;
  snapshot.mode = has_state_ ? state_.mode : "";
  snapshot.on_ground =
      has_extended_state_ &&
      dataFresh(extended_state_received_, data_freshness_sec_) &&
      extended_state_.landed_state ==
          mavros_msgs::ExtendedState::LANDED_STATE_ON_GROUND;

  snapshot.rc_fresh =
      has_rc_ && dataFresh(rc_received_, data_freshness_sec_);
  snapshot.rc_channel_count = has_rc_ ? rc_.channels.size() : 0;
  snapshot.arm_switch_engaged = armSwitchEngaged();
  snapshot.kill_switch_engaged = killEngaged();

  snapshot.battery_fresh =
      has_battery_ &&
      dataFresh(battery_received_, battery_freshness_sec_);
  snapshot.battery_remaining =
      has_battery_ ? static_cast<double>(battery_.percentage) : -1.0;

  snapshot.estimator_fresh =
      has_estimator_ && dataFresh(estimator_received_, data_freshness_sec_);
  if (has_estimator_) {
    snapshot.attitude_valid = estimator_.attitude_status_flag;
    snapshot.velocity_horiz_valid = estimator_.velocity_horiz_status_flag;
    snapshot.velocity_vert_valid = estimator_.velocity_vert_status_flag;
    snapshot.position_horiz_rel_valid =
        estimator_.pos_horiz_rel_status_flag;
    snapshot.position_vert_abs_valid =
        estimator_.pos_vert_abs_status_flag;
    snapshot.gps_glitch = estimator_.gps_glitch_status_flag;
    snapshot.accel_error = estimator_.accel_error_status_flag;
  }

  snapshot.clock_valid =
      has_clock_ && clock_monotonic_ &&
      dataFresh(clock_received_, data_freshness_sec_) &&
      observedRate(clock_arrivals_, rate_window_sec_) >= 20.0;
  snapshot.lidar_hz = observedRate(lidar_arrivals_, rate_window_sec_);
  snapshot.imu_hz = observedRate(imu_arrivals_, rate_window_sec_);
  snapshot.fastlio_hz = observedRate(fastlio_arrivals_, rate_window_sec_);
  snapshot.vision_pose_hz =
      observedRate(vision_arrivals_, rate_window_sec_);
  snapshot.local_odom_hz =
      observedRate(local_odom_arrivals_, rate_window_sec_);
  snapshot.rc_hz = observedRate(rc_arrivals_, rate_window_sec_);
  snapshot.odom_fresh =
      has_odom_ && dataFresh(odom_received_, data_freshness_sec_) &&
      odomValid();
  snapshot.odom_stable = odomStable(odom_stability_window_sec_);
  snapshot.competing_setpoint_publishers =
      competingSetpointPublisherCount();
  return snapshot;
}

bool FlightInterface::enableSetpointPublisher() {
  if (setpoint_publisher_enabled_) {
    return true;
  }
  const int competing = competingSetpointPublisherCount();
  if (competing != 0) {
    ROS_ERROR("refusing setpoint publisher: competing publishers=%d",
              competing);
    return false;
  }
  setpoint_pub_ = nh_.advertise<mavros_msgs::PositionTarget>(
      setpoint_topic_, setpoint_queue_size_);
  setpoint_publisher_enabled_ = true;
  return true;
}

void FlightInterface::disableSetpointPublisher() {
  if (setpoint_publisher_enabled_) {
    setpoint_pub_.shutdown();
    setpoint_publisher_enabled_ = false;
  }
}

int FlightInterface::competingSetpointPublisherCount() const {
  XmlRpc::XmlRpcValue request;
  XmlRpc::XmlRpcValue response;
  XmlRpc::XmlRpcValue payload;
  request.setSize(1);
  request[0] = ros::this_node::getName();
  if (!ros::master::execute("getSystemState", request, response, payload,
                            false) ||
      payload.getType() != XmlRpc::XmlRpcValue::TypeArray ||
      payload.size() < 1) {
    return -1;
  }

  const XmlRpc::XmlRpcValue& publishers = payload[0];
  if (publishers.getType() != XmlRpc::XmlRpcValue::TypeArray) {
    return -1;
  }

  int count = 0;
  for (int i = 0; i < publishers.size(); ++i) {
    const XmlRpc::XmlRpcValue& entry = publishers[i];
    if (entry.getType() != XmlRpc::XmlRpcValue::TypeArray ||
        entry.size() != 2 ||
        static_cast<std::string>(entry[0]) != setpoint_topic_) {
      continue;
    }
    const XmlRpc::XmlRpcValue& nodes = entry[1];
    if (nodes.getType() != XmlRpc::XmlRpcValue::TypeArray) {
      return -1;
    }
    for (int j = 0; j < nodes.size(); ++j) {
      if (static_cast<std::string>(nodes[j]) !=
          ros::this_node::getName()) {
        ++count;
      }
    }
  }
  return count;
}

bool FlightInterface::requestMode(const std::string& mode_value) {
  mavros_msgs::SetMode request;
  request.request.base_mode = 0;
  request.request.custom_mode = mode_value;
  const bool called = set_mode_client_.call(request);
  ROS_INFO("mode request mode=%s called=%d mode_sent=%d",
           mode_value.c_str(), called, request.response.mode_sent);
  return called && request.response.mode_sent;
}

bool FlightInterface::requestOffboard() {
  return requestMode("OFFBOARD");
}

bool FlightInterface::arm(bool value) {
  mavros_msgs::CommandBool request;
  request.request.value = value;
  const bool called = arming_client_.call(request);
  ROS_INFO("arm request value=%d called=%d success=%d result=%u", value,
           called, request.response.success, request.response.result);
  return called && request.response.success;
}

bool FlightInterface::boot() {
  if (!setpoint_publisher_enabled_) {
    ROS_ERROR("cannot boot mission without an enabled setpoint publisher");
    return false;
  }
  if (!waitReady(ros::Duration(wait_ready_timeout_sec_))) {
    return false;
  }

  const geometry_msgs::Point hold_point = currentPosition();
  const double hold_yaw = currentYaw();
  const ros::WallTime prestream_deadline =
      ros::WallTime::now() + ros::WallDuration(prestream_sec_);
  ros::WallRate rate(rate_hz_);
  while (ros::ok() && ros::WallTime::now() < prestream_deadline) {
    ros::spinOnce();
    if (killEngaged() || !controlReliable() || mode() != "POSCTL") {
      ROS_ERROR(
          "prestream aborted: kill=%d reliable=%d mode=%s",
          killEngaged(), controlReliable(), mode().c_str());
      disableSetpointPublisher();
      return false;
    }
    publishPositionTarget(hold_point, hold_yaw, true);
    rate.sleep();
  }
  if (!requestOffboard()) {
    disableSetpointPublisher();
    return false;
  }

  const ros::WallTime mode_deadline =
      ros::WallTime::now() + ros::WallDuration(3.0);
  while (ros::ok() && ros::WallTime::now() < mode_deadline) {
    ros::spinOnce();
    if (killEngaged() || !controlReliable()) {
      ROS_ERROR("OFFBOARD transition aborted by Kill or unreliable state");
      disableSetpointPublisher();
      return false;
    }
    publishPositionTarget(hold_point, hold_yaw, true);
    if (inOffboard()) {
      break;
    }
    rate.sleep();
  }
  if (!inOffboard()) {
    ROS_ERROR("PX4 did not enter OFFBOARD");
    disableSetpointPublisher();
    return false;
  }

  if (killEngaged() || !controlReliable()) {
    ROS_ERROR("arming blocked by Kill or unreliable state");
    disableSetpointPublisher();
    return false;
  }
  if (!arm(true)) {
    disableSetpointPublisher();
    return false;
  }
  const ros::WallTime arm_deadline =
      ros::WallTime::now() + ros::WallDuration(3.0);
  while (ros::ok() && ros::WallTime::now() < arm_deadline) {
    ros::spinOnce();
    if (killEngaged() || !controlReliable() || !inOffboard()) {
      ROS_ERROR(
          "arming confirmation aborted: kill=%d reliable=%d mode=%s",
          killEngaged(), controlReliable(), mode().c_str());
      disableSetpointPublisher();
      return false;
    }
    publishPositionTarget(hold_point, hold_yaw, true);
    if (armed()) {
      return true;
    }
    rate.sleep();
  }
  ROS_ERROR("PX4 did not confirm armed state");
  disableSetpointPublisher();
  return false;
}

void FlightInterface::lock() {
  arm(false);
}

bool FlightInterface::waitLanded(
    const ros::Duration& timeout,
    const ros::Duration& stable_duration) {
  const ros::WallTime deadline =
      ros::WallTime::now() + ros::WallDuration(timeout.toSec());
  ros::WallTime stable_since;
  bool stable_started = false;
  ros::WallRate rate(rate_hz_);

  while (ros::ok() && ros::WallTime::now() < deadline) {
    ros::spinOnce();
    if (landed()) {
      if (!stable_started) {
        stable_started = true;
        stable_since = ros::WallTime::now();
      }
      if ((ros::WallTime::now() - stable_since).toSec() >=
          stable_duration.toSec()) {
        return true;
      }
    } else {
      stable_started = false;
    }
    rate.sleep();
  }
  return false;
}

bool FlightInterface::disarmUntilLocked(const ros::Duration& timeout) {
  const ros::WallTime deadline =
      ros::WallTime::now() + ros::WallDuration(timeout.toSec());
  ros::WallRate rate(2.0);
  while (ros::ok() && ros::WallTime::now() < deadline) {
    ros::spinOnce();
    if (!armed()) {
      return true;
    }
    arm(false);
    rate.sleep();
  }
  return false;
}

geometry_msgs::Point FlightInterface::currentPosition() const {
  return odom_.pose.pose.position;
}

geometry_msgs::Vector3 FlightInterface::currentVelocity() const {
  return odom_.twist.twist.linear;
}

double FlightInterface::currentYaw() const {
  return quaternionYaw(odom_.pose.pose.orientation);
}

std::string FlightInterface::statusText() const {
  std::ostringstream out;
  out << "connected=" << connected() << " armed=" << armed()
      << " mode=" << mode() << " rc=" << has_rc_
      << " kill=" << killEngaged();
  if (has_odom_) {
    const geometry_msgs::Point point = currentPosition();
    out << " pos=(" << point.x << ", " << point.y << ", " << point.z
        << ") odom_valid=" << odomValid();
  } else {
    out << " has_odom=0";
  }
  return out.str();
}

void FlightInterface::publishPositionTarget(
    const geometry_msgs::Point& point, double yaw, bool use_yaw) {
  if (!setpoint_publisher_enabled_) {
    ROS_ERROR_THROTTLE(1.0,
                       "setpoint publication blocked: publisher disabled");
    return;
  }

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

void FlightInterface::holdPosition(
    const geometry_msgs::Point& point, const ros::Duration& duration,
    double yaw, bool use_yaw) {
  const ros::WallTime deadline =
      ros::WallTime::now() + ros::WallDuration(duration.toSec());
  ros::WallRate rate(rate_hz_);
  while (ros::ok() && ros::WallTime::now() < deadline) {
    publishPositionTarget(point, yaw, use_yaw);
    ros::spinOnce();
    rate.sleep();
  }
}

bool FlightInterface::ready() const {
  return connected() && has_odom_ && odomValid() &&
         dataFresh(odom_received_, data_freshness_sec_);
}

bool FlightInterface::connected() const {
  return has_state_ && state_.connected &&
         dataFresh(state_received_, data_freshness_sec_);
}

bool FlightInterface::armed() const {
  return has_state_ && state_.armed;
}

bool FlightInterface::landed() const {
  return has_extended_state_ &&
         extended_state_.landed_state ==
             mavros_msgs::ExtendedState::LANDED_STATE_ON_GROUND;
}

bool FlightInterface::inOffboard() const {
  return has_state_ && state_.mode == "OFFBOARD";
}

bool FlightInterface::killEngaged() const {
  return has_rc_ && kill_channel_index_ >= 0 &&
         static_cast<std::size_t>(kill_channel_index_) <
             rc_.channels.size() &&
         rc_.channels[static_cast<std::size_t>(kill_channel_index_)] >=
             switch_pwm_threshold_;
}

bool FlightInterface::armSwitchEngaged() const {
  return has_rc_ && arm_channel_index_ >= 0 &&
         static_cast<std::size_t>(arm_channel_index_) <
             rc_.channels.size() &&
         rc_.channels[static_cast<std::size_t>(arm_channel_index_)] >=
             switch_pwm_threshold_;
}

bool FlightInterface::controlReliable() const {
  return connected() && has_estimator_ &&
         dataFresh(estimator_received_, data_freshness_sec_) &&
         estimator_.attitude_status_flag &&
         estimator_.velocity_horiz_status_flag &&
         estimator_.velocity_vert_status_flag &&
         estimator_.pos_horiz_rel_status_flag &&
         estimator_.pos_vert_abs_status_flag &&
         !estimator_.accel_error_status_flag && has_odom_ &&
         dataFresh(odom_received_, data_freshness_sec_) && odomValid();
}

std::string FlightInterface::mode() const {
  return has_state_ ? state_.mode : "";
}

bool FlightInterface::odomValid() const {
  if (!has_odom_) {
    return false;
  }
  const geometry_msgs::Point point = currentPosition();
  const geometry_msgs::Vector3 velocity = currentVelocity();
  return std::isfinite(point.x) && std::isfinite(point.y) &&
         std::isfinite(point.z) && std::isfinite(velocity.x) &&
         std::isfinite(velocity.y) && std::isfinite(velocity.z) &&
         std::abs(point.x) <= odom_max_abs_position_m_ &&
         std::abs(point.y) <= odom_max_abs_position_m_ &&
         std::abs(point.z) <= odom_max_abs_position_m_ &&
         std::abs(velocity.x) <= odom_max_abs_speed_mps_ &&
         std::abs(velocity.y) <= odom_max_abs_speed_mps_ &&
         std::abs(velocity.z) <= odom_max_abs_speed_mps_;
}

bool FlightInterface::odomStable(double window_sec) const {
  if (odom_samples_.size() < 10) {
    return false;
  }
  const ros::WallTime cutoff =
      ros::WallTime::now() - ros::WallDuration(window_sec);
  auto first = odom_samples_.begin();
  while (first != odom_samples_.end() && first->received < cutoff) {
    ++first;
  }
  if (first == odom_samples_.end() ||
      (odom_samples_.back().received - first->received).toSec() <
          window_sec * 0.90) {
    return false;
  }

  double min_x = first->position.x;
  double max_x = first->position.x;
  double min_y = first->position.y;
  double max_y = first->position.y;
  double min_z = first->position.z;
  double max_z = first->position.z;
  double max_yaw_delta = 0.0;
  double max_speed = 0.0;
  const double first_yaw = first->yaw;
  for (auto it = first; it != odom_samples_.end(); ++it) {
    min_x = std::min(min_x, it->position.x);
    max_x = std::max(max_x, it->position.x);
    min_y = std::min(min_y, it->position.y);
    max_y = std::max(max_y, it->position.y);
    min_z = std::min(min_z, it->position.z);
    max_z = std::max(max_z, it->position.z);
    max_yaw_delta =
        std::max(max_yaw_delta, wrappedAngleDistance(it->yaw, first_yaw));
    const double speed =
        std::sqrt(it->velocity.x * it->velocity.x +
                  it->velocity.y * it->velocity.y +
                  it->velocity.z * it->velocity.z);
    max_speed = std::max(max_speed, speed);
  }
  const double horizontal_range =
      std::hypot(max_x - min_x, max_y - min_y);
  return horizontal_range <= odom_stable_horizontal_range_m_ &&
         (max_z - min_z) <= odom_stable_vertical_range_m_ &&
         max_yaw_delta <= odom_stable_yaw_range_rad_ &&
         max_speed <= odom_stable_speed_mps_;
}

bool FlightInterface::dataFresh(const ros::WallTime& received,
                                double max_age_sec) const {
  return !received.isZero() &&
         (ros::WallTime::now() - received).toSec() <= max_age_sec;
}

double FlightInterface::observedRate(
    const std::deque<ros::WallTime>& arrivals, double window_sec) const {
  if (arrivals.size() < 2) {
    return 0.0;
  }
  const ros::WallTime cutoff =
      ros::WallTime::now() - ros::WallDuration(window_sec);
  auto first = arrivals.begin();
  while (first != arrivals.end() && *first < cutoff) {
    ++first;
  }
  if (first == arrivals.end()) {
    return 0.0;
  }
  const std::size_t count =
      static_cast<std::size_t>(std::distance(first, arrivals.end()));
  const double elapsed = (arrivals.back() - *first).toSec();
  return count >= 2 && elapsed > 0.0
             ? static_cast<double>(count - 1) / elapsed
             : 0.0;
}

void FlightInterface::recordArrival(
    std::deque<ros::WallTime>* arrivals) {
  arrivals->push_back(ros::WallTime::now());
  trimArrivals(arrivals, std::max(10.0, rate_window_sec_ * 2.0));
}

void FlightInterface::trimArrivals(
    std::deque<ros::WallTime>* arrivals, double window_sec) {
  const ros::WallTime cutoff =
      ros::WallTime::now() - ros::WallDuration(window_sec);
  while (!arrivals->empty() && arrivals->front() < cutoff) {
    arrivals->pop_front();
  }
}

bool FlightInterface::readVehicleInfo(SafetySnapshot* snapshot) {
  mavros_msgs::VehicleInfoGet request;
  request.request.sysid = mavros_msgs::VehicleInfoGetRequest::GET_MY_SYSID;
  request.request.compid =
      mavros_msgs::VehicleInfoGetRequest::GET_MY_COMPID;
  request.request.get_all = false;
  if (!vehicle_info_client_.call(request) || !request.response.success ||
      request.response.vehicles.empty()) {
    return false;
  }
  const mavros_msgs::VehicleInfo& vehicle = request.response.vehicles.front();
  snapshot->uid = vehicle.uid;
  snapshot->sysid = vehicle.sysid;
  snapshot->compid = vehicle.compid;
  return true;
}

std::map<std::string, double> FlightInterface::readPx4Params(
    const SafetyConfig& config) {
  std::map<std::string, double> values;
  for (const auto& expected : config.expected_px4_params) {
    mavros_msgs::ParamGet request;
    request.request.param_id = expected.first;
    if (!param_get_client_.call(request) || !request.response.success) {
      continue;
    }
    values[expected.first] =
        isRealParameter(expected.first)
            ? request.response.value.real
            : static_cast<double>(request.response.value.integer);
  }
  return values;
}

void FlightInterface::stateCallback(
    const mavros_msgs::State::ConstPtr& msg) {
  state_ = *msg;
  has_state_ = true;
  state_received_ = ros::WallTime::now();
}

void FlightInterface::extendedStateCallback(
    const mavros_msgs::ExtendedState::ConstPtr& msg) {
  extended_state_ = *msg;
  has_extended_state_ = true;
  extended_state_received_ = ros::WallTime::now();
}

void FlightInterface::estimatorCallback(
    const mavros_msgs::EstimatorStatus::ConstPtr& msg) {
  estimator_ = *msg;
  has_estimator_ = true;
  estimator_received_ = ros::WallTime::now();
}

void FlightInterface::rcCallback(
    const mavros_msgs::RCIn::ConstPtr& msg) {
  rc_ = *msg;
  has_rc_ = true;
  rc_received_ = ros::WallTime::now();
  recordArrival(&rc_arrivals_);
}

void FlightInterface::batteryCallback(
    const sensor_msgs::BatteryState::ConstPtr& msg) {
  battery_ = *msg;
  has_battery_ = true;
  battery_received_ = ros::WallTime::now();
}

void FlightInterface::odomCallback(
    const nav_msgs::Odometry::ConstPtr& msg) {
  odom_ = *msg;
  has_odom_ = true;
  odom_received_ = ros::WallTime::now();
  recordArrival(&local_odom_arrivals_);

  OdomSample sample;
  sample.received = odom_received_;
  sample.position = msg->pose.pose.position;
  sample.velocity = msg->twist.twist.linear;
  sample.yaw = quaternionYaw(msg->pose.pose.orientation);
  odom_samples_.push_back(sample);
  const ros::WallTime cutoff =
      ros::WallTime::now() -
      ros::WallDuration(std::max(10.0, odom_stability_window_sec_ * 2.0));
  while (!odom_samples_.empty() &&
         odom_samples_.front().received < cutoff) {
    odom_samples_.pop_front();
  }
}

void FlightInterface::clockCallback(
    const rosgraph_msgs::Clock::ConstPtr& msg) {
  if (has_clock_ && msg->clock < last_clock_) {
    clock_monotonic_ = false;
  }
  last_clock_ = msg->clock;
  has_clock_ = true;
  clock_received_ = ros::WallTime::now();
  recordArrival(&clock_arrivals_);
}

void FlightInterface::lidarRateCallback(
    const topic_tools::ShapeShifter::ConstPtr&) {
  recordArrival(&lidar_arrivals_);
}

void FlightInterface::imuRateCallback(
    const topic_tools::ShapeShifter::ConstPtr&) {
  recordArrival(&imu_arrivals_);
}

void FlightInterface::fastlioRateCallback(
    const topic_tools::ShapeShifter::ConstPtr&) {
  recordArrival(&fastlio_arrivals_);
}

void FlightInterface::visionRateCallback(
    const topic_tools::ShapeShifter::ConstPtr&) {
  recordArrival(&vision_arrivals_);
}

}  // namespace mission_control
