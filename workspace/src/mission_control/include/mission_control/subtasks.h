#pragma once

#include <memory>
#include <string>

#include <ros/ros.h>

#include "mission_control/mission_context.h"

namespace mission_control {

class Subtask {
public:
  virtual ~Subtask() = default;
  virtual std::string name() const = 0;
  virtual bool run(MissionContext& mission) = 0;
};

std::unique_ptr<Subtask> createSubtask(const std::string& name, ros::NodeHandle& nh);

}  // namespace mission_control
