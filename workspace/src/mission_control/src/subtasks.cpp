#include "mission_control/subtasks.h"

#include <stdexcept>
#include <vector>

#include "mission_control/mission_policy.h"
namespace mission_control {
namespace {

class TestFlightSubtask final : public Subtask {
public:
  explicit TestFlightSubtask(ros::NodeHandle& nh) {
    nh.param("test_flight/takeoff_height_m", takeoff_height_m_,
             takeoff_height_m_);
    nh.param("test_flight/takeoff_duration_sec", takeoff_duration_sec_,
             takeoff_duration_sec_);
    nh.param("test_flight/move_distance_m", move_distance_m_,
             move_distance_m_);
    nh.param("test_flight/move_duration_sec", move_duration_sec_,
             move_duration_sec_);
    nh.param("test_flight/hover_sec", hover_sec_, hover_sec_);
    nh.param("test_flight/land_duration_sec", land_duration_sec_,
             land_duration_sec_);
    nh.param("landing/floor_height_m", landing_floor_height_m_,
             landing_floor_height_m_);
  }

  std::string name() const override { return "test_flight"; }

  bool run(MissionContext& mission) override {
    if (!mission.boot()) {
      return false;
    }
    if (!mission.takeoff(takeoff_height_m_, takeoff_duration_sec_)) {
      return false;
    }
    if (!mission.hover(hover_sec_)) {
      return false;
    }

    const geometry_msgs::Point origin = mission.takeoffOrigin();
    const LocalPoint policy_origin{origin.x, origin.y, origin.z};
    const std::vector<LocalPoint> waypoints =
        MissionPolicy::squareWaypoints(
            policy_origin, takeoff_height_m_, move_distance_m_,
            mission.initialYaw());
    for (std::size_t index = 1; index < waypoints.size(); ++index) {
      geometry_msgs::Point target;
      target.x = waypoints[index].x;
      target.y = waypoints[index].y;
      target.z = waypoints[index].z;
      if (!mission.moveToPoint(target, move_duration_sec_)) {
        return false;
      }
    }
    if (!mission.hover(hover_sec_)) {
      return false;
    }
    return mission.land(land_duration_sec_, landing_floor_height_m_, true);
  }

private:
  double takeoff_height_m_{1.0};
  double takeoff_duration_sec_{4.0};
  double move_distance_m_{0.6};
  double move_duration_sec_{4.0};
  double hover_sec_{2.0};
  double land_duration_sec_{5.0};
  double landing_floor_height_m_{-0.03};
};

}  // namespace

std::unique_ptr<Subtask> createSubtask(const std::string& name, ros::NodeHandle& nh) {
  if (name == "test_flight") {
    return std::unique_ptr<Subtask>(new TestFlightSubtask(nh));
  }
  throw std::runtime_error("unknown subtask: " + name);
}

}  // namespace mission_control
