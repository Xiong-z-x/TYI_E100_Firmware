# Mission Coordinate Navigation And Auto-Land r6 Design

## Evidence

Flight `mission_20260724_110315.csv` completed takeoff and all four legs. The
actual leg directions differed from their commanded directions by only
`0.28°`, `0.68°`, `-1.62°`, and `-0.73°`. The route itself used the captured
takeoff yaw `2.899 rad` (`166.1°`), so it was not aligned to a local ENU
cardinal axis.

PX4 log ID 8 contains repeated `Disarming denied: not landed`. During physical
ground contact, `vehicle_land_detected.has_low_throttle` remained false and
motor outputs stayed high. The current OFFBOARD landing held the exact origin
position against the floor. Kill reduced the motor outputs to zero; PX4 then
reported landed about 1.4 seconds later.

## Selected Design

1. Snap the captured takeoff yaw to the nearest `90°` local ENU axis. For this
   flight, `166.1°` becomes `180°`. Hold the takeoff position while the yaw
   aligns, then generate the square as absolute local coordinate waypoints.
2. Make `moveToPoint` a real go-to operation: publish the smooth coordinate
   trajectory, then hold the endpoint until position and speed stay inside the
   configured tolerance. A timeout is an ordinary task failure.
3. After returning near the origin at flight height, request PX4 `AUTO.LAND`
   and immediately stop OFFBOARD setpoints. PX4 Land mode descends at the
   current position, lowers thrust for its land detector, and uses the existing
   `COM_DISARM_LAND=2.0 s` auto-disarm.
4. Keep RC Kill and manual mode override unchanged. Do not add force-disarm,
   change motor mapping, tune land-detector thresholds, or alter FAST-LIO,
   MAVROS, or EKF.

## Rejected Alternatives

- Force-disarm after a companion-computer height check: rejected because a
  false ground estimate could stop motors in flight.
- Relax PX4 land-detector thresholds: rejected because it changes every
  landing, including the already successful manual-flight path.
- Keep body-yaw-relative waypoints and add a hard-coded angle: rejected because
  it reproduces the measured correction only for one startup orientation.

## Acceptance

- A yaw of `2.899 rad` produces a route yaw of `pi`.
- Every coordinate leg waits for endpoint convergence before advancing.
- Normal mission landing contains no ground-height OFFBOARD hold and no force
  disarm; it hands control to `AUTO.LAND`.
- Preflight requires `COM_DISARM_LAND=2.0`.
- All existing safety tests plus the focused new tests pass on ARM64.
- Deployment validation remains ground-only: disarmed, landed, no mission node
  and no setpoint publisher after the check.
