#include <gtest/gtest.h>

#include "mission_control/preflight_receipt.h"

namespace mission_control {
namespace {

PreflightReceipt validReceipt() {
  PreflightReceipt receipt;
  receipt.uid = 3761439192332449336ULL;
  receipt.sysid = 51;
  receipt.compid = 1;
  receipt.issued_at_sec = 1000.0;
  return receipt;
}

TEST(PreflightReceiptTest, AcceptsRecentMatchingReceipt) {
  const ReceiptValidation result = validateReceipt(
      validReceipt(), 3761439192332449336ULL, 51, 1, 1100.0, 120.0);
  EXPECT_TRUE(result.valid);
  EXPECT_TRUE(result.reason.empty());
}

TEST(PreflightReceiptTest, RejectsExpiredReceipt) {
  const ReceiptValidation result = validateReceipt(
      validReceipt(), 3761439192332449336ULL, 51, 1, 1120.001, 120.0);
  EXPECT_FALSE(result.valid);
  EXPECT_EQ("preflight receipt expired", result.reason);
}

TEST(PreflightReceiptTest, RejectsFutureReceipt) {
  const ReceiptValidation result = validateReceipt(
      validReceipt(), 3761439192332449336ULL, 51, 1, 999.0, 120.0);
  EXPECT_FALSE(result.valid);
  EXPECT_EQ("preflight receipt timestamp is in the future", result.reason);
}

TEST(PreflightReceiptTest, RejectsEveryIdentityMismatch) {
  EXPECT_FALSE(
      validateReceipt(validReceipt(), 1, 51, 1, 1010.0, 120.0).valid);
  EXPECT_FALSE(validateReceipt(validReceipt(), 3761439192332449336ULL, 1, 1,
                               1010.0, 120.0)
                   .valid);
  EXPECT_FALSE(validateReceipt(validReceipt(), 3761439192332449336ULL, 51, 2,
                               1010.0, 120.0)
                   .valid);
}

}  // namespace
}  // namespace mission_control

int main(int argc, char** argv) {
  testing::InitGoogleTest(&argc, argv);
  return RUN_ALL_TESTS();
}
