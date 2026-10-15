cmake_minimum_required(VERSION 3.8)
project(mobile_robot_cartographer_bringup)

find_package(ament_cmake REQUIRED)
find_package(fastcdr REQUIRED)

add_library(fastcdr_compat SHARED
  src/fastcdr_compat.cc
)

target_compile_features(fastcdr_compat PUBLIC cxx_std_17)
target_link_libraries(fastcdr_compat PRIVATE fastcdr)

install(
  DIRECTORY launch config
  DESTINATION share/${PROJECT_NAME}
)

install(
  TARGETS fastcdr_compat
  DESTINATION lib
)

install(
  FILES README.md
  DESTINATION share/${PROJECT_NAME}
)

ament_package()
