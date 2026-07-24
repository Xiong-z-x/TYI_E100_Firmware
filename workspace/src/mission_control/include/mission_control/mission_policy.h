#pragma once

#include <string>
#include <vector>

namespace mission_control {

enum class FailureAction {
  Continue,
  YieldToPilot,
  ControlledLand,
  StopSetpoints,
};

struct LocalPoint {
  double x{0.0};
  double y{0.0};
  double z{0.0};
};

class MissionPolicy {
public:
  FailureAction onModeChanged(const std::string& previous_mode,
                              const std::string& current_mode) const;
  FailureAction onTaskFailure(bool control_reliable) const;
  bool forceDisarmAllowed() const;

  static double nearestCardinalYaw(double yaw);
  static std::vector<LocalPoint> squareWaypoints(const LocalPoint& origin,
                                                 double height,
                                                 double side_length,
                                                 double yaw);
};

}  // namespace mission_control
