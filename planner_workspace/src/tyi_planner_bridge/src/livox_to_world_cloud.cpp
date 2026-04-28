#include <algorithm>
#include <cmath>
#include <mutex>
#include <string>

#include <Eigen/Geometry>
#include <livox_ros_driver2/CustomMsg.h>
#include <nav_msgs/Odometry.h>
#include <pcl/point_cloud.h>
#include <pcl/point_types.h>
#include <pcl_conversions/pcl_conversions.h>
#include <ros/ros.h>
#include <sensor_msgs/PointCloud2.h>

class LivoxToWorldCloud {
public:
  LivoxToWorldCloud() : nh_(), pnh_("~") {
    pnh_.param<std::string>("livox_topic", livox_topic_, "/livox/lidar");
    pnh_.param<std::string>("odom_topic", odom_topic_, "/robot/fastlio2/odom");
    pnh_.param<std::string>("output_topic", output_topic_, "/drone_0_planner/cloud");
    pnh_.param<std::string>("output_frame_id", output_frame_id_, "world");
    pnh_.param("point_stride", point_stride_, 6);
    pnh_.param("min_range", min_range_, 0.8);
    pnh_.param("max_range", max_range_, 12.0);

    std::vector<double> extrinsic_xyz{0.0, 0.0, 0.0};
    std::vector<double> extrinsic_rpy_deg{0.0, 0.0, 0.0};
    pnh_.param("extrinsic_xyz", extrinsic_xyz, extrinsic_xyz);
    pnh_.param("extrinsic_rpy_deg", extrinsic_rpy_deg, extrinsic_rpy_deg);

    t_bl_ = Eigen::Vector3d(extrinsic_xyz[0], extrinsic_xyz[1], extrinsic_xyz[2]);
    const double roll = extrinsic_rpy_deg[0] * M_PI / 180.0;
    const double pitch = extrinsic_rpy_deg[1] * M_PI / 180.0;
    const double yaw = extrinsic_rpy_deg[2] * M_PI / 180.0;
    r_bl_ = Eigen::AngleAxisd(yaw, Eigen::Vector3d::UnitZ()) *
            Eigen::AngleAxisd(pitch, Eigen::Vector3d::UnitY()) *
            Eigen::AngleAxisd(roll, Eigen::Vector3d::UnitX());

    odom_sub_ = nh_.subscribe(odom_topic_, 100, &LivoxToWorldCloud::onOdom, this);
    livox_sub_ = nh_.subscribe(livox_topic_, 10, &LivoxToWorldCloud::onLivox, this);
    cloud_pub_ = nh_.advertise<sensor_msgs::PointCloud2>(output_topic_, 10);
  }

private:
  void onOdom(const nav_msgs::OdometryConstPtr &msg) {
    std::lock_guard<std::mutex> lock(mutex_);
    latest_odom_ = *msg;
    have_odom_ = true;
  }

  void onLivox(const livox_ros_driver2::CustomMsgConstPtr &msg) {
    nav_msgs::Odometry odom;
    {
      std::lock_guard<std::mutex> lock(mutex_);
      if (!have_odom_) {
        return;
      }
      odom = latest_odom_;
    }

    Eigen::Quaterniond q_wb(
        odom.pose.pose.orientation.w,
        odom.pose.pose.orientation.x,
        odom.pose.pose.orientation.y,
        odom.pose.pose.orientation.z);
    if (!q_wb.norm()) {
      return;
    }
    q_wb.normalize();
    const Eigen::Vector3d t_wb(
        odom.pose.pose.position.x,
        odom.pose.pose.position.y,
        odom.pose.pose.position.z);

    pcl::PointCloud<pcl::PointXYZI> cloud;
    cloud.points.reserve(msg->points.size() / std::max(1, point_stride_));

    for (size_t i = 0; i < msg->points.size(); i += static_cast<size_t>(std::max(1, point_stride_))) {
      const auto &pt = msg->points[i];
      Eigen::Vector3d p_lidar(pt.x, pt.y, pt.z);
      if (p_lidar.norm() < min_range_) {
        continue;
      }
      if (p_lidar.norm() > max_range_) {
        continue;
      }

      const Eigen::Vector3d p_body = r_bl_ * p_lidar + t_bl_;
      const Eigen::Vector3d p_world = q_wb * p_body + t_wb;

      pcl::PointXYZI out;
      out.x = static_cast<float>(p_world.x());
      out.y = static_cast<float>(p_world.y());
      out.z = static_cast<float>(p_world.z());
      out.intensity = static_cast<float>(pt.reflectivity);
      cloud.push_back(out);
    }

    cloud.width = static_cast<uint32_t>(cloud.size());
    cloud.height = 1;
    cloud.is_dense = false;

    sensor_msgs::PointCloud2 out_msg;
    pcl::toROSMsg(cloud, out_msg);
    out_msg.header.stamp = msg->header.stamp;
    out_msg.header.frame_id = output_frame_id_;
    cloud_pub_.publish(out_msg);
  }

  ros::NodeHandle nh_;
  ros::NodeHandle pnh_;
  ros::Subscriber odom_sub_;
  ros::Subscriber livox_sub_;
  ros::Publisher cloud_pub_;
  std::mutex mutex_;
  nav_msgs::Odometry latest_odom_;
  bool have_odom_{false};
  std::string livox_topic_;
  std::string odom_topic_;
  std::string output_topic_;
  std::string output_frame_id_;
  int point_stride_{6};
  double min_range_{0.8};
  double max_range_{12.0};
  Eigen::Quaterniond r_bl_{Eigen::Quaterniond::Identity()};
  Eigen::Vector3d t_bl_{Eigen::Vector3d::Zero()};
};

int main(int argc, char **argv) {
  ros::init(argc, argv, "livox_to_world_cloud");
  LivoxToWorldCloud node;
  ros::spin();
  return 0;
}
