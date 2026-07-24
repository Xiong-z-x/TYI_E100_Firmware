#include <exception>
#include <memory>
#include <string>

#include <ros/ros.h>

#include "mission_control/flight_interface.h"
#include "mission_control/mission_context.h"
#include "mission_control/preflight.h"
#include "mission_control/safety_gate.h"
#include "mission_control/subtasks.h"

namespace {

std::string privateArg(int argc, char** argv, const std::string& name,
                       const std::string& fallback) {
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
  return value == "1" || value == "true" || value == "True" ||
         value == "yes" || value == "on";
}

}  // namespace

int main(int argc, char** argv) {
  const std::string mission_name =
      privateArg(argc, argv, "mission", "idle");
  const bool auto_start =
      parseBool(privateArg(argc, argv, "auto_start", "false"));
  const bool confirm_uav_051 =
      parseBool(privateArg(argc, argv, "confirm_uav_051", "false"));

  ros::init(argc, argv, "mission_main");
  ros::NodeHandle nh("~");
  mission_control::FlightInterface flight(nh);
  const mission_control::SafetyConfig safety_config =
      mission_control::SafetyConfig::uav051Defaults();

  ROS_INFO("mission entry: mission=%s auto_start=%d confirm_uav_051=%d",
           mission_name.c_str(), auto_start, confirm_uav_051);

  if (mission_name == "idle" || mission_name == "status") {
    ROS_INFO("mission control is installed but disabled");
    return 0;
  }

  mission_control::Preflight preflight(nh, flight, safety_config);
  if (mission_name == "check") {
    return preflight.run(mission_control::GatePhase::KillEngaged) ? 0 : 1;
  }

  if (mission_name != "test_flight") {
    ROS_ERROR("unsupported mission: %s", mission_name.c_str());
    return 2;
  }
  if (!auto_start || !confirm_uav_051) {
    ROS_ERROR(
        "test_flight blocked: both auto_start and confirm_uav_051 are "
        "required");
    return 2;
  }
  if (!preflight.run(mission_control::GatePhase::KillReleased)) {
    return 1;
  }
  if (!flight.enableSetpointPublisher()) {
    return 1;
  }

  mission_control::MissionContext mission(flight);
  try {
    const std::unique_ptr<mission_control::Subtask> subtask =
        mission_control::createSubtask(mission_name, nh);
    ROS_INFO("running subtask: %s", subtask->name().c_str());
    const bool ok = subtask->run(mission);
    if (!ok) {
      mission.recoverFromFailure();
      flight.disableSetpointPublisher();
      ROS_ERROR("subtask failed: %s", subtask->name().c_str());
      return 1;
    }
    flight.disableSetpointPublisher();
    ROS_INFO("subtask completed safely: %s", subtask->name().c_str());
    return 0;
  } catch (const std::exception& error) {
    ROS_ERROR("mission exception: %s", error.what());
    mission.recoverFromFailure();
    flight.disableSetpointPublisher();
    return 1;
  }
}
