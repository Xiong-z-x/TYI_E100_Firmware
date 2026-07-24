#include <gtest/gtest.h>

#include "mission_control/mission_policy.h"

namespace mission_control {
namespace {

TEST(MissionPolicyTest, GivesPilotPriorityAfterLeavingOffboard) {
  MissionPolicy policy;
  EXPECT_EQ(FailureAction::YieldToPilot,
            policy.onModeChanged("OFFBOARD", "POSCTL"));
  EXPECT_EQ(FailureAction::YieldToPilot,
            policy.onModeChanged("OFFBOARD", "ALTCTL"));
}

TEST(MissionPolicyTest, UsesControlledLandingOnlyWithReliableControl) {
  MissionPolicy policy;
  EXPECT_EQ(FailureAction::ControlledLand, policy.onTaskFailure(true));
  EXPECT_EQ(FailureAction::StopSetpoints, policy.onTaskFailure(false));
}

TEST(MissionPolicyTest, NeverAllowsForceDisarm) {
  MissionPolicy policy;
  EXPECT_FALSE(policy.forceDisarmAllowed());
}

TEST(MissionPolicyTest, AnchorsSquareToTakeoffOrigin) {
  const LocalPoint origin{2.0, -3.0, 0.4};
  const std::vector<LocalPoint> points =
      MissionPolicy::squareWaypoints(origin, 1.2, 1.0);

  ASSERT_EQ(5U, points.size());
  EXPECT_DOUBLE_EQ(2.0, points[0].x);
  EXPECT_DOUBLE_EQ(-3.0, points[0].y);
  EXPECT_DOUBLE_EQ(1.6, points[0].z);
  EXPECT_DOUBLE_EQ(3.0, points[1].x);
  EXPECT_DOUBLE_EQ(-3.0, points[1].y);
  EXPECT_DOUBLE_EQ(3.0, points[2].x);
  EXPECT_DOUBLE_EQ(-2.0, points[2].y);
  EXPECT_DOUBLE_EQ(2.0, points[3].x);
  EXPECT_DOUBLE_EQ(-2.0, points[3].y);
  EXPECT_DOUBLE_EQ(points[0].x, points[4].x);
  EXPECT_DOUBLE_EQ(points[0].y, points[4].y);
  EXPECT_DOUBLE_EQ(points[0].z, points[4].z);
}

}  // namespace
}  // namespace mission_control

int main(int argc, char** argv) {
  testing::InitGoogleTest(&argc, argv);
  return RUN_ALL_TESTS();
}
