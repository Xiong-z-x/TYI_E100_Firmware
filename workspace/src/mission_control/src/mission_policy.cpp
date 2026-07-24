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

double MissionPolicy::nearestCardinalYaw(double yaw) {
  if (!std::isfinite(yaw)) {
    throw std::invalid_argument("yaw must be finite");
  }
  const double half_pi = std::acos(-1.0) / 2.0;
  return std::round(yaw / half_pi) * half_pi;
}

std::vector<LocalPoint> MissionPolicy::squareWaypoints(
    const LocalPoint& origin, double height, double side_length, double yaw) {
  if (!std::isfinite(height) || height <= 0.0 ||
      !std::isfinite(side_length) || side_length <= 0.0 ||
      !std::isfinite(yaw)) {
    throw std::invalid_argument(
        "height and side_length must be finite and positive; yaw must be finite");
  }

  const double z = origin.z + height;
  const double cos_yaw = std::cos(yaw);
  const double sin_yaw = std::sin(yaw);
  const auto rotate = [&](double forward, double left) {
    return LocalPoint{
        origin.x + forward * cos_yaw - left * sin_yaw,
        origin.y + forward * sin_yaw + left * cos_yaw,
        z,
    };
  };

  return {
      rotate(0.0, 0.0),
      rotate(side_length, 0.0),
      rotate(side_length, side_length),
      rotate(0.0, side_length),
      rotate(0.0, 0.0),
  };
}

}  // namespace mission_control
