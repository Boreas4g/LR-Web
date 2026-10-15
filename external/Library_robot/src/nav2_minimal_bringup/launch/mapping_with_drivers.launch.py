import os
from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def _package_file(package_name: str, *relative_path: str) -> str:
    return os.path.join(get_package_share_directory(package_name), *relative_path)


def _workspace_file(*relative_path: str) -> str:
    pkg_share = Path(get_package_share_directory("mobile_robot_nav_bringup"))
    workspace_root = pkg_share.parents[3]
    return str(workspace_root.joinpath(*relative_path))


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

    use_sim_time = LaunchConfiguration("use_sim_time")
    car_params_file = LaunchConfiguration("car_params_file")
    front_lidar_params_file = LaunchConfiguration("front_lidar_params_file")
    back_lidar_params_file = LaunchConfiguration("back_lidar_params_file")
    slam_params_file = LaunchConfiguration("slam_params_file")
    laser_merger_target_frame = LaunchConfiguration("laser_merger_target_frame")
    use_rviz = LaunchConfiguration("use_rviz")
    rviz_config = LaunchConfiguration("rviz_config")

    front_laser_tf = Node(
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

    back_laser_tf = Node(
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

    slam_node = Node(
        package="slam_toolbox",
        executable="async_slam_toolbox_node",
        name="slam_toolbox",
        output="screen",
        parameters=[
            slam_params_file,
            {"use_sim_time": use_sim_time},
        ],
    )

    lifecycle_manager = Node(
        package="nav2_lifecycle_manager",
        executable="lifecycle_manager",
        name="lifecycle_manager_slam",
        output="screen",
        parameters=[
            {"use_sim_time": use_sim_time},
            {"autostart": True},
            {"bond_timeout": 0.0},
            {"node_names": ["slam_toolbox"]},
        ],
    )

    rviz_node = Node(
        condition=IfCondition(use_rviz),
        package="rviz2",
        executable="rviz2",
        arguments=["-d", rviz_config],
        output="screen",
    )

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "use_sim_time",
                default_value="false",
                description="Use simulation clock if true.",
            ),
            DeclareLaunchArgument(
                "car_params_file",
                default_value=_package_file("car_ctrl", "config", "ddsm_hat_diff_drive.yaml"),
                description="Full path to the chassis, IMU and odometry parameter file.",
            ),
            DeclareLaunchArgument(
                "front_lidar_params_file",
                default_value=_package_file("lslidar_driver", "params", "lsx10_1.yaml"),
                description="Full path to the front lidar parameter file.",
            ),
            DeclareLaunchArgument(
                "back_lidar_params_file",
                default_value=_package_file("lslidar_driver", "params", "lsx10_2.yaml"),
                description="Full path to the rear lidar parameter file.",
            ),
            DeclareLaunchArgument(
                "slam_params_file",
                default_value=os.path.join(pkg_share, "config", "slam_params.yaml"),
                description="Full path to the slam_toolbox parameter file.",
            ),
            DeclareLaunchArgument(
                "laser_merger_target_frame",
                default_value="base_link",
                description="Target frame for the dual laser merger output.",
            ),
            DeclareLaunchArgument(
                "use_rviz",
                default_value="true",
                description="Start RViz together with slam_toolbox.",
            ),
            DeclareLaunchArgument(
                "rviz_config",
                default_value=_workspace_file("config", "slam_tool.rviz"),
                description="Full path to the RViz config file.",
            ),
            _car_node("ddsm_hat_diff_drive_node", car_params_file),
            _car_node("imu_driver", car_params_file),
            _car_node("car_odometry", car_params_file),
            front_laser_tf,
            back_laser_tf,
            Node(
                package="lslidar_driver",
                executable="lslidar_driver_node",
                namespace="lidar_1",
                output="screen",
                emulate_tty=True,
                parameters=[front_lidar_params_file],
            ),
            Node(
                package="lslidar_driver",
                executable="lslidar_driver_node",
                namespace="lidar_2",
                output="screen",
                emulate_tty=True,
                parameters=[back_lidar_params_file],
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
                        "scan_time": 0.1,
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
            slam_node,
            lifecycle_manager,
            rviz_node,
        ]
    )
