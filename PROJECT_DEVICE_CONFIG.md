# UAV 051 Device Configuration

Last verified: 2026-07-23

## Identity

- PX4 firmware: `1.16.2`
- PX4 board: MicoAir743v2
- MAVLink system/component: `51/1`
- Flight-controller UID: `3761439192332449336`
- Nano WLAN MAC: `70:A6:CC:0D:8F:5E`
- Nano WLAN IPv4 at verification: `192.168.0.108` (DHCP; rediscover before use)

## Flight-controller link

- Nano FCU device: `/dev/ttyTHS0:921600`
- MAVROS target system/component: `51/1`
- The onboard Nano-to-PX4 link does not depend on Windows USB.

## QGroundControl isolation

- Windows WLAN IPv4 at verification: `192.168.0.126`
- Nano GCS URL:
  `udp://0.0.0.0:14555@192.168.0.126:14551`
- QGC automatic UDP listener: `14551`
- QGC default listener `14550`: disabled by moving the automatic listen port
- Windows QGC setting:

```ini
[AutoConnect]
autoConnectUDP=true
udpListenPort=14551
```

Eight-second bidirectional capture after configuration:

- Nano to QGC: 2645 UDP packets
- QGC to Nano: 16 UDP packets
- Live direct receive test: only source `192.168.0.108:14555`, only MAVLink
  system ID `51`

If the Windows WLAN IPv4 changes, update `GCS_URL` before relying on QGC.
Do not change Windows routes, static IP, DNS, proxy, or firewall as part of this
device configuration.

## MID360

- Serial number: `147MDM5T0020051`
- Nano Ethernet: `192.168.2.50/24`
- MID360 IPv4: `192.168.2.205`
- Final ping: 3/3 replies, 0% loss, average 1.289 ms

## Dynamic coordinate-direction verification

The test was performed disarmed with RC channel 7 Kill high. All propellers were
required to be removed. The vehicle was moved manually while keeping its heading.

### Forward

| Output | Body-forward | Lateral crosstalk | Vertical change |
|---|---:|---:|---:|
| PX4 local position | +0.362 m | 0.005 m | +0.000 m |
| MAVROS vision pose | +0.367 m | 0.019 m | +0.024 m |
| FAST-LIO odometry | +0.350 m | 0.006 m | -0.004 m |

### Right

| Output | Body-right | Fore-aft crosstalk | Vertical change |
|---|---:|---:|---:|
| PX4 local position | +0.338 m | 0.006 m | +0.003 m |
| MAVROS vision pose | +0.332 m | 0.005 m | -0.025 m |
| FAST-LIO odometry | +0.338 m | 0.011 m | +0.004 m |

### Up

| Output | Vertical-up | Horizontal change |
|---|---:|---:|
| PX4 local position | +0.289 m | 0.072 m |
| MAVROS vision pose | +0.296 m | 0.067 m |
| FAST-LIO odometry | +0.284 m | 0.068 m |

The forward, right, and up signs are consistent through
MID360/FAST-LIO, vision pose, MAVROS, and PX4 local position.

After returning near the initial point, all three outputs agreed that the
physical placement was about 0.15 m from the recorded start. During the next
approximately 14.8 seconds of rest:

- FAST-LIO displacement: about 4.4 mm
- PX4 local-position displacement: about 8.2 mm
- Vision-pose displacement: about 18 mm

No sustained one-direction divergence was observed in this ground test.

## Final runtime gate

- Containers: `flight-core`, `control-gateway`, and `pointcloud-gateway` healthy
- Livox LiDAR: approximately 20.27 Hz
- Livox IMU: approximately 199.84 Hz
- FAST-LIO odometry: approximately 20.27 Hz
- PX4 local odometry: approximately 20.03 Hz
- MAVROS: connected, disarmed, manual input present, mode `POSCTL`
- RC: 18 channels; channel 7 Kill high at about 1994
- PX4 estimator: attitude, horizontal/vertical velocity, relative horizontal
  position, absolute vertical position, and predicted relative horizontal
  position valid
- No publisher on `/mavros/setpoint_raw/local`
- No publisher on `/mavros/setpoint_position/local`
- No mission, offboard, or takeoff node running

`const_pos_mode_status_flag=true` while the vehicle is stationary is not by
itself a loss-of-aiding verdict on PX4 1.16.2; the other estimator flags and
dynamic response must be considered.

## Restart behavior observed

Recreating `flight-core` directly also caused `control-gateway` and
`pointcloud-gateway` processes to exit and restart under their own restart
policies when ROS master disappeared. Always revalidate all three containers
and topic rates after restarting `flight-core`.

## Rollback

The pre-change Nano environment backup is:

```text
/home/tfboys_nano/tyi-backups/qgc-isolation-20260723-1818/.env.before
```

The pre-change QGC settings backup is:

```text
C:\nano\backups\qgc-isolation-20260723-1818\QGroundControl.ini.before
```

Rollback also requires restoring PX4 `MAV_SYS_ID=1` before restarting PX4 and
restoring MAVROS target system `1`. Do not partially roll back only one side of
the MAVLink identity pair.

## Flight-safety boundary

This ground validation proves link isolation and coordinate-direction
consistency. It does not by itself authorize takeoff. Before fitting propellers
or attempting flight, separately verify motor order/direction, RC/failsafe,
battery behavior, arming checks with Kill deliberately released in a safe
propeller-off state, and position stability under actual motor vibration.

## Guarded mission module deployed on 2026-07-24

- Mission source fix: `ccae8eab813f13055ff53f6301e5ca7884a74e66`
- Final rate-aware deployment:
  `d01bf4afa3e2d90c190e2896d298274557d7502a`
- Git branch/tag: `feature/mission-control-safe` /
  `mission-safe-uav051-20260724-r4`
- Manual-flight rollback baseline:
  `53de4c7a4d13ca66af4e6560be57fdff1359a8f7`
- Runtime image: `tyi/tyi_e100:0.1.2-mission-safe-r4`
- Runtime image ID: `sha256:d92019fc2661...`
- Base image preserved:
  `tyi/tyi_e100:0.1.2-shared-monotonic-ekf-mavros200`
- Runtime environment backup:
  `backups/mission-control-20260724/env.before-mission-safe`

The module is default-off. Container startup creates no mission node and no
setpoint publisher. The only host entry point is:

```bash
./scripts/mission check
./scripts/mission test_flight --confirm-uav-051
```

The first command requires Kill engaged and Arm low. It checks the exact FCU
UID/SYSID/COMPID, POSCTL/disarmed/landed state, RC and battery, estimator
flags, PX4 failsafe parameters, LiDAR/IMU/FAST-LIO/vision/local-odom rates,
stationary odometry, monotonic clock, and competing setpoint publishers.

The deployed Kill-stage check returned:

```text
PRECHECK PASS - SAFE TO RELEASE KILL
```

Its mode-0600 one-use receipt was removed after deployment validation, so a
new check is mandatory immediately before any real test flight.

The final `r4` image passed the full Kill-stage preflight three consecutive
times. Each run returned zero and printed the pass line above.

At the time of the `r4` deployment, the real mission had not been executed. It
was fixed to a 1.2 m relative
takeoff, 3 s hover, 1 m closed square referenced to the takeoff origin, another
3 s hover, controlled descent, landed confirmation, and normal disarm.

Kill or any manual exit from OFFBOARD stops the mission and setpoint stream.
FCU/MAVROS/position loss stops setpoints and yields to PX4 failsafe. Only an
ordinary task failure with reliable control initiates controlled landing, with
PX4 `AUTO.LAND` as fallback. There is no forced-disarm command.

Post-deploy evidence:

- all three containers healthy;
- LiDAR about 20.2 Hz, Livox IMU about 202 Hz, FAST-LIO about 20.2 Hz;
- vision pose about 200 Hz and PX4 local odometry about 20.2 Hz;
- no mission node and no publisher on `/mavros/setpoint_raw/local`;
- MAVROS connected, vehicle disarmed, manual input present, mode `POSCTL`;
- 26 ARM64 policy tests and 3 default-off static tests passed.

The final preflight tuning uses rate-specific freshness limits: `1.5 s` for
the measured 1 Hz MAVROS state/estimator topics, `3.0 s` for the measured
0.5 Hz battery topic, and the stricter `0.5 s` limit for RC, position, and
clock data. The snapshot waits up to 3 seconds for a post-service dynamic
refresh. The final static test count is 5.

## First program-flight diagnosis and r5 ground deployment on 2026-07-24

The first real `test_flight` using `r4` took off to about 1.2 m, completed the
initial hover, started the first square leg, then PX4 entered `AUTO.LAND`.
The aircraft landed and disarmed normally.

Three independent records identify the same sequence:

- mission CSV:
  `/home/tfboys_nano/TYI_E100_Firmware/logs/mission-control/mission_20260724_102446.csv`;
- ROS log:
  `/home/tfboys_nano/TYI_E100_Firmware/logs/ros/2b1ec34a-1dd2-11b2-8e6d-4449c01d7f90/rosout.log`;
- PX4 ULog:
  `/home/tfboys_nano/TYI_E100_Firmware/logs/flight-monitor/px4-log-id7.ulg`,
  SHA-256
  `820189C4F7D8DB8BA0D6568A3D29662E4A1020C868C04A9F47658C84ED5AFC08`.

The ULog shows:

- `manual_control_signal_lost` changed from `0` to `1` about 12.562 s after
  arming and returned to `0` about 0.119 s later;
- the surrounding SBUS `input_rc` samples contain an approximately 0.708 s
  gap;
- the configured `COM_RC_LOSS_T=0.5` therefore activated PX4 RC-loss failsafe;
- `NAV_RCL_ACT=3` selected landing;
- local-position validity remained true and dead reckoning remained false;
- `offboard_control_signal_lost` occurred only after the mission yielded and
  stopped setpoints.

Therefore this event was not caused by FAST-LIO position invalidity, MAVROS
setpoint loss, or the square state machine. The independent earlier rosbag also
contained repeated RC gaps, including a maximum gap of about 0.980 s.

The observed rearward motion was a coordinate-frame issue, not an unintended
extra command. `r4` defined the first square leg as map-frame ENU `+X`, while
the captured initial yaw was about 2.929 rad, so map `+X` was nearly behind the
aircraft.

The `r5` change is deliberately limited to:

- `COM_RC_LOSS_T`: `0.5 -> 1.0` seconds;
- keep `NAV_RCL_ACT=3` (`Land`);
- keep `COM_RCL_EXCEPT=0`, so OFFBOARD is not exempt from RC-loss protection;
- rotate the existing square offsets by the captured initial yaw, making the
  first leg body-forward while preserving the closed 1 m square;
- no change to Kill priority, manual POSCTL takeover, normal-failure landing,
  communication-loss behavior, motor mapping, FAST-LIO, MAVROS, or EKF
  configuration.

Deployment and rollback identifiers:

- source commit: `fbcaaa71a73c7a4ea71650abb2af6a9763ef7c78`;
- runtime image: `tyi/tyi_e100:0.1.2-mission-safe-r5`;
- runtime image ID:
  `sha256:a9060308db6cbfbefaf76d46efdb35af4714307fe50b404de3f6cf1a669e5bf5`;
- Nano pre-change tag: `pre-mission-safe-r5-20260724`;
- previous runtime image retained:
  `tyi/tyi_e100:0.1.2-mission-safe-r4`.

Verification completed without arming or entering OFFBOARD:

- test-first RED: the old three-argument square API rejected the new
  yaw-aligned test;
- GREEN: 30 ARM64 mission-control tests, zero failures;
- full mission node and library compilation succeeded;
- image build succeeded from
  `tyi/tyi_e100:0.1.2-shared-monotonic-ekf-mavros200`;
- PX4 readback:
  `COM_RC_LOSS_T=1.0`, `NAV_RCL_ACT=3`, `COM_RCL_EXCEPT=0`;
- `./scripts/mission check` returned
  `PRECHECK PASS - SAFE TO RELEASE KILL`;
- after the check, the vehicle remained disarmed, landed, in `POSCTL`, with
  CH7 Kill high;
- no `mission_main` node and no publisher on
  `/mavros/setpoint_raw/local`;
- the generated one-use preflight receipt was deleted.

This validates the software build and ground integration only. The new
RC-gap tolerance and body-heading square still require a controlled flight
test before they can be considered flight-verified.

## Successful r5 flight diagnosis and r6 ground deployment on 2026-07-24

The second real `test_flight` completed takeoff, the closed square, return,
hover, and physical touchdown. Evidence was copied to:

```text
C:\nano\flight-analysis\20260724-110315-r5\
```

The primary records are:

- mission CSV `mission_20260724_110315.csv`, SHA-256
  `1F78EAAFFEDE5C797C809E2D506D9141A1A82A9FF1F0BBB3308BEEFE94E4F559`;
- ROS log `rosout.log`;
- PX4 ULog `px4-log-id8.ulg`, SHA-256
  `2B446BB2C7DBE88A33850103706BE3D297174E8B94B9C6DD203E32A35E23D82D`.

### Root cause: route angle

The r5 route used the measured takeoff yaw directly. The recorded yaw was
`2.899 rad` (`166.1 deg`), so the first 1 m leg was intentionally generated
as approximately `(-0.97, +0.21)` in local ENU coordinates. The controller
tracked all four commanded legs accurately: the measured direction error per
leg was approximately `+0.28`, `+0.68`, `-1.62`, and `-0.73 deg`. The visible
route-angle error was therefore in route-frame selection, not waypoint
tracking or FAST-LIO drift.

r6 snaps the captured yaw to the nearest local coordinate axis in 90 degree
increments. For this flight, `166.1 deg` becomes `180.0 deg`, a `+13.9 deg`
correction. The post-takeoff hover commands the same snapped yaw, and every
subsequent waypoint is an absolute `(x, y, z)` target generated on those
orthogonal axes.

Each go-to now has a second coordinate-convergence stage after the 3 s
trajectory ramp. A waypoint is accepted only after remaining within:

```text
horizontal position: 0.25 m
vertical position:   0.15 m
3D speed:            0.35 m/s
stable time:         0.50 s
extra timeout:       5.00 s
```

Failure to converge is an ordinary task failure and enters the existing
controlled-landing path while control remains reliable.

### Root cause: motors continued after touchdown

PX4 ULog ID 8 repeatedly reports:

```text
[commander] Disarming denied: not landed
```

During the physical ground-contact interval before the pilot engaged Kill,
PX4 continued to report `has_low_throttle=false`, `ground_contact=false`, and
`landed=false`. The r5 mission was still in OFFBOARD and held the exact
takeoff-origin ground-height setpoint, so the position controller continued
producing nonzero motor outputs while the landing gear was already touching
the floor. PX4 correctly rejected every normal disarm request because its land
detector had not declared landing. Kill then reduced all motor outputs to zero,
after which PX4 disarmed.

r6 removes the OFFBOARD ground-height hold and repeated disarm requests.
After returning within the start-point tolerance and completing the final
hover, the mission requests PX4 `AUTO.LAND`, marks the mission inactive, and
immediately shuts down its setpoint publisher. PX4 therefore owns descent,
touchdown detection, and its existing `COM_DISARM_LAND=2.0 s` automatic
disarm. The mission only observes `landed` and `disarmed`; it does not issue a
force-disarm command. Kill and any pilot mode takeover retain higher priority.

### r6 implementation and ground evidence

- design commit:
  `213e3a918f170f948a98d33d90313cbc126b01a8`;
- source commit:
  `998ea5389ac2e82a5de17579a18562907c99807f`;
- deployment-selection commit:
  `e98bb332b6995357d8a0a39cc1e7abd5faaa56de`;
- runtime image:
  `tyi/tyi_e100:0.1.2-mission-safe-r6`;
- runtime image ID:
  `sha256:90dc41e246cbe5cea6b27e3b5e18d15abf893333732b61bc62cbd1fc3ed200e6`;
- preserved rollback image and tag:
  `tyi/tyi_e100:0.1.2-mission-safe-r5` /
  `mission-safe-uav051-20260724-r5`.

Verification completed without arming or entering OFFBOARD:

- test-first RED reproduced all three missing r6 behaviors;
- 32 ARM64 mission-control tests passed with zero failures;
- the complete mission library and `mission_main` compiled successfully;
- all three runtime containers became healthy after replacing only
  `flight-core`;
- LiDAR approximately `20.9 Hz`, Livox IMU `212.2 Hz`, FAST-LIO `20.3 Hz`,
  MAVROS vision pose `293.0 Hz`, and PX4 local odometry `20.5 Hz`;
- PX4 readback:
  `COM_DISARM_LAND=2.0`, `COM_RC_LOSS_T=1.0`, `NAV_RCL_ACT=3`,
  `COM_RCL_EXCEPT=0`, `COM_RC_OVERRIDE=3`, `RC_MAP_ARM_SW=6`,
  `RC_MAP_KILL_SW=7`, and `RC_MAP_FLTMODE=5`;
- the vehicle remained connected, disarmed, and on ground;
- no mission node, no mission setpoint publisher, and no preflight receipt
  were present.

The transmitter was off during final deployment (`manual_input=false`), so the
Kill-stage `./scripts/mission check` was deliberately not run. r6 is compiled,
deployed, and ground-integrated, but its new coordinate alignment and
`AUTO.LAND` behavior still require one controlled flight verification.

### Final parameter-type audit: r6.1

The completion audit found that `COM_DISARM_LAND` is a MAVROS real-valued
parameter. The first r6 safety gate required the correct value `2.0`, but the
parameter type policy would have read the integer field (`0`) and caused a
false preflight rejection. This did not affect the deployed flight behavior,
but it would have blocked the next `./scripts/mission check`.

Commit `5bb0f00d1ff5bd5048785e35c0785c524ac71053` adds
`COM_DISARM_LAND` to the real-parameter policy and adds a regression test.
The immutable final image is:

```text
tyi/tyi_e100:0.1.2-mission-safe-r6.1
sha256:de5faae0e3a7e1025f195fbea05a46b1ce6063a8f7be6654b9e14f637410503c
```

The ARM64 package was rebuilt and again passed all 32 Catkin test targets with
zero errors or failures; the updated Python safety suite passed all 8 checks.
After deployment, all three containers were healthy, the vehicle remained
connected/disarmed/on-ground, `COM_DISARM_LAND` read back through MAVROS as
`real: 2.0`, all LiDAR/FAST-LIO/MAVROS data paths remained live, and there was
still no mission node, setpoint publisher, or preflight receipt. The r6 and r5
images/tags remain available for rollback.

## USB camera baseline on 2026-07-24

The camera connected to the Orin Nano is a standards-compliant USB Video Class
device:

```text
USB VID:PID:       05a3:9230
USB description:   ARC International / USB 2.0 Camera: HD USB Camera
Kernel driver:     uvcvideo
USB topology:      USB 2.0 high-speed, 480 Mbit/s
Capture node:      /dev/video0
Metadata node:     /dev/video1
Current USB port:  platform-3610000.xhci-usb-0:3.3:1.0
```

The device exposes no unique serial number and only reports generic vendor and
product strings, so the exact retail SKU or image sensor cannot be proven from
USB enumeration alone. The observed capabilities are:

```text
MJPEG: 1920x1080@30, 1280x720@60, 1024x768@30,
       1280x1024@30, 800x600@60, 640x480@120
YUYV:  1920x1080@6, 1280x720@9, 800x600@20, 640x480@30
```

The camera exposes automatic white balance and auto/manual exposure controls,
but no V4L2 autofocus control. Lens focus and useful QR working distance must
therefore be verified physically.

### Existing single-owner stream

`/dev/video0` is already owned by:

```text
systemd unit:  orin-camera-stream.service
process:       /usr/bin/python3 /opt/orin-ground-sender/orin_camera_stream.py
capture:       1280x720 MJPEG at 60 fps
HTTP output:   15 fps
health:        http://127.0.0.1:8090/healthz
snapshot:      http://127.0.0.1:8090/snapshot.jpg
stream:        http://127.0.0.1:8090/stream.mjpg
```

The service is enabled, active, has zero restarts, and uses approximately
24 MB RAM. A four-second independent LAN client received 4,132,583 bytes
without interrupting the service, proving that downstream consumers can share
the existing MJPEG server without reopening the V4L2 device.

The selected perception boundary is therefore:

1. Keep `orin-camera-stream.service` as the only `/dev/video0` owner.
2. Make the future YOLO/QR process consume
   `http://127.0.0.1:8090/stream.mjpg`.
3. Decode/infer at the rate required by the model rather than duplicating
   camera capture.
4. Publish only detection results to ROS/the ground-station interface unless a
   debug image stream is explicitly required.

This avoids a V4L2 ownership conflict with the teammate's RK3588 stream and
avoids a second MJPEG encoding stage because the camera already outputs JPEG.

The repository variable `MEDIA_CAMERA_PREFERRED_DEVICE` still references the
obsolete path `platform-3610000.xhci-usb-0:1:1.3-video-index0`; it is not used
by the active systemd camera service and was deliberately left unchanged.
Future direct-camera fallback should use the verified `index0` path above or a
dedicated udev alias, never bare `/dev/video0`.

A decoded evidence frame was saved as:

```text
C:\nano\camera-analysis\camera_snapshot_20260724.jpg
SHA-256 FAB3D44397D828A83531C17EBE849E7A2D128DB5653B75DC991B2EE5B77A303D
```

It is valid `1280x720` RGB JPEG, but the measured mean luma is only `4.97/255`;
the current view is almost completely black. Before selecting QR size,
detection distance, lens field of view, or a YOLO input resolution, point the
camera at a normally lit scene and capture a new frame.
