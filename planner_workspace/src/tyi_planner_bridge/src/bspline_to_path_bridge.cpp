#include "bspline_opt/uniform_bspline.h"
#include "nav_msgs/Path.h"
#include "std_msgs/String.h"
#include "traj_utils/Bspline.h"

#include <Eigen/Core>
#include <algorithm>
#include <cmath>
#include <string>
#include <vector>

#include <geometry_msgs/PoseStamped.h>
#include <ros/ros.h>

using ego_planner::UniformBspline;

namespace
{
ros::Publisher g_path_pub;
std::vector<UniformBspline> g_traj;
ros::Time g_start_time;
double g_traj_duration = 0.0;
std::string g_output_frame_id = "world";
double g_sample_dt = 0.05;
int g_max_points = 180;
int g_min_visual_points = 18;
bool g_has_traj = false;

void publishPredictedPath();

void clearPredictedPath()
{
  g_traj.clear();
  g_traj_duration = 0.0;
  g_has_traj = false;
  publishPredictedPath();
}

nav_msgs::Path buildPredictedPath()
{
  nav_msgs::Path path;
  path.header.stamp = ros::Time::now();
  path.header.frame_id = g_output_frame_id;

  if (!g_has_traj || g_traj.empty())
  {
    return path;
  }

  const int max_points = std::max(g_max_points, 1);
  const double sample_dt = std::max(g_sample_dt, 0.01);
  const double duration = std::max(g_traj_duration, 0.0);
  const double t_start = 0.0;
  const double t_end = duration;

  int point_count = 1;
  if (duration > 1e-6)
  {
    point_count = std::min(
        max_points,
        std::max(g_min_visual_points, static_cast<int>(std::ceil(duration / sample_dt)) + 1));
  }

  path.poses.reserve(point_count);
  for (int index = 0; index < point_count; ++index)
  {
    const double ratio = point_count <= 1 ? 0.0 : static_cast<double>(index) / static_cast<double>(point_count - 1);
    const double sample_t = point_count <= 1 ? t_end : (t_start + ratio * duration);
    const Eigen::VectorXd position = g_traj.front().evaluateDeBoorT(sample_t);

    geometry_msgs::PoseStamped pose;
    pose.header = path.header;
    pose.pose.position.x = position(0);
    pose.pose.position.y = position(1);
    pose.pose.position.z = position(2);
    pose.pose.orientation.w = 1.0;
    path.poses.push_back(pose);
  }

  return path;
}

void publishPredictedPath()
{
  g_path_pub.publish(buildPredictedPath());
}

void bsplineCallback(const traj_utils::BsplineConstPtr& msg)
{
  Eigen::MatrixXd pos_pts(3, msg->pos_pts.size());
  for (size_t index = 0; index < msg->pos_pts.size(); ++index)
  {
    pos_pts(0, index) = msg->pos_pts[index].x;
    pos_pts(1, index) = msg->pos_pts[index].y;
    pos_pts(2, index) = msg->pos_pts[index].z;
  }

  Eigen::VectorXd knots(msg->knots.size());
  for (size_t index = 0; index < msg->knots.size(); ++index)
  {
    knots(index) = msg->knots[index];
  }

  const double spline_dt = msg->knots.size() > 1
      ? std::max(1e-3, static_cast<double>(msg->knots[1] - msg->knots[0]))
      : 0.1;
  UniformBspline pos_traj(pos_pts, msg->order, spline_dt);
  pos_traj.setKnot(knots);

  g_traj.clear();
  g_traj.push_back(pos_traj);
  g_start_time = msg->start_time;
  g_traj_duration = g_traj.front().getTimeSum();
  g_has_traj = true;

  publishPredictedPath();
}

void plannerStatusCallback(const std_msgs::StringConstPtr& msg)
{
  const std::string& payload = msg->data;
  const bool idle_state =
      payload.find("\"plannerState\":\"WAIT_TARGET\"") != std::string::npos ||
      payload.find("\"plannerState\":\"INIT\"") != std::string::npos;

  if (idle_state && g_has_traj)
  {
    clearPredictedPath();
  }
}
} // namespace

int main(int argc, char** argv)
{
  ros::init(argc, argv, "bspline_to_path_bridge");
  ros::NodeHandle node_handle;
  ros::NodeHandle private_handle("~");

  std::string input_topic = "/broadcast_bspline";
  std::string output_topic = "/tyi_planner/predicted_path";
  std::string status_topic = "/tyi_planner/status";

  private_handle.param<std::string>("input_topic", input_topic, input_topic);
  private_handle.param<std::string>("output_topic", output_topic, output_topic);
  private_handle.param<std::string>("status_topic", status_topic, status_topic);
  private_handle.param<std::string>("output_frame_id", g_output_frame_id, g_output_frame_id);
  private_handle.param("sample_dt", g_sample_dt, g_sample_dt);
  private_handle.param("max_points", g_max_points, g_max_points);
  private_handle.param("min_visual_points", g_min_visual_points, g_min_visual_points);

  g_path_pub = node_handle.advertise<nav_msgs::Path>(output_topic, 1, true);
  ros::Subscriber subscriber = node_handle.subscribe(input_topic, 5, bsplineCallback);
  ros::Subscriber status_subscriber = node_handle.subscribe(status_topic, 10, plannerStatusCallback);

  ROS_INFO_STREAM("bspline_to_path_bridge listening on " << input_topic << ", publishing " << output_topic);
  ros::spin();

  (void)subscriber;
  (void)status_subscriber;
  return 0;
}
