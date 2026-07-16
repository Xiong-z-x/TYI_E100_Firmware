# mission_control

`mission_control` is the UAV Nano mission layer. It keeps the existing board-facing package name and entry points, while exposing a small set of beginner-friendly flight actions built on MAVROS.

## Entry Points

- `mission_control_node`: optional service node. It provides `~boot` for OFFBOARD/arm and lock control.
- `mission_main`: task runner used by `scripts/mission`.

The board script runs inside the `flight-core` container:

```bash
cd /home/uav_nano/TYI_E100_Firmware
scripts/mission _mission:=status _auto_start:=true
```

`status` and `hold` do not arm the vehicle. Real flight requires `_auto_start:=true`:

```bash
scripts/mission _mission:=test_flight _auto_start:=true
```

## Structure

| Path | Purpose |
| --- | --- |
| `src/main_mission_node.cpp` | Parses `_mission` and `_auto_start`, then runs a task. |
| `src/control_node.cpp` | Optional boot/lock service node. |
| `src/subtasks.cpp` | Task definitions. `test_flight` is the reference flight task. |
| `src/mission_context.cpp` | High-level actions: boot, takeoff, move, hover, land, lock. |
| `src/flight_interface.cpp` | MAVROS topics and services. |
| `src/trajectory_generator.cpp` | Smooth point interpolation. |
| `config/mission_control.yaml` | Board-specific topics, rates, tolerances, and reference-flight parameters. |

## Board Parameters

The current UAV Nano stack publishes `/mavros/local_position/odom` at about 20 Hz, so `rate_hz` is kept at `20.0`. The setpoint stream publishes to `/mavros/setpoint_raw/local`, and odometry comes from MAVROS after the FAST-LIO to MAVROS bridge.

Key parameters:

- `prestream_sec`: how long to publish the current position before requesting OFFBOARD.
- `wait_ready_timeout_sec`: timeout for MAVROS connection and odometry readiness.
- `takeoff.*`: climb rate and target-reaching tolerances.
- `landing.*`: controlled descent, ground-contact detection, and disarm timing.
- `test_flight.*`: height, square size, motion duration, hover time, and landing duration for the reference flight.

## Tasks

| `_mission` | Arms | Behavior |
| --- | --- | --- |
| `idle` | No | Spins without flight action. |
| `status` | No | Waits for MAVROS/odom readiness and prints status. |
| `hold` | No | Holds current position for `hold_duration_sec`. |
| `test_flight` | Yes | Takeoff, fly a small square, hover between legs, land, disarm. |

Older task names `takeoff_move_land` and `takeoff_hover_land` are accepted as aliases for `test_flight` to avoid breaking existing scripts, but new development should use `test_flight`.

## Development Notes

- Keep task logic in `src/subtasks.cpp` and use `MissionContext` actions instead of calling MAVROS directly.
- Keep MAVROS topic/service changes in `config/mission_control.yaml` and `FlightInterface`.
- Start with `status`, then `hold`, then `test_flight` after confirming the flight area is clear.
- Mission samples are logged to `/tmp/mission_control_logs/mission_*.csv` inside the container.
