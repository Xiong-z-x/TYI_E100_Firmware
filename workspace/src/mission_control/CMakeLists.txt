cmake_minimum_required(VERSION 3.0.2)
project(mission_control)

add_compile_options(-std=c++14 -Wall -Wextra -Wpedantic)

find_package(catkin REQUIRED COMPONENTS
  geometry_msgs
  mavros_msgs
  nav_msgs
  roscpp
  std_srvs
)

catkin_package(
  INCLUDE_DIRS include
  CATKIN_DEPENDS geometry_msgs mavros_msgs nav_msgs roscpp std_srvs
)

include_directories(
  include
  ${catkin_INCLUDE_DIRS}
)

add_library(mission_control_core
  src/flight_interface.cpp
  src/trajectory_generator.cpp
  src/mission_context.cpp
  src/subtasks.cpp
)
target_link_libraries(mission_control_core ${catkin_LIBRARIES})
add_dependencies(mission_control_core ${catkin_EXPORTED_TARGETS})

add_executable(mission_control_node src/control_node.cpp)
target_link_libraries(mission_control_node mission_control_core ${catkin_LIBRARIES})
add_dependencies(mission_control_node ${catkin_EXPORTED_TARGETS})

add_executable(mission_main src/main_mission_node.cpp)
target_link_libraries(mission_main mission_control_core ${catkin_LIBRARIES})
add_dependencies(mission_main ${catkin_EXPORTED_TARGETS})

install(TARGETS mission_control_core mission_control_node mission_main
  ARCHIVE DESTINATION ${CATKIN_PACKAGE_LIB_DESTINATION}
  LIBRARY DESTINATION ${CATKIN_PACKAGE_LIB_DESTINATION}
  RUNTIME DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
)

install(DIRECTORY include/${PROJECT_NAME}/
  DESTINATION ${CATKIN_PACKAGE_INCLUDE_DESTINATION}
)

install(DIRECTORY launch config
  DESTINATION ${CATKIN_PACKAGE_SHARE_DESTINATION}
)
