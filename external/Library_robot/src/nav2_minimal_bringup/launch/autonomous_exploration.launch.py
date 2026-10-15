"""Launch dual-lidar SLAM, Nav2, and optional frontier exploration."""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription, TimerAction
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    pkg_share = get_package_share_directory('mobile_robot_nav_bringup')
    use_sim_time = LaunchConfiguration('use_sim_time')
    nav2_params_file = LaunchConfiguration('nav2_params_file')
    exploration_params_file = LaunchConfiguration('exploration_params_file')
    use_rviz = LaunchConfiguration('use_rviz')
    rviz_config = LaunchConfiguration('rviz_config')
    start_exploration = LaunchConfiguration('start_exploration')
    navigation_start_delay_sec = LaunchConfiguration('navigation_start_delay_sec')
    exploration_start_delay_sec = LaunchConfiguration('exploration_start_delay_sec')

    mapping = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(pkg_share, 'launch', 'mapping_with_drivers.launch.py')
        ),
        launch_arguments={
            'use_sim_time': use_sim_time,
            'use_rviz': use_rviz,
            'rviz_config': rviz_config,
        }.items(),
    )
    navigation = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(pkg_share, 'launch', 'navigation_slam.launch.py')
        ),
        launch_arguments={
            'use_sim_time': use_sim_time,
            'params_file': nav2_params_file,
            'navigation_autostart': 'true',
        }.items(),
    )
    exploration = Node(
        condition=IfCondition(start_exploration),
        package='explore_lite',
        executable='explore',
        name='explore_node',
        output='screen',
        parameters=[
            exploration_params_file,
            {'use_sim_time': use_sim_time},
        ],
        remappings=[('/tf', 'tf'), ('/tf_static', 'tf_static')],
    )
    delayed_navigation = TimerAction(
        period=navigation_start_delay_sec,
        actions=[navigation],
    )
    delayed_exploration = TimerAction(
        period=exploration_start_delay_sec,
        actions=[exploration],
    )

    return LaunchDescription([
        DeclareLaunchArgument(
            'use_sim_time', default_value='false',
            description='Use simulation clock if true.',
        ),
        DeclareLaunchArgument(
            'nav2_params_file',
            default_value=os.path.join(pkg_share, 'config', 'nav2_params.yaml'),
            description='Full path to the Nav2 parameters file.',
        ),
        DeclareLaunchArgument(
            'exploration_params_file',
            default_value=os.path.join(pkg_share, 'config', 'exploration_params.yaml'),
            description='Full path to the frontier exploration parameters file.',
        ),
        DeclareLaunchArgument(
            'start_exploration', default_value='false',
            description='Start frontier exploration. Keep false until the robot is in a safe test area.',
        ),
        DeclareLaunchArgument(
            'navigation_start_delay_sec', default_value='8.0',
            description='Wait for chassis, lidar, and SLAM initialization before starting Nav2.',
        ),
        DeclareLaunchArgument(
            'exploration_start_delay_sec', default_value='15.0',
            description='Wait for SLAM and delayed Nav2 startup before creating the exploration node.',
        ),
        DeclareLaunchArgument(
            'use_rviz', default_value='true', description='Start mapping RViz.',
        ),
        DeclareLaunchArgument(
            'rviz_config',
            default_value=os.path.join(pkg_share, 'rviz', 'mapping.rviz'),
            description='Full path to the RViz configuration file.',
        ),
        mapping,
        delayed_navigation,
        delayed_exploration,
    ])
