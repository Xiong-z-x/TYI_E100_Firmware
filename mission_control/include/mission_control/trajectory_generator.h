#pragma once

#include <geometry_msgs/Point.h>

namespace mission_control {

class TrajectoryGenerator {
public:
  static geometry_msgs::Point interpolate(const geometry_msgs::Point& start,
                                          const geometry_msgs::Point& target,
                                          double ratio);
};

}  // namespace mission_control
