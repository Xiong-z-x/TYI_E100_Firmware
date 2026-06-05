# Changelog

## 0.1.4

- Updated the repository to the current `uav-nano` maintenance state with Nano-specific MID360 configuration and PX4 serial parameters
- Kept `flight-core`, `control-gateway`, and `pointcloud-gateway` as the default runtime set, while moving media and vision services behind optional Compose profiles
- Refined `high_rate_odom_ekf` for the active Nano stack, fusing FastLIO2 odometry with high-rate Livox IMU data without adding gravity-calibration or landed-state side effects
- Removed the EKF output rate cap so the fused odometry can follow the available IMU cadence and provide a higher-rate MAVROS vision-pose input
- Added a MAVROS status helper for controlled state checks during board validation
- Updated gateway and planner defaults to consume the optimized odometry stream and keep optional subsystems cleanly disabled until enabled by profile
- Cleaned generated Python cache files and stale local Docker image tags from the board workspace
- Validated on `uav-nano`: flight core, control gateway, and pointcloud gateway report healthy; MAVROS is connected; high-rate EKF output and MAVROS vision-pose forwarding are active

## 0.1.3

- Replaced the legacy DLIO runtime path with Fast-LIO2 as the active LiDAR odometry source for E100
- Forced the Livox MID360 driver onto the shared monotonic ROS timebase so NTP/system-time corrections after networking no longer jump sensor stamps
- Added `high_rate_odom_ekf`, fusing 10-20 Hz Fast-LIO2 odometry with 200 Hz Livox IMU data
- Switched `fastlio_to_mavros` to consume the fused high-rate odometry stream and forward MAVROS vision-pose data with high-rate input stamps
- Updated `fastlio_to_mavros` to support configurable queues/rates and callback-driven publishing so high-rate odometry is not collapsed by the main loop
- Added control gateway, pointcloud gateway, optional media/planner surfaces, calibration, and validation files used by the integrated E100 stack
- Added validation scripts for stack health and timebase checks
- Validated on board: shared monotonic clock remains active, fused odometry uses 5 ms stamps, MAVROS receives the high-rate vision pose stream, and the gateway stack reports healthy
- Captured the validated runtime as `tyi/tyi_e100:0.1.2-shared-monotonic-ekf-mavros200`

## 0.1.2

- Added dual deployment modes for the product repository: prebuilt image pull by default, local source build with `--build`
- Switched the default product image reference to the published ACR image `crpi-zpvbhgsm3t97idht.cn-hangzhou.personal.cr.aliyuncs.com/tyi-tech/tyi_e100:0.1.1`
- Added Compose override file `docker-compose.build.yml` for local source builds while keeping the default Compose path image-based
- Updated deployment and operation scripts to support both pull mode and build mode
- Expanded Chinese and English documentation to explain ACR login, prebuilt image usage, and the fallback source-build workflow
- Verified on `uav-nx` that the default image-pull path can authenticate to ACR and pull the published image successfully
- Published aligned ACR image tags `0.1.2`, `latest`, and `stable`

## 0.1.1

- Verified clean source build on the `uav-nx` production board with `docker compose build --no-cache base-stack`
- Normalized the ROS1 `livox_ros_driver2` manifest during image build so the workspace can be discovered reliably in fresh builds
- Bundled the required GeographicLib `egm96-5` geoid dataset into the firmware repository to remove the SourceForge runtime download dependency
- Added retry handling for base `apt` bootstrap during image build to reduce transient DNS or mirror failures
- Rebuilt and revalidated the runtime stack on board, including `livox_lidar_publisher2`, `fastlio2_odom`, `fastlio_to_mavros`, `mavros`, `/robot/fastlio2/odom`, and `/mavros/vision_pose/pose`

## 0.1.0

- Initial product firmware repository extracted from the production `uav-nx` base stack
- Source-visible Docker Compose bring-up for `livox_ros_driver2`, `fast_lio`, `fastlio_to_mavros`, `mavros`, and `mavlink`
- Single-machine configuration surface through `machine.env`
- Bilingual firmware documentation in `docs/en_US` and `docs/zh_CN`
- Removed internal smoke-test and development-only operation entrypoints from the product firmware package
