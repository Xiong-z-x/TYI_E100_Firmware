#pragma once

#include <cstddef>
#include <cstdint>
#include <deque>
#include <map>
#include <string>

#include <geometry_msgs/Point.h>
#include <geometry_msgs/Vector3.h>
#include <mavros_msgs/CommandBool.h>
#include <mavros_msgs/EstimatorStatus.h>
#include <mavros_msgs/ExtendedState.h>
#include <mavros_msgs/ParamGet.h>
#include <mavros_msgs/PositionTarget.h>
#include <mavros_msgs/RCIn.h>
#include <mavros_msgs/SetMode.h>
#include <mavros_msgs/State.h>
#include <mavros_msgs/VehicleInfoGet.h>
#include <nav_msgs/Odometry.h>
#include <ros/ros.h>
#include <rosgraph_msgs/Clock.h>
#include <sensor_msgs/BatteryState.h>
#include <topic_tools/shape_shifter.h>

#include "mission_control/safety_gate.h"

namespace mission_control {

class FlightInterface {
public:
  explicit FlightInterface(ros::NodeHandle& nh);

  bool waitReady(const ros::Duration& timeout);
  bool waitForSafetyObservation(const ros::Duration& timeout,
                                double minimum_window_sec);
  SafetySnapshot safetySnapshot(const SafetyConfig& config);

  bool enableSetpointPublisher();
  void disableSetpointPublisher();
  bool setpointPublisherEnabled() const { return setpoint_publisher_enabled_; }
  int competingSetpointPublisherCount() const;

  bool requestMode(const std::string& mode);
  bool requestOffboard();
  bool arm(bool value);
  bool boot();
  void lock();
  bool waitLanded(const ros::Duration& timeout,
                  const ros::Duration& stable_duration);
  bool waitDisarmed(const ros::Duration& timeout);
  bool disarmUntilLocked(const ros::Duration& timeout);

  geometry_msgs::Point currentPosition() const;
  geometry_msgs::Vector3 currentVelocity() const;
  double currentYaw() const;
  std::string statusText() const;
  void publishPositionTarget(const geometry_msgs::Point& point, double yaw,
                             bool use_yaw);
  void holdPosition(const geometry_msgs::Point& point,
                    const ros::Duration& duration, double yaw = 0.0,
                    bool use_yaw = false);

  double rateHz() const { return rate_hz_; }
  bool ready() const;
  bool connected() const;
  bool armed() const;
  bool landed() const;
  bool inOffboard() const;
  bool killEngaged() const;
  bool armSwitchEngaged() const;
  bool controlReliable() const;
  std::string mode() const;

private:
  struct OdomSample {
    ros::WallTime received;
    geometry_msgs::Point position;
    geometry_msgs::Vector3 velocity;
    double yaw{0.0};
  };

  bool odomValid() const;
  bool odomStable(double window_sec) const;
  bool dataFresh(const ros::WallTime& received, double max_age_sec) const;
  double observedRate(const std::deque<ros::WallTime>& arrivals,
                      double window_sec) const;
  void recordArrival(std::deque<ros::WallTime>* arrivals);
  void trimArrivals(std::deque<ros::WallTime>* arrivals,
                    double window_sec);
  bool readVehicleInfo(SafetySnapshot* snapshot);
  std::map<std::string, double> readPx4Params(
      const SafetyConfig& config);

  void stateCallback(const mavros_msgs::State::ConstPtr& msg);
  void extendedStateCallback(
      const mavros_msgs::ExtendedState::ConstPtr& msg);
  void estimatorCallback(
      const mavros_msgs::EstimatorStatus::ConstPtr& msg);
  void rcCallback(const mavros_msgs::RCIn::ConstPtr& msg);
  void batteryCallback(const sensor_msgs::BatteryState::ConstPtr& msg);
  void odomCallback(const nav_msgs::Odometry::ConstPtr& msg);
  void clockCallback(const rosgraph_msgs::Clock::ConstPtr& msg);
  void lidarRateCallback(const topic_tools::ShapeShifter::ConstPtr&);
  void imuRateCallback(const topic_tools::ShapeShifter::ConstPtr&);
  void fastlioRateCallback(const topic_tools::ShapeShifter::ConstPtr&);
  void visionRateCallback(const topic_tools::ShapeShifter::ConstPtr&);

  ros::NodeHandle nh_;
  ros::Publisher setpoint_pub_;
  ros::Subscriber state_sub_;
  ros::Subscriber extended_state_sub_;
  ros::Subscriber estimator_sub_;
  ros::Subscriber rc_sub_;
  ros::Subscriber battery_sub_;
  ros::Subscriber odom_sub_;
  ros::Subscriber clock_sub_;
  ros::Subscriber lidar_rate_sub_;
  ros::Subscriber imu_rate_sub_;
  ros::Subscriber fastlio_rate_sub_;
  ros::Subscriber vision_rate_sub_;
  ros::ServiceClient set_mode_client_;
  ros::ServiceClient arming_client_;
  ros::ServiceClient vehicle_info_client_;
  ros::ServiceClient param_get_client_;

  mavros_msgs::State state_;
  mavros_msgs::ExtendedState extended_state_;
  mavros_msgs::EstimatorStatus estimator_;
  mavros_msgs::RCIn rc_;
  sensor_msgs::BatteryState battery_;
  nav_msgs::Odometry odom_;
  bool has_state_{false};
  bool has_extended_state_{false};
  bool has_estimator_{false};
  bool has_rc_{false};
  bool has_battery_{false};
  bool has_odom_{false};
  bool has_clock_{false};
  bool clock_monotonic_{true};
  ros::Time last_clock_;

  ros::WallTime state_received_;
  ros::WallTime extended_state_received_;
  ros::WallTime estimator_received_;
  ros::WallTime rc_received_;
  ros::WallTime battery_received_;
  ros::WallTime odom_received_;
  ros::WallTime clock_received_;

  std::deque<ros::WallTime> lidar_arrivals_;
  std::deque<ros::WallTime> imu_arrivals_;
  std::deque<ros::WallTime> fastlio_arrivals_;
  std::deque<ros::WallTime> vision_arrivals_;
  std::deque<ros::WallTime> local_odom_arrivals_;
  std::deque<ros::WallTime> rc_arrivals_;
  std::deque<ros::WallTime> clock_arrivals_;
  std::deque<OdomSample> odom_samples_;

  bool setpoint_publisher_enabled_{false};
  double rate_hz_{20.0};
  double prestream_sec_{3.0};
  double wait_ready_timeout_sec_{12.0};
  double data_freshness_sec_{0.5};
  double status_freshness_sec_{1.5};
  double battery_freshness_sec_{3.0};
  double rate_window_sec_{4.0};
  double odom_stability_window_sec_{3.0};
  double odom_max_abs_position_m_{20.0};
  double odom_max_abs_speed_mps_{4.0};
  double odom_stable_horizontal_range_m_{0.12};
  double odom_stable_vertical_range_m_{0.10};
  double odom_stable_yaw_range_rad_{0.0523598776};
  double odom_stable_speed_mps_{0.30};
  int setpoint_queue_size_{50};
  int subscriber_queue_size_{100};
  int arm_channel_index_{5};
  int kill_channel_index_{6};
  int switch_pwm_threshold_{1500};
  std::string setpoint_topic_;
  std::string state_topic_;
  std::string extended_state_topic_;
  std::string estimator_topic_;
  std::string rc_topic_;
  std::string battery_topic_;
  std::string odom_topic_;
  std::string lidar_topic_;
  std::string imu_topic_;
  std::string fastlio_topic_;
  std::string vision_topic_;
  std::string set_mode_service_;
  std::string arming_service_;
  std::string vehicle_info_service_;
  std::string param_get_service_;
};

}  // namespace mission_control
