from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from ament_index_python.packages import get_package_share_directory
from launch_ros.actions import Node

import os
from pathlib import Path


def _default_map_yaml(pkg_share: str) -> str:
    # Keep the installed package self-contained and aligned with LR-01's
    # approved 1813lab map. Other maps must be selected explicitly.
    return str(Path(pkg_share) / "maps" / "1813lab.yaml")


def _package_file(package_name: str, *relative_path: str) -> str:
    return os.path.join(get_package_share_directory(package_name), *relative_path)


def _car_node(executable_name: str, params_file: LaunchConfiguration) -> Node:
    return Node(
        package="car_ctrl",
        executable=executable_name,
        output="screen",
        emulate_tty=True,
        parameters=[params_file],
    )


def generate_launch_description():
    pkg_share = get_package_share_directory("mobile_robot_nav_bringup")
    car_params_default = _package_file("car_ctrl", "config", "ddsm_hat_diff_drive.yaml")
    lidar_params_default = _package_file("lslidar_driver", "params", "lsx10_1.yaml")
    lidar_2_params_default = _package_file("lslidar_driver", "params", "lsx10_2.yaml")
    lidar_rviz_default = _package_file("lslidar_driver", "rviz", "lslidar.rviz")

    map_yaml = LaunchConfiguration("map")
    nav2_params_file = LaunchConfiguration("nav2_params_file")
    car_params_file = LaunchConfiguration("car_params_file")
    lidar_params_file = LaunchConfiguration("lidar_params_file")
    lidar_2_params_file = LaunchConfiguration("lidar_2_params_file")
    laser_merger_target_frame = LaunchConfiguration("laser_merger_target_frame")
    use_sim_time = LaunchConfiguration("use_sim_time")
    autostart = LaunchConfiguration("autostart")
    navigation_autostart = LaunchConfiguration("navigation_autostart")
    startup_navigation_on_initial_pose = LaunchConfiguration("startup_navigation_on_initial_pose")
    use_composition = LaunchConfiguration("use_composition")
    use_respawn = LaunchConfiguration("use_respawn")
    use_nav_rviz = LaunchConfiguration("use_nav_rviz")
    use_lidar_rviz = LaunchConfiguration("use_lidar_rviz")
    start_imu_driver = LaunchConfiguration("start_imu_driver")
    nav_rviz_config = LaunchConfiguration("nav_rviz_config")
    lidar_rviz_config = LaunchConfiguration("lidar_rviz_config")
    log_level = LaunchConfiguration("log_level")

    navigation_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(pkg_share, "launch", "navigation.launch.py")
        ),
        launch_arguments={
            "map": map_yaml,
            "params_file": nav2_params_file,
            "use_sim_time": use_sim_time,
            "autostart": autostart,
            "navigation_autostart": navigation_autostart,
            "startup_navigation_on_initial_pose": startup_navigation_on_initial_pose,
            "use_composition": use_composition,
            "use_respawn": use_respawn,
            "use_rviz": use_nav_rviz,
            "rviz_config": nav_rviz_config,
            "log_level": log_level,
        }.items(),
    )

    lidar_rviz = ExecuteProcess(
        cmd=[
            "rviz2",
            "-d",
            lidar_rviz_config,
        ],
        output="screen",
        emulate_tty=True,
        condition=IfCondition(use_lidar_rviz),
    )

    # ENU: right turn around +Z is negative yaw.
    laser_static_tf = Node(
        package="tf2_ros",
        executable="static_transform_publisher",
        name="base_to_front_laser_tf",
        output="screen",
        arguments=[
            "--x",
            "0.235",
            "--y",
            "0.282",
            "--z",
            "0.0",
            "--roll",
            "0.0",
            "--pitch",
            "0.0",
            "--yaw",
            "0.78539816339",
            "--frame-id",
            "base_link",
            "--child-frame-id",
            "laser_link",
        ],
    )

    back_laser_static_tf = Node(
        package="tf2_ros",
        executable="static_transform_publisher",
        name="base_to_back_laser_tf",
        output="screen",
        arguments=[
            "--x",
            "-0.235",
            "--y",
            "-0.282",
            "--z",
            "0.0",
            "--roll",
            "0.0",
            "--pitch",
            "0.0",
            "--yaw",
            "-2.3561944901",
            "--frame-id",
            "base_link",
            "--child-frame-id",
            "laser_link_b",
        ],
    )

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "map",
                default_value=_default_map_yaml(pkg_share),
                description="Full path to the map YAML file.",
            ),
            DeclareLaunchArgument(
                "nav2_params_file",
                default_value=os.path.join(pkg_share, "config", "nav2_params.yaml"),
                description="Full path to the Nav2 parameters file.",
            ),
            DeclareLaunchArgument(
                "car_params_file",
                default_value=car_params_default,
                description="Full path to the car controller and IMU parameters file.",
            ),
            DeclareLaunchArgument(
                "lidar_params_file",
                default_value=lidar_params_default,
                description="Full path to the front lidar driver parameters file.",
            ),
            DeclareLaunchArgument(
                "lidar_2_params_file",
                default_value=lidar_2_params_default,
                description="Full path to the rear lidar driver parameters file.",
            ),
            DeclareLaunchArgument(
                "laser_merger_target_frame",
                default_value="base_link",
                description="Target frame for the dual laser merger output.",
            ),
            DeclareLaunchArgument(
                "use_sim_time",
                default_value="false",
                description="Use simulation clock if true.",
            ),
            DeclareLaunchArgument(
                "autostart",
                default_value="true",
                description="Automatically transition localization lifecycle nodes.",
            ),
            DeclareLaunchArgument(
                "navigation_autostart",
                default_value="false",
                description="Automatically transition navigation lifecycle nodes.",
            ),
            DeclareLaunchArgument(
                "startup_navigation_on_initial_pose",
                default_value="true",
                description="Start navigation lifecycle after an initial pose is received.",
            ),
            DeclareLaunchArgument(
                "use_composition",
                default_value="False",
                description="Run Nav2 nodes as separate processes so nested costmap parameters load reliably.",
            ),
            DeclareLaunchArgument(
                "use_respawn",
                default_value="False",
                description="Respawn Nav2 nodes if they crash.",
            ),
            DeclareLaunchArgument(
                "use_nav_rviz",
                default_value="true",
                description="Start Nav2 RViz with the integrated bringup. Keep false on SBCs unless actively debugging.",
            ),
            DeclareLaunchArgument(
                "use_lidar_rviz",
                default_value="false",
                description="Start the lidar vendor RViz with the integrated bringup.",
            ),
            DeclareLaunchArgument(
                "start_imu_driver",
                default_value="true",
                description="Start the IMU driver with navigation. Disable when an external supervisor owns it.",
            ),
            DeclareLaunchArgument(
                "nav_rviz_config",
                default_value=os.path.join(
                    get_package_share_directory("nav2_bringup"),
                    "rviz",
                    "nav2_default_view.rviz",
                ),
                description="Full path to the Nav2 RViz config file.",
            ),
            DeclareLaunchArgument(
                "lidar_rviz_config",
                default_value=lidar_rviz_default,
                description="Full path to the lidar RViz config file.",
            ),
            DeclareLaunchArgument(
                "log_level",
                default_value="info",
                description="Logging level for Nav2 nodes.",
            ),
            _car_node("ddsm_hat_diff_drive_node", car_params_file),
            Node(
                package="car_ctrl",
                executable="imu_driver",
                output="screen",
                emulate_tty=True,
                parameters=[car_params_file],
                condition=IfCondition(start_imu_driver),
            ),
            _car_node("car_odometry", car_params_file),
            laser_static_tf,
            back_laser_static_tf,
            Node(
                package="lslidar_driver",
                executable="lslidar_driver_node",
                namespace="lidar_1",
                output="screen",
                emulate_tty=True,
                parameters=[lidar_params_file],
            ),
            Node(
                package="lslidar_driver",
                executable="lslidar_driver_node",
                namespace="lidar_2",
                output="screen",
                emulate_tty=True,
                parameters=[lidar_2_params_file],
            ),
            Node(
                package="dual_laser_merger",
                executable="dual_laser_merger_node",
                name="dual_laser_merger",
                output="screen",
                emulate_tty=True,
                parameters=[
                    {
                        "laser_1_topic": "/scan_f",
                        "laser_2_topic": "/scan_b",
                        "merged_scan_topic": "/scan",
                        "merged_cloud_topic": "/merged_cloud",
                        "target_frame": laser_merger_target_frame,
                        "laser_1_x_offset": 0.0,
                        "laser_1_y_offset": 0.0,
                        "laser_1_yaw_offset": 0.0,
                        "laser_2_x_offset": 0.0,
                        "laser_2_y_offset": 0.0,
                        "laser_2_yaw_offset": 0.0,
                        "tolerance": 0.01,
                        "queue_size": 5,
                        "angle_increment": 0.001,
                        "scan_time": 0.15,
                        "range_min": 0.01,
                        "range_max": 25.0,
                        "min_height": -1.0,
                        "max_height": 1.0,
                        "angle_min": -3.141592654,
                        "angle_max": 3.141592654,
                        "inf_epsilon": 1.0,
                        "use_inf": True,
                        "allowed_radius": 0.45,
                        "enable_shadow_filter": True,
                        "enable_average_filter": False,
                        "enable_single_laser_fallback": True,
                        "single_laser_timeout": 0.6,
                        "scan_status_topic": "/localization/scan_mode",
                    }
                ],
            ),
            # scan_odometry only feeds /scan_odom diagnostics that car_odometry
            # ignores at runtime (use_scan_odometry: false). Dropped to save
            # ~50 MB RAM and one lidar-rate processing thread on the N150.
            # _car_node("scan_odometry", car_params_file),
            lidar_rviz,
            navigation_launch,
        ]
    )
