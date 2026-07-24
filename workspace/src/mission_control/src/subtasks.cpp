#include "mission_control/subtasks.h"

#include <stdexcept>

namespace mission_control {
namespace {

class StatusSubtask final : public Subtask {
public:
  std::string name() const override { return "status"; }

  bool run(MissionContext& mission) override {
    if (!mission.waitReady()) {
      ROS_ERROR("flight stack is not ready");
      return false;
    }
    ROS_INFO_STREAM(mission.statusText());
    return true;
  }
};

class HoldSubtask final : public Subtask {
public:
  explicit HoldSubtask(double duration_sec) : duration_sec_(duration_sec) {}

  std::string name() const override { return "hold"; }

  bool run(MissionContext& mission) override {
    if (!mission.waitReady()) {
      ROS_ERROR("flight stack is not ready");
      return false;
    }
    mission.hover(duration_sec_);
    return true;
  }

private:
  double duration_sec_;
};

class TestFlightSubtask final : public Subtask {
public:
  explicit TestFlightSubtask(ros::NodeHandle& nh) {
    nh.param("test_flight/takeoff_height_m", takeoff_height_m_, takeoff_height_m_);
    nh.param("test_flight/takeoff_duration_sec", takeoff_duration_sec_, takeoff_duration_sec_);
    nh.param("test_flight/move_distance_m", move_distance_m_, move_distance_m_);
    nh.param("test_flight/move_duration_sec", move_duration_sec_, move_duration_sec_);
    nh.param("test_flight/hover_sec", hover_sec_, hover_sec_);
    nh.param("test_flight/land_duration_sec", land_duration_sec_, land_duration_sec_);
    nh.param("landing/floor_height_m", landing_floor_height_m_, landing_floor_height_m_);
  }

  std::string name() const override { return "test_flight"; }

  bool run(MissionContext& mission) override {
    if (!mission.boot()) {
      return false;
    }
    if (!mission.takeoff(takeoff_height_m_, takeoff_duration_sec_)) {
      return false;
    }
    mission.hover(hover_sec_);
    mission.moveTo(move_distance_m_, 0.0, 0.0, move_duration_sec_, true);
    mission.moveTo(0.0, move_distance_m_, 0.0, move_duration_sec_, true);
    mission.moveTo(-move_distance_m_, 0.0, 0.0, move_duration_sec_, true);
    mission.moveTo(0.0, -move_distance_m_, 0.0, move_duration_sec_, true);
    mission.hover(hover_sec_);
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
  if (name == "status") {
    return std::unique_ptr<Subtask>(new StatusSubtask());
  }
  if (name == "hold") {
    double duration_sec = 2.0;
    nh.param("hold_duration_sec", duration_sec, duration_sec);
    return std::unique_ptr<Subtask>(new HoldSubtask(duration_sec));
  }
  if (name == "test_flight" || name == "takeoff_move_land" || name == "takeoff_hover_land") {
    return std::unique_ptr<Subtask>(new TestFlightSubtask(nh));
  }
  throw std::runtime_error("unknown subtask: " + name);
}

}  // namespace mission_control
