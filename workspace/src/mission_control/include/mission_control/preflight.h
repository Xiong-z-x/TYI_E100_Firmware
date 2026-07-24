#pragma once

#include <string>

#include <ros/ros.h>

#include "mission_control/flight_interface.h"
#include "mission_control/preflight_receipt.h"
#include "mission_control/safety_gate.h"

namespace mission_control {

class Preflight {
public:
  Preflight(ros::NodeHandle& nh, FlightInterface& flight,
            const SafetyConfig& config);

  bool run(GatePhase phase);

private:
  bool writeReceipt(const SafetySnapshot& snapshot);
  bool readReceipt(PreflightReceipt* receipt) const;
  bool consumeAndValidateReceipt(const SafetySnapshot& snapshot);

  FlightInterface& flight_;
  SafetyConfig config_;
  std::string receipt_path_;
  double observation_timeout_sec_{15.0};
  double observation_window_sec_{3.0};
  double receipt_max_age_sec_{120.0};
};

}  // namespace mission_control
