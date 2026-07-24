#pragma once

#include <cstdint>
#include <string>

namespace mission_control {

struct PreflightReceipt {
  std::uint64_t uid{0};
  int sysid{0};
  int compid{0};
  double issued_at_sec{0.0};
};

struct ReceiptValidation {
  bool valid{false};
  std::string reason;
};

ReceiptValidation validateReceipt(const PreflightReceipt& receipt,
                                  std::uint64_t expected_uid,
                                  int expected_sysid,
                                  int expected_compid,
                                  double now_sec,
                                  double max_age_sec);

}  // namespace mission_control
