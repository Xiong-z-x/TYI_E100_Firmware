#include <nav_msgs/Odometry.h>
#include <ros/ros.h>

class OdomRelay {
public:
  OdomRelay() : nh_(), pnh_("~") {
    pnh_.param<std::string>("input_topic", input_topic_, "/robot/fastlio2/odom");
    pnh_.param<std::string>("output_topic", output_topic_, "/drone_0_visual_slam/odom");
    pnh_.param<std::string>("output_frame_id", output_frame_id_, "world");
    pnh_.param<std::string>("output_child_frame_id", output_child_frame_id_, "body");

    pub_ = nh_.advertise<nav_msgs::Odometry>(output_topic_, 10);
    sub_ = nh_.subscribe(input_topic_, 50, &OdomRelay::onOdom, this);
  }

private:
  void onOdom(const nav_msgs::OdometryConstPtr &msg) {
    nav_msgs::Odometry out = *msg;
    out.header.frame_id = output_frame_id_;
    out.child_frame_id = output_child_frame_id_;
    pub_.publish(out);
  }

  ros::NodeHandle nh_;
  ros::NodeHandle pnh_;
  ros::Publisher pub_;
  ros::Subscriber sub_;
  std::string input_topic_;
  std::string output_topic_;
  std::string output_frame_id_;
  std::string output_child_frame_id_;
};

int main(int argc, char **argv) {
  ros::init(argc, argv, "odom_relay");
  OdomRelay relay;
  ros::spin();
  return 0;
}
