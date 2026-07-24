#include "mission_control/mission_policy.h"

#include <cmath>
#include <stdexcept>

namespace mission_control {

FailureAction MissionPolicy::onModeChanged(
    const std::string& previous_mode,
    const std::string& current_mode) const {
  if (previous_mode == "OFFBOARD" && current_mode != "OFFBOARD") {
    return FailureAction::YieldToPilot;
  }
  return FailureAction::Continue;
}

FailureAction MissionPolicy::onTaskFailure(bool control_reliable) const {
  return control_reliable ? FailureAction::ControlledLand
                          : FailureAction::StopSetpoints;
}

bool MissionPolicy::forceDisarmAllowed() const {
  return false;
}

std::vector<LocalPoint> MissionPolicy::squareWaypoints(
    const LocalPoint& origin, double height, double side_length) {
  if (!std::isfinite(height) || height <= 0.0 ||
      !std::isfinite(side_length) || side_length <= 0.0) {
    throw std::invalid_argument(
        "height and side_length must be finite and positive");
  }

  const double z = origin.z + height;
  return {
      {origin.x, origin.y, z},
      {origin.x + side_length, origin.y, z},
      {origin.x + side_length, origin.y + side_length, z},
      {origin.x, origin.y + side_length, z},
      {origin.x, origin.y, z},
  };
}

}  // namespace mission_control
