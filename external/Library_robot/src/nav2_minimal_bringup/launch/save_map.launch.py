from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def _default_maps_dir():
    pkg_share = Path(get_package_share_directory('mobile_robot_nav_bringup'))
    workspace_root = pkg_share.parents[3]
    repo_maps_dir = workspace_root / 'maps'
    repo_maps_dir.mkdir(parents=True, exist_ok=True)
    return str(repo_maps_dir)


def generate_launch_description():
    save_dir = LaunchConfiguration('save_dir')
    map_name = LaunchConfiguration('map_name')
    use_sim_time = LaunchConfiguration('use_sim_time')

    save_map_node = Node(
        package='nav2_map_server',
        executable='map_saver_cli',
        name='map_saver_cli',
        output='screen',
        arguments=[
            '-f',
            [save_dir, '/', map_name],
        ],
        parameters=[{
            'use_sim_time': use_sim_time,
            # Keep the last /map sample so save_map can succeed even when
            # no fresh map message arrives within a short window.
            'map_subscribe_transient_local': True,
            # Raspberry Pi + slam_toolbox can publish /map intermittently.
            'save_map_timeout': 20.0,
        }],
    )

    return LaunchDescription([
        DeclareLaunchArgument(
            'save_dir',
            default_value=_default_maps_dir(),
            description='Directory where the map YAML and image files will be saved.',
        ),
        DeclareLaunchArgument(
            'map_name',
            default_value='Library_map',
            description='Base name of the saved map files.',
        ),
        DeclareLaunchArgument(
            'use_sim_time',
            default_value='false',
            description='Use simulation clock if true.',
        ),
        save_map_node,
    ])
