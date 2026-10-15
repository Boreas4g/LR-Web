import os
from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def _workspace_file(*relative_path: str) -> str:
    pkg_share = Path(get_package_share_directory('mobile_robot_nav_bringup'))
    workspace_root = pkg_share.parents[3]
    return str(workspace_root.joinpath(*relative_path))


def generate_launch_description():
    pkg_share = get_package_share_directory('mobile_robot_nav_bringup')
    lidar_params_default = os.path.join(
        get_package_share_directory('lslidar_driver'),
        'params',
        'lsx10.yaml',
    )

    use_sim_time = LaunchConfiguration('use_sim_time')
    slam_params_file = LaunchConfiguration('slam_params_file')
    lidar_params_file = LaunchConfiguration('lidar_params_file')
    autostart = LaunchConfiguration('autostart')
    use_rviz = LaunchConfiguration('use_rviz')
    start_lidar = LaunchConfiguration('start_lidar')
    rviz_config = LaunchConfiguration('rviz_config')

    slam_node = Node(
        package='slam_toolbox',
        executable='async_slam_toolbox_node',
        name='slam_toolbox',
        output='screen',
        parameters=[
            slam_params_file,
            {'use_sim_time': use_sim_time},
        ],
    )

    rviz_node = Node(
        condition=IfCondition(use_rviz),
        package='rviz2',
        executable='rviz2',
        arguments=['-d', rviz_config],
        output='screen',
    )

    lifecycle_manager = Node(
        package='nav2_lifecycle_manager',
        executable='lifecycle_manager',
        name='lifecycle_manager_slam',
        output='screen',
        parameters=[
            {'use_sim_time': use_sim_time},
            {'autostart': autostart},
            {'bond_timeout': 0.0},
            {'node_names': ['slam_toolbox']},
        ],
    )

    # ENU: right turn around +Z is negative yaw.
    laser_static_tf = Node(
        package='tf2_ros',
        executable='static_transform_publisher',
        name='base_to_laser_tf',
        output='screen',
        arguments=[
            '--x', '0.26',
            '--y', '0.0',
            '--z', '0.0',
            '--roll', '0.0',
            '--pitch', '0.0',
            '--yaw', '-0.78539816339',
            '--frame-id', 'base_link',
            '--child-frame-id', 'laser_link',
        ],
    )

    lidar_node = Node(
        condition=IfCondition(start_lidar),
        package='lslidar_driver',
        executable='lslidar_driver_node',
        output='screen',
        emulate_tty=True,
        parameters=[lidar_params_file],
    )

    return LaunchDescription([
        DeclareLaunchArgument(
            'use_sim_time',
            default_value='false',
            description='Use simulation clock if true.',
        ),
        DeclareLaunchArgument(
            'slam_params_file',
            default_value=os.path.join(pkg_share, 'config', 'slam_params.yaml'),
            description='Full path to the SLAM parameters file.',
        ),
        DeclareLaunchArgument(
            'lidar_params_file',
            default_value=lidar_params_default,
            description='Full path to the lidar driver parameters file.',
        ),
        DeclareLaunchArgument(
            'autostart',
            default_value='true',
            description='Automatically configure and activate slam_toolbox.',
        ),
        DeclareLaunchArgument(
            'start_lidar',
            default_value='true',
            description='Start the lidar driver so slam_toolbox receives /scan.',
        ),
        DeclareLaunchArgument(
            'use_rviz',
            default_value='true',
            description='Start RViz together with slam_toolbox.',
        ),
        DeclareLaunchArgument(
            'rviz_config',
            default_value=_workspace_file('config', 'slam_tool.rviz'),
            description='Full path to the RViz config file.',
        ),
        slam_node,
        lifecycle_manager,
        rviz_node,
        # laser_static_tf,
        # lidar_node,
    ])
