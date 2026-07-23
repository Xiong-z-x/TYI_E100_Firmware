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
