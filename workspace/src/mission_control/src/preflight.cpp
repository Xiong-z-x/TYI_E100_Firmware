#include "mission_control/preflight.h"

#include <cstdio>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <sys/stat.h>

namespace mission_control {

Preflight::Preflight(ros::NodeHandle& nh, FlightInterface& flight,
                     const SafetyConfig& config)
    : flight_(flight), config_(config) {
  nh.param<std::string>("preflight_receipt_path", receipt_path_,
                         "/tmp/mission_control/precheck.receipt");
  nh.param("preflight_observation_timeout_sec", observation_timeout_sec_,
            observation_timeout_sec_);
  nh.param("preflight_observation_window_sec", observation_window_sec_,
            observation_window_sec_);
  nh.param("preflight_receipt_max_age_sec", receipt_max_age_sec_,
            receipt_max_age_sec_);
}

bool Preflight::run(GatePhase phase) {
  if (!flight_.waitForSafetyObservation(
          ros::Duration(observation_timeout_sec_),
          observation_window_sec_)) {
    ROS_ERROR("PRECHECK FAIL: timed out collecting safety observations");
    return false;
  }

  const SafetySnapshot snapshot = flight_.safetySnapshot(config_);
  const GateResult result = SafetyGate(config_).evaluate(snapshot, phase);
  for (const std::string& failure : result.failures) {
    ROS_ERROR_STREAM("PRECHECK FAIL: " << failure);
  }
  if (!result.passed) {
    return false;
  }

  if (phase == GatePhase::KillEngaged) {
    if (!writeReceipt(snapshot)) {
      ROS_ERROR("PRECHECK FAIL: could not write first-stage receipt");
      return false;
    }
    ROS_INFO("PRECHECK PASS - SAFE TO RELEASE KILL");
    return true;
  }

  if (!consumeAndValidateReceipt(snapshot)) {
    return false;
  }
  ROS_INFO("PRECHECK PASS - KILL RELEASED, MISSION START PERMITTED");
  return true;
}

bool Preflight::writeReceipt(const SafetySnapshot& snapshot) {
  const std::string directory =
      receipt_path_.substr(0, receipt_path_.find_last_of('/'));
  if (!directory.empty() && mkdir(directory.c_str(), 0750) != 0) {
    struct stat info {};
    if (stat(directory.c_str(), &info) != 0 || !S_ISDIR(info.st_mode)) {
      return false;
    }
  }

  const std::string temporary = receipt_path_ + ".tmp";
  {
    std::ofstream out(temporary.c_str(),
                      std::ios::out | std::ios::trunc);
    if (!out.is_open()) {
      return false;
    }
    out << snapshot.uid << '\n'
        << snapshot.sysid << '\n'
        << snapshot.compid << '\n'
        << std::fixed << std::setprecision(6)
        << ros::WallTime::now().toSec() << '\n';
    if (!out.good()) {
      return false;
    }
  }
  if (chmod(temporary.c_str(), 0600) != 0 ||
      std::rename(temporary.c_str(), receipt_path_.c_str()) != 0) {
    std::remove(temporary.c_str());
    return false;
  }
  return true;
}

bool Preflight::readReceipt(PreflightReceipt* receipt) const {
  std::ifstream in(receipt_path_.c_str());
  if (!in.is_open()) {
    return false;
  }
  return static_cast<bool>(
      in >> receipt->uid >> receipt->sysid >> receipt->compid >>
          receipt->issued_at_sec);
}

bool Preflight::consumeAndValidateReceipt(
    const SafetySnapshot& snapshot) {
  PreflightReceipt receipt;
  if (!readReceipt(&receipt)) {
    ROS_ERROR("PRECHECK FAIL: run 'scripts/mission check' first");
    return false;
  }
  const ReceiptValidation validation = validateReceipt(
      receipt, snapshot.uid, snapshot.sysid, snapshot.compid,
      ros::WallTime::now().toSec(), receipt_max_age_sec_);
  if (!validation.valid) {
    ROS_ERROR_STREAM("PRECHECK FAIL: " << validation.reason);
    return false;
  }
  if (std::remove(receipt_path_.c_str()) != 0) {
    ROS_ERROR("PRECHECK FAIL: could not consume preflight receipt");
    return false;
  }
  return true;
}

}  // namespace mission_control
