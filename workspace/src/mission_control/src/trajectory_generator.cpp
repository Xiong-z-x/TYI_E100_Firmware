#include "mission_control/trajectory_generator.h"

#include <algorithm>
#include <cmath>

namespace mission_control {

geometry_msgs::Point TrajectoryGenerator::interpolate(const geometry_msgs::Point& start,
                                                      const geometry_msgs::Point& target,
                                                      double ratio) {
  ratio = std::max(0.0, std::min(1.0, ratio));
  const double eased = 0.5 - 0.5 * std::cos(M_PI * ratio);
  geometry_msgs::Point point;
  point.x = start.x + (target.x - start.x) * eased;
  point.y = start.y + (target.y - start.y) * eased;
  point.z = start.z + (target.z - start.z) * eased;
  return point;
}

}  // namespace mission_control
