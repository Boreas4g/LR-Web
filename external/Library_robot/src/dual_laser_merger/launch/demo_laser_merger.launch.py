# Copyright 2024 pradyum
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import ComposableNodeContainer, Node
from launch_ros.descriptions import ComposableNode


def generate_launch_description():

    ld = LaunchDescription()

    # bag_file_path = f"{get_package_share_directory('dual_laser_merger')}/bag/dual_lidar"

    # play_bag_node = ExecuteProcess(
    #     cmd=['ros2', 'bag', 'play', bag_file_path, '--loop'],
    #     output='screen',
    #     shell=False,
    # )

    # ld.add_action(play_bag_node)

    laser_static_tf = Node(
        package='tf2_ros',
        executable='static_transform_publisher',
        name='base_to_front_laser_tf',
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

    back_laser_static_tf = Node(
        package='tf2_ros',
        executable='static_transform_publisher',
        name='base_to_back_laser_tf',
        output='screen',
        arguments=[
            '--x', '-0.26',
            '--y', '0.0',
            '--z', '0.0',
            '--roll', '0.0',
            '--pitch', '-3.14',
            '--yaw', '0.78539816339',
            '--frame-id', 'base_link',
            '--child-frame-id', 'laser_link_b',
        ],
    )

    ld.add_action(laser_static_tf)
    ld.add_action(back_laser_static_tf)

    dual_laser_merger_node = ComposableNodeContainer(
        name='demo_container',
        namespace='',
        package='rclcpp_components',
        executable='component_container',
        composable_node_descriptions=[
            ComposableNode(
                package='dual_laser_merger',
                plugin='merger_node::MergerNode',
                name='dual_laser_merger',
                parameters=[
                    {'laser_1_topic': '/scan_f'},
                    {'laser_2_topic': '/scan_b'},
                    {'merged_scan_topic': '/scan'},
                    {'target_frame': 'base_link'},
                    {'laser_1_x_offset': 0.0},
                    {'laser_1_y_offset': 0.0},
                    {'laser_1_yaw_offset': 0.0},
                    {'laser_2_x_offset': 0.0},
                    {'laser_2_y_offset': 0.0},
                    {'laser_2_yaw_offset': 0.0},
                    {'tolerance': 0.01},
                    {'queue_size': 5},
                    {'angle_increment': 0.001},
                    {'scan_time': 0.1},
                    {'range_min': 0.01},
                    {'range_max': 25.0},
                    {'min_height': -1.0},
                    {'max_height': 1.0},
                    {'angle_min': -3.141592654},
                    {'angle_max': 3.141592654},
                    {'inf_epsilon': 1.0},
                    {'use_inf': True},
                    {'allowed_radius': 0.45},
                    {'enable_shadow_filter': True},
                    {'enable_average_filter': False},
                ],
            )
        ],
        output='screen',
    )

    ld.add_action(dual_laser_merger_node)

    rviz_node = Node(
        package='rviz2',
        executable='rviz2',
        output='both',
        arguments=[
            '-d',
            f"{get_package_share_directory('dual_laser_merger')}/config/rviz_config.rviz",
        ],
    )

    ld.add_action(rviz_node)

    return ld
