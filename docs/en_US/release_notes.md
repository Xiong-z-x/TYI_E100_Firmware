# Release Notes

## 0.1.4

This release records the current `uav-nano` firmware state and the high-rate EKF fusion optimization now used by the active flight stack.

Main updates:

- applies the Nano-specific MID360 serial/IP configuration and PX4 high-speed serial settings
- keeps `flight-core`, `control-gateway`, and `pointcloud-gateway` as the default runtime services
- leaves media and vision services disabled by default, while preserving their Compose profiles and configuration surfaces for later expansion
- refines `high_rate_odom_ekf` to fuse FastLIO2 odometry with Livox IMU data for a higher-rate odometry stream
- removes the EKF output rate cap so the output can follow the available IMU cadence instead of being limited by a fixed throttle
- forwards the fused odometry stream to MAVROS as a high-rate vision-pose input
- updates gateway, planner, and validation defaults around the optimized odometry path
- cleans generated Python cache files and stale local Docker image tags from the board workspace

Validated runtime:

- `flight-core`, `control-gateway`, and `pointcloud-gateway` report healthy on `uav-nano`
- MAVROS is connected to PX4 over the configured high-speed serial link
- the EKF output is active at high rate and MAVROS receives the corresponding vision-pose stream
- optional media and vision services remain available through profiles but are not part of the default Nano bring-up

Operational conclusion:

- the repository can now be maintained from GitHub as the active Nano firmware baseline
- the LIO-to-MAVROS path is suitable for higher-rate vision odometry input to PX4
- optional camera/media/VLM functionality remains an extension surface instead of a default runtime dependency

## 0.1.3

This release confirms the current E100 field firmware after the LiDAR timebase and high-rate odometry update.

Main updates:

- moves the active LiDAR odometry path to Fast-LIO2
- forces Livox MID360 timestamps to the shared monotonic ROS timebase, preventing NTP/system-time corrections from breaking Fast-LIO after the device comes online
- adds `high_rate_odom_ekf` to fuse Fast-LIO2 odometry with Livox IMU data
- connects the fused high-rate odometry stream into `fastlio_to_mavros`
- publishes MAVROS vision pose data from the high-rate odometry stream
- updates `fastlio_to_mavros` with configurable queue sizes, configurable publish rate, wall-clock loop timing, and callback-driven publishing
- adds the current control gateway, pointcloud gateway, optional media/planner surfaces, calibration, and validation files used by the integrated E100 stack
- adds stack and timebase validation scripts

Validated runtime:

- fused odometry publishes with 5 ms input stamps
- MAVROS vision pose follows the high-rate odometry stream and is subscribed by MAVROS
- MAVROS remains connected and unarmed during validation
- control gateway, pointcloud gateway, and optional gateway/planner services report healthy when enabled
- validated runtime image: `tyi/tyi_e100:0.1.2-shared-monotonic-ekf-mavros200`

Operational conclusion:

- the current E100 firmware is confirmed for the shared-monotonic Fast-LIO2 + high-rate MAVROS odometry path
- networking/NTP should no longer trigger automatic timestamp-domain switching in the LiDAR/Fast-LIO path
- downstream APP, pointcloud, optional media/vision, and planner integration surfaces are included in this firmware repository state

## 0.1.2

This release adds dual deployment modes on top of `0.1.1`, and also publishes the aligned prebuilt image tag `0.1.2`.

Main updates:

- switches the default deployment path to the published ACR image
- keeps local source build support through `bash ./scripts/deploy.sh --build`
- adds `docker-compose.build.yml` as the Compose override for source builds
- updates deployment and operation scripts to support both pull mode and build mode
- expands the bilingual documentation for ACR login, prebuilt image usage, and source-build fallback
- verifies on `uav-nx` that the default image-pull path can authenticate to ACR and pull the published image successfully
- publishes aligned ACR tags `0.1.2`, `latest`, and `stable`

Operational conclusion:

- users can now prefer the prebuilt image for faster deployment
- users can still deploy from source with `--build` when local customization is required
- the default published runtime image is now `0.1.2`, aligned with the repository workflow version

## 0.1.1

This release closes the clean source-build workflow for the product firmware repository and has been validated on the `uav-nx` production board.

Main updates:

- supports `docker compose build --no-cache base-stack` directly from the checked-out source tree
- normalizes the ROS1 manifest for `livox_ros_driver2` during image build so fresh builds can discover the workspace package reliably
- bundles the required GeographicLib `egm96-5` geoid dataset with the repository instead of downloading it separately during build
- adds retry handling for the base `apt` dependency bootstrap path to reduce transient DNS or mirror failures
- validated runtime chain:
  `livox_lidar_publisher2`, `fastlio2_odom`, `fastlio_to_mavros`, `mavros`
- validated key topics:
  `/robot/fastlio2/odom`, `/mavros/vision_pose/pose`

Operational conclusion:

- users can now clone this repository and build the runtime directly with Docker Compose
- machine-specific differences should remain in `machine.env`
- control bridge capability continues to be delivered separately through `TYI_Plugin_Ctl`
