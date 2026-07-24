#include <cstdlib>
#include <string>

#include <ros/ros.h>

#include "mission_control/flight_interface.h"
#include "mission_control/mission_context.h"
#include "mission_control/subtasks.h"

namespace {

std::string privateArg(int argc, char** argv, const std::string& name, const std::string& fallback) {
  const std::string prefix = "_" + name + ":=";
  for (int i = 1; i < argc; ++i) {
    const std::string arg(argv[i]);
    if (arg.compare(0, prefix.size(), prefix) == 0) {
      return arg.substr(prefix.size());
    }
  }
  return fallback;
}

bool parseBool(const std::string& value) {
  return value == "1" || value == "true" || value == "True" || value == "yes" || value == "on";
}

}  // namespace

int main(int argc, char** argv) {
  const std::string mission_name = privateArg(argc, argv, "mission", "idle");
  const bool auto_start = parseBool(privateArg(argc, argv, "auto_start", "false"));

  ros::init(argc, argv, "mission_main");
  ros::NodeHandle nh("~");

  mission_control::FlightInterface flight(nh);
  mission_control::MissionContext mission(flight);

  ROS_INFO("main mission loaded: mission=%s auto_start=%d", mission_name.c_str(), auto_start);
  if (mission_name == "idle" || !auto_start) {
    if (mission.waitReady()) {
      ROS_INFO_STREAM("idle ready: " << mission.statusText());
    }
    ros::Rate rate(1.0);
    while (ros::ok()) {
      ros::spinOnce();
      rate.sleep();
    }
    return 0;
  }

  try {
    const std::unique_ptr<mission_control::Subtask> subtask = mission_control::createSubtask(mission_name, nh);
    ROS_INFO("running subtask: %s", subtask->name().c_str());
    const bool ok = subtask->run(mission);
    ROS_INFO("subtask completed: %s ok=%d", subtask->name().c_str(), ok);
    return ok ? 0 : 1;
  } catch (const std::exception& error) {
    ROS_ERROR("main mission failed: %s", error.what());
    return 1;
  }
}
