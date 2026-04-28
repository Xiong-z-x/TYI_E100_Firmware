cmake_minimum_required(VERSION 3.0.2)
project(tyi_planner_bridge)

add_compile_options(-std=c++14 -O2 -Wall)

find_package(catkin REQUIRED COMPONENTS
  bspline_opt
  geometry_msgs
  livox_ros_driver2
  mavros_msgs
  nav_msgs
  pcl_conversions
  pcl_ros
  quadrotor_msgs
  roscpp
  sensor_msgs
  std_msgs
  traj_utils
)
find_package(Eigen3 REQUIRED)
find_package(PCL REQUIRED)

catkin_package()

include_directories(
  SYSTEM
  ${catkin_INCLUDE_DIRS}
  ${EIGEN3_INCLUDE_DIRS}
  ${PCL_INCLUDE_DIRS}
)

add_executable(odom_relay src/odom_relay.cpp)
target_link_libraries(odom_relay ${catkin_LIBRARIES})

add_executable(livox_to_world_cloud src/livox_to_world_cloud.cpp)
target_link_libraries(livox_to_world_cloud ${catkin_LIBRARIES} ${PCL_LIBRARIES})

add_executable(position_command_to_mavros src/position_command_to_mavros.cpp)
target_link_libraries(position_command_to_mavros ${catkin_LIBRARIES})

add_executable(bspline_to_path_bridge src/bspline_to_path_bridge.cpp)
target_link_libraries(bspline_to_path_bridge ${catkin_LIBRARIES})
