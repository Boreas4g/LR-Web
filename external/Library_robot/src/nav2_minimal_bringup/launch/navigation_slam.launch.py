"""Start Nav2 navigation servers against the live map published by SLAM."""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration


def generate_launch_description():
    pkg_share = get_package_share_directory('mobile_robot_nav_bringup')
    use_sim_time = LaunchConfiguration('use_sim_time')
    params_file = LaunchConfiguration('params_file')
    use_composition = LaunchConfiguration('use_composition')
    use_respawn = LaunchConfiguration('use_respawn')
    navigation_autostart = LaunchConfiguration('navigation_autostart')
    log_level = LaunchConfiguration('log_level')

    navigation = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(pkg_share, 'launch', 'navigation.launch.py')
        ),
        launch_arguments={
            'params_file': params_file,
            'use_sim_time': use_sim_time,
            'autostart': 'false',
            'start_localization': 'false',
            'navigation_autostart': navigation_autostart,
            'startup_navigation_on_initial_pose': 'false',
            'use_composition': use_composition,
            'use_respawn': use_respawn,
            'use_rviz': 'false',
            'log_level': log_level,
        }.items(),
    )

    return LaunchDescription([
        DeclareLaunchArgument(
            'use_sim_time', default_value='false',
            description='Use simulation clock if true.',
        ),
        DeclareLaunchArgument(
            'params_file',
            default_value=os.path.join(pkg_share, 'config', 'nav2_params.yaml'),
            description='Full path to the Nav2 parameters file.',
        ),
        DeclareLaunchArgument(
            'navigation_autostart', default_value='true',
            description='Automatically activate navigation lifecycle nodes.',
        ),
        DeclareLaunchArgument(
            'use_composition', default_value='False',
            description='Run Nav2 nodes in a component container if true.',
        ),
        DeclareLaunchArgument(
            'use_respawn', default_value='False',
            description='Respawn non-composed Nav2 nodes if they exit.',
        ),
        DeclareLaunchArgument(
            'log_level', default_value='info', description='ROS log level.',
        ),
        navigation,
    ])
