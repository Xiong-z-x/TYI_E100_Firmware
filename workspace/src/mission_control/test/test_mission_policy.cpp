#include <cmath>

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
      MissionPolicy::squareWaypoints(origin, 1.2, 1.0, 0.0);

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

TEST(MissionPolicyTest, AlignsSquareFirstLegWithInitialYaw) {
  const double half_pi = std::acos(-1.0) / 2.0;
  const LocalPoint origin{2.0, -3.0, 0.4};
  const std::vector<LocalPoint> points =
      MissionPolicy::squareWaypoints(origin, 1.2, 1.0, half_pi);

  ASSERT_EQ(5U, points.size());
  EXPECT_NEAR(2.0, points[1].x, 1e-12);
  EXPECT_NEAR(-2.0, points[1].y, 1e-12);
  EXPECT_NEAR(1.0, points[2].x, 1e-12);
  EXPECT_NEAR(-2.0, points[2].y, 1e-12);
  EXPECT_NEAR(1.0, points[3].x, 1e-12);
  EXPECT_NEAR(-3.0, points[3].y, 1e-12);
  EXPECT_DOUBLE_EQ(1.6, points[1].z);
  EXPECT_DOUBLE_EQ(points[0].x, points[4].x);
  EXPECT_DOUBLE_EQ(points[0].y, points[4].y);
  EXPECT_DOUBLE_EQ(points[0].z, points[4].z);
}

TEST(MissionPolicyTest, SnapsMeasuredYawToNearestLocalCoordinateAxis) {
  const double pi = std::acos(-1.0);

  EXPECT_NEAR(pi, MissionPolicy::nearestCardinalYaw(2.899), 1e-12);
  EXPECT_NEAR(-pi / 2.0,
              MissionPolicy::nearestCardinalYaw(-1.328), 1e-12);
}

}  // namespace
}  // namespace mission_control

int main(int argc, char** argv) {
  testing::InitGoogleTest(&argc, argv);
  return RUN_ALL_TESTS();
}
