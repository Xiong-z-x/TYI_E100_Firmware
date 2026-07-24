cmake_minimum_required(VERSION 3.0.2)
project(mission_control)

add_compile_options(-std=c++14 -Wall -Wextra -Wpedantic)

find_package(catkin REQUIRED COMPONENTS
  geometry_msgs
  mavros_msgs
  nav_msgs
  roscpp
  rosgraph_msgs
  sensor_msgs
  std_srvs
  topic_tools
)

catkin_package(
  INCLUDE_DIRS include
  CATKIN_DEPENDS geometry_msgs mavros_msgs nav_msgs roscpp rosgraph_msgs sensor_msgs std_srvs topic_tools
)

include_directories(
  include
  ${catkin_INCLUDE_DIRS}
)

add_library(mission_control_core
  src/mission_policy.cpp
  src/preflight.cpp
  src/preflight_receipt.cpp
  src/safety_gate.cpp
  src/flight_interface.cpp
  src/trajectory_generator.cpp
  src/mission_context.cpp
  src/subtasks.cpp
)
target_link_libraries(mission_control_core ${catkin_LIBRARIES})
add_dependencies(mission_control_core ${catkin_EXPORTED_TARGETS})

add_executable(mission_main src/main_mission_node.cpp)
target_link_libraries(mission_main mission_control_core ${catkin_LIBRARIES})
add_dependencies(mission_main ${catkin_EXPORTED_TARGETS})

install(TARGETS mission_control_core mission_main
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

if(CATKIN_ENABLE_TESTING)
  find_package(PythonInterp REQUIRED)

  catkin_add_gtest(test_safety_gate
    test/test_safety_gate.cpp
    src/safety_gate.cpp
  )
  if(TARGET test_safety_gate)
    target_link_libraries(test_safety_gate ${catkin_LIBRARIES})
  endif()

  catkin_add_gtest(test_mission_policy
    test/test_mission_policy.cpp
    src/mission_policy.cpp
  )
  if(TARGET test_mission_policy)
    target_link_libraries(test_mission_policy ${catkin_LIBRARIES})
  endif()

  catkin_add_gtest(test_preflight_receipt
    test/test_preflight_receipt.cpp
    src/preflight_receipt.cpp
  )
  if(TARGET test_preflight_receipt)
    target_link_libraries(test_preflight_receipt ${catkin_LIBRARIES})
  endif()

  add_test(
    NAME test_mission_control_default_off
    COMMAND ${PYTHON_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/test/test_default_off.py
  )
endif()
