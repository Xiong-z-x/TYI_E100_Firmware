#include <mavros_msgs/PositionTarget.h>
#include <quadrotor_msgs/PositionCommand.h>
#include <ros/ros.h>

class PositionCommandToMavros {
public:
  PositionCommandToMavros() : nh_(), pnh_("~") {
    pnh_.param<std::string>("input_topic", input_topic_, "/drone_0_planning/pos_cmd");
    pnh_.param<std::string>("output_topic", output_topic_, "/tyi_planner/mavros/setpoint_raw/local_shadow");
    pnh_.param("use_acceleration", use_acceleration_, false);

    pub_ = nh_.advertise<mavros_msgs::PositionTarget>(output_topic_, 20);
    sub_ = nh_.subscribe(input_topic_, 50, &PositionCommandToMavros::onCommand, this);
  }

private:
  void onCommand(const quadrotor_msgs::PositionCommandConstPtr &msg) {
    mavros_msgs::PositionTarget out;
    out.header = msg->header;
    out.coordinate_frame = mavros_msgs::PositionTarget::FRAME_LOCAL_NED;
    out.type_mask = 0;
    if (!use_acceleration_) {
      out.type_mask |= mavros_msgs::PositionTarget::IGNORE_AFX |
                       mavros_msgs::PositionTarget::IGNORE_AFY |
                       mavros_msgs::PositionTarget::IGNORE_AFZ;
    }

    out.position = msg->position;
    out.velocity = msg->velocity;
    out.acceleration_or_force = msg->acceleration;
    out.yaw = msg->yaw;
    out.yaw_rate = msg->yaw_dot;
    pub_.publish(out);
  }

  ros::NodeHandle nh_;
  ros::NodeHandle pnh_;
  ros::Publisher pub_;
  ros::Subscriber sub_;
  std::string input_topic_;
  std::string output_topic_;
  bool use_acceleration_{false};
};

int main(int argc, char **argv) {
  ros::init(argc, argv, "position_command_to_mavros");
  PositionCommandToMavros node;
  ros::spin();
  return 0;
}
