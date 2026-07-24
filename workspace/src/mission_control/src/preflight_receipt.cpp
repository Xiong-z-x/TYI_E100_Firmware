#include "mission_control/preflight_receipt.h"

#include <cmath>

namespace mission_control {

ReceiptValidation validateReceipt(const PreflightReceipt& receipt,
                                  std::uint64_t expected_uid,
                                  int expected_sysid,
                                  int expected_compid,
                                  double now_sec,
                                  double max_age_sec) {
  if (receipt.uid != expected_uid) {
    return {false, "preflight receipt UID mismatch"};
  }
  if (receipt.sysid != expected_sysid) {
    return {false, "preflight receipt SYSID mismatch"};
  }
  if (receipt.compid != expected_compid) {
    return {false, "preflight receipt COMPID mismatch"};
  }
  if (!std::isfinite(receipt.issued_at_sec) || !std::isfinite(now_sec) ||
      !std::isfinite(max_age_sec) || max_age_sec <= 0.0) {
    return {false, "preflight receipt time is invalid"};
  }

  const double age_sec = now_sec - receipt.issued_at_sec;
  if (age_sec < 0.0) {
    return {false, "preflight receipt timestamp is in the future"};
  }
  if (age_sec > max_age_sec) {
    return {false, "preflight receipt expired"};
  }
  return {true, ""};
}

}  // namespace mission_control
