import os

from ament_index_python.packages import get_package_prefix, get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def _package_file(package_name: str, *relative_path: str) -> str:
    return os.path.join(get_package_share_directory(package_name), *relative_path)


def _car_node(executable_name: str, params_file: LaunchConfiguration, condition: IfCondition) -> Node:
    return Node(
        package="car_ctrl",
        executable=executable_name,
        output="screen",
        emulate_tty=True,
        parameters=[params_file],
        condition=condition,
    )


def generate_launch_description():
    pkg_share = get_package_share_directory("mobile_robot_cartographer_bringup")
    pkg_prefix = get_package_prefix("mobile_robot_cartographer_bringup")
    nav_pkg_share = get_package_share_directory("mobile_robot_nav_bringup")
    compat_lib = os.path.join(pkg_prefix, "lib", "libfastcdr_compat.so")
    existing_ld_preload = os.environ.get("LD_PRELOAD", "")
    cartographer_env = {
        "LD_PRELOAD": compat_lib if not existing_ld_preload else f"{compat_lib}:{existing_ld_preload}"
    }

    configuration_directory = LaunchConfiguration("configuration_directory")
    configuration_basename = LaunchConfiguration("configuration_basename")
    car_params_file = LaunchConfiguration("car_params_file")
    lidar_params_file = LaunchConfiguration("lidar_params_file")
    use_sim_time = LaunchConfiguration("use_sim_time")
    resolution = LaunchConfiguration("resolution")
    publish_period_sec = LaunchConfiguration("publish_period_sec")
    use_rviz = LaunchConfiguration("use_rviz")
    rviz_config = LaunchConfiguration("rviz_config")
    start_robot_drivers = LaunchConfiguration("start_robot_drivers")
    publish_laser_tf = LaunchConfiguration("publish_laser_tf")

    start_robot_drivers_condition = IfCondition(start_robot_drivers)
    publish_laser_tf_condition = IfCondition(publish_laser_tf)

    cartographer_node = Node(
        package="cartographer_ros",
        executable="cartographer_node",
        name="cartographer_node",
        output="screen",
        additional_env=cartographer_env,
        parameters=[{"use_sim_time": use_sim_time}],
        arguments=[
            "-configuration_directory",
            configuration_directory,
            "-configuration_basename",
            configuration_basename,
        ],
        remappings=[
            ("scan", "/scan"),
            ("odom", "/odom"),
        ],
    )

    occupancy_grid_node = Node(
        package="cartographer_ros",
        executable="cartographer_occupancy_grid_node",
        name="cartographer_occupancy_grid_node",
        output="screen",
        additional_env=cartographer_env,
        parameters=[{"use_sim_time": use_sim_time}],
        arguments=[
            "-resolution",
            resolution,
            "-publish_period_sec",
            publish_period_sec,
        ],
    )

    rviz_node = Node(
        condition=IfCondition(use_rviz),
        package="rviz2",
        executable="rviz2",
        arguments=["-d", rviz_config],
        output="screen",
    )

    laser_static_tf = Node(
        condition=publish_laser_tf_condition,
        package="tf2_ros",
        executable="static_transform_publisher",
        name="base_to_laser_tf",
        output="screen",
        arguments=[
            "--x",
            "0.26",
            "--y",
            "0.0",
            "--z",
            "0.0",
            "--roll",
            "0.0",
            "--pitch",
            "0.0",
            "--yaw",
            "-0.78539816339",
            "--frame-id",
            "base_link",
            "--child-frame-id",
            "laser_link",
        ],
    )

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "configuration_directory",
                default_value=os.path.join(pkg_share, "config"),
                description="Full path to the Cartographer configuration directory.",
            ),
            DeclareLaunchArgument(
                "configuration_basename",
                default_value="mobile_robot_2d.lua",
                description="Cartographer Lua configuration file name.",
            ),
            DeclareLaunchArgument(
                "car_params_file",
                default_value=_package_file("car_ctrl", "config", "ddsm_hat_diff_drive.yaml"),
                description="Full path to the car controller and IMU parameters file.",
            ),
            DeclareLaunchArgument(
                "lidar_params_file",
                default_value=_package_file("lslidar_driver", "params", "lsx10.yaml"),
                description="Full path to the lidar driver parameters file.",
            ),
            DeclareLaunchArgument(
                "use_sim_time",
                default_value="false",
                description="Use simulation clock if true.",
            ),
            DeclareLaunchArgument(
                "resolution",
                default_value="0.05",
                description="Resolution of a grid cell in the published occupancy grid.",
            ),
            DeclareLaunchArgument(
                "publish_period_sec",
                default_value="1.0",
                description="OccupancyGrid publishing period in seconds.",
            ),
            DeclareLaunchArgument(
                "use_rviz",
                default_value="false",
                description="Start RViz together with Cartographer.",
            ),
            DeclareLaunchArgument(
                "rviz_config",
                default_value=os.path.join(nav_pkg_share, "rviz", "mapping.rviz"),
                description="Full path to the RViz config file.",
            ),
            DeclareLaunchArgument(
                "start_robot_drivers",
                default_value="true",
                description="Start chassis, IMU, odometry and lidar drivers together with Cartographer.",
            ),
            DeclareLaunchArgument(
                "publish_laser_tf",
                default_value="true",
                description="Publish the static transform from base_link to laser_link.",
            ),
            _car_node("ddsm_hat_diff_drive_node", car_params_file, start_robot_drivers_condition),
            _car_node("imu_driver", car_params_file, start_robot_drivers_condition),
            _car_node("car_odometry", car_params_file, start_robot_drivers_condition),
            Node(
                condition=start_robot_drivers_condition,
                package="lslidar_driver",
                executable="lslidar_driver_node",
                output="screen",
                emulate_tty=True,
                parameters=[lidar_params_file],
            ),
            laser_static_tf,
            cartographer_node,
            occupancy_grid_node,
            rviz_node,
        ]
    )
