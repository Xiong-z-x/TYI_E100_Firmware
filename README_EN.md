<p align="center">
  <img src="assets/logo.png" alt="TYI Innovation" width="260">
</p>

# TYI E100 Firmware

ROS1 product firmware repository for `TYI E100`.
This repository keeps the runtime source visible and provides the build and runtime entrypoints through `docker compose`.
The current release supports both:

- running from a prebuilt image
- building and running directly from the checked-out source tree

Chinese homepage: [README.md](README.md)

Current version: [VERSION](VERSION)

## What This Repository Delivers

- source-visible deployment package for Ubuntu 20.04 + ROS1 Noetic
- single-machine configuration entry through [machine.env](machine.env)
- Docker-based bring-up for `livox_ros_driver2`, `fast_lio`, `high_rate_odom_ekf`, `fastlio_to_mavros`, `mavros`, `mavlink`, and `uav_base_bringup`
- default services for `flight-core`, `control-gateway`, and `pointcloud-gateway`, with media / vision / planner kept as optional Compose profile extensions
- product firmware deployment and operation scripts

## Runtime Pipeline

`Livox MID360 -> FastLIO2 + Livox IMU EKF -> fastlio_to_mavros -> MAVROS -> PX4`

The current Nano maintenance state includes the MID360 machine parameters, the high-speed PX4 serial link, and the high-rate EKF fusion update. The EKF node fuses lower-rate FastLIO2 odometry with high-rate Livox IMU data, providing MAVROS with a more continuous vision-pose input and improving the real-time path from LIO to the flight controller.

## Quick Start

```bash
git clone git@github.com:TYI-Tech/TYI_E100_Firmware.git
cd TYI_E100_Firmware
bash ./scripts/check_host.sh
vim machine.env
bash ./scripts/deploy.sh
```

For a local source build instead:

```bash
bash ./scripts/deploy.sh --build
```

To pull the prebuilt image manually:

```bash
docker pull crpi-zpvbhgsm3t97idht.cn-hangzhou.personal.cr.aliyuncs.com/tyi-tech/tyi_e100:0.1.2
```

Optional stable alias:

```bash
docker pull crpi-zpvbhgsm3t97idht.cn-hangzhou.personal.cr.aliyuncs.com/tyi-tech/tyi_e100:stable
```

After deployment:

```bash
bash ./scripts/status.sh
bash ./scripts/logs.sh
bash ./scripts/enter.sh
```

## Shortest Path

For a first-time bring-up with the fewest decisions, use this order:

1. clone the repository and run `bash ./scripts/check_host.sh`
2. edit only [machine.env](machine.env)
3. run `bash ./scripts/deploy.sh`
4. confirm the runtime with `bash ./scripts/status.sh` and `bash ./scripts/logs.sh`

Notes:

- `deploy.sh` pulls the prebuilt ACR image and starts it by default
- `deploy.sh` builds the default gateway images locally and does not depend on hidden external source packages
- `deploy.sh --build` switches to local source-build mode
- the build now includes the required GeographicLib geoid locally and retries the base `apt` bootstrap path
- machine-specific differences are expected to stay within `machine.env`
- `media-gateway` and `vision-gateway` are disabled by default and should be enabled only through their Compose profiles when the hardware/feature is present

## Common Configuration Entry Points

- [machine.env](machine.env)
  airframe UART, MID360 serial/IP, and host NIC settings
- [configs/fastlio_to_mavros/bridge.yaml](configs/fastlio_to_mavros/bridge.yaml)
  bridge topic and frame settings when downstream control integration is needed
- [configs/fastlio2](configs/fastlio2)
  FastLIO2 runtime parameters
- [workspace/src/uav_base_bringup/scripts/high_rate_odom_ekf.py](workspace/src/uav_base_bringup/scripts/high_rate_odom_ekf.py)
  high-rate fusion node for FastLIO2 odometry and Livox IMU data
- [configs/mavros](configs/mavros)
  MAVROS plugin and FCU parameters

## Start Here

- [Chinese quick start](docs/zh_CN/%E5%BF%AB%E9%80%9F%E4%B8%8A%E6%89%8B.md)
- [Chinese documentation](docs/zh_CN/README.md)
- [Chinese release notes](docs/zh_CN/%E7%89%88%E6%9C%AC%E8%AF%B4%E6%98%8E.md)
- [Documentation index](docs/README.md)
- [English quick start](docs/en_US/quick_start.md)
- [English documentation](docs/en_US/README.md)
- [English release notes](docs/en_US/release_notes.md)
- [Changelog](CHANGELOG.md)

## Repository Layout

- `configs/`
  runtime configuration mounted into the container
- `docker/`
  Docker build and runtime entrypoint files
- `scripts/`
  firmware operation entrypoints
- `third_party/`
  vendored third-party build dependencies
- `workspace/src/`
  ROS source packages used by the runtime

## Optional Bridge Package

If the project later needs control bridge capabilities, install `TYI_Plugin_Ctl` separately:

```bash
sudo apt install tyi-plugin-ctl
```
