# Intel RealSense D435i ROS 接入与 MID360 图像投影方案

## 当前实现

本次在不改动 `flight-core` 主链启动面的前提下，把 D435i ROS 接入和 MID360->图像平面投影全部放进了独立容器 `TYI_VLN`。

当前 `TYI_VLN` 同时提供：

- HTTP 深度查询服务
- D435i ROS 话题发布
- MID360 点云投影到 D435i 图像平面的对应输出

## 关键输入配置

- D435i ROS 配置文件：`configs/realsense/d435i_ros.yaml`
- MID360 外参来源：`configs/fastlio2/mid360.yaml` 中 `fast_lio.extrinsics.baselink2lidar`
- D435i 外参来源：`configs/realsense/d435i_ros.yaml` 中 `base_to_camera_link`
- 当前现场假设：MID360 机体坐标中心与无人机中心重合，因此 `base_link -> livox_frame` 采用单位变换

说明：

- 代码和话题已接好
- 真实外参仍需后续标定后填写
- 在外参未填写前，投影结果仅用于联调，不代表真实几何对应

## D435i ROS 话题

- `/d435i/color/image_raw`
- `/d435i/aligned_depth_to_color/image_raw`
- `/d435i/color/camera_info`

## MID360 对应输出

## MID360 输入兼容性

- `TYI_VLN` 会自动识别 `/livox/lidar` 的消息类型
- 当前现场已验证兼容 `livox_ros_driver2/CustomMsg`
- 如后续切换为 `sensor_msgs/PointCloud2`，同一投影链路仍可继续复用

- `/d435i/livox/projected_depth`
  - `32FC1`，每个像素存储投影到图像平面的 MID360 最近深度（米）
- `/d435i/livox/overlay`
  - `rgb8`，把投影点画在当前 D435i 彩图上
- `/d435i/livox/correspondences`
  - `PointCloud2`，每个点包含 lidar 原始坐标、投影像素 `(u, v)`、相机坐标系位置、RealSense 深度值和深度误差

## TF

`TYI_VLN` 会发布以下静态 TF：

- `base_link -> d435i_link`
- `d435i_link -> d435i_color_optical_frame`
- `d435i_link -> d435i_depth_optical_frame`
- `base_link -> livox_frame`

其中：

- `base_link -> d435i_link` 读取 `configs/realsense/d435i_ros.yaml`
- `base_link -> livox_frame` 读取 `configs/fastlio2/mid360.yaml`
- `configs/realsense/d435i_ros.yaml` 中 `frame_chain.lidar_origin_coincident_with_vehicle_center=true` 用于显式声明“激光雷达中心 = 机体中心”

当前现场参数链优化说明：

- `fast_lio.extrinsics.baselink2lidar.t = [0, 0, 0]`
- `fast_lio.extrinsics.baselink2lidar.R = I`
- 因此 `base_link`、`livox_frame` 与“无人机中心”在几何上按同一点解释
- `TYI_VLN /v1/qwen-ground` 会同时返回 `relativeToBaseLinkM`、`relativeToLidarFrameM` 和 `relativeToVehicleCenterM`
- 如果后续 `baselink2lidar` 不再是单位变换，服务端会自动保留三者区别，并在响应中标出假设不一致告警

## 容器环境变量

`docker-compose.realsense.yml` 中新增：

- `UAV_CONFIG_DIR=/opt/uav/configs`
- `REALSENSE_ROS_ENABLE`
- `REALSENSE_ROS_CONFIG_PATH`
- `LIO_CONFIG_PATH`

并挂载：

- `./configs:/opt/uav/configs:ro`

## 验证建议

在 `TYI_VLN` 容器内执行：

```bash
source /opt/ros/noetic/setup.bash
rostopic list | grep d435i
rostopic echo -n 1 /d435i/color/camera_info
rostopic echo -n 1 /d435i/livox/projected_depth
```

## 后续只剩外参标定

当前代码层面已经把以下工作准备好：

1. D435i 进入 ROS master
2. D435i 彩图、深度图、相机内参发布
3. MID360 静态 TF 接入
4. MID360 点云投影到图像平面
5. 像素与点云对应结果输出

后续只需完成真实 `base_link->d435i_link` 与 `base_link->livox_frame` 外参标定并填入配置，即可得到可信的深度图/点云对应关系。
