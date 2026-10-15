from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, GroupAction, IncludeLaunchDescription, SetEnvironmentVariable
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PythonExpression
from ament_index_python.packages import get_package_share_directory
from launch_ros.actions import LoadComposableNodes, Node, SetParameter
from launch_ros.descriptions import ComposableNode, ParameterFile
from nav2_common.launch import ReplaceString, RewrittenYaml

import os
from pathlib import Path


NAV2_CONTAINER_NAME = "nav2_container"


def _default_map_yaml(pkg_share: str) -> str:
    return str(Path(pkg_share) / "maps" / "1813lab.yaml")


def generate_launch_description():
    pkg_share = get_package_share_directory("mobile_robot_nav_bringup")
    nav2_bringup_share = get_package_share_directory("nav2_bringup")
    behavior_tree_dir = os.path.join(pkg_share, "behavior_trees")

    namespace = LaunchConfiguration("namespace")
    map_yaml = LaunchConfiguration("map")
    params_file = LaunchConfiguration("params_file")
    use_sim_time = LaunchConfiguration("use_sim_time")
    autostart = LaunchConfiguration("autostart")
    start_localization = LaunchConfiguration("start_localization")
    navigation_autostart = LaunchConfiguration("navigation_autostart")
    startup_navigation_on_initial_pose = LaunchConfiguration("startup_navigation_on_initial_pose")
    use_composition = LaunchConfiguration("use_composition")
    use_respawn = LaunchConfiguration("use_respawn")
    use_rviz = LaunchConfiguration("use_rviz")
    rviz_config = LaunchConfiguration("rviz_config")
    log_level = LaunchConfiguration("log_level")

    replaced_params = ReplaceString(
        source_file=params_file,
        replacements={
            "__NAV2_BEHAVIOR_TREE_DIR__": behavior_tree_dir,
            "__NAV2_MAP_YAML__": map_yaml,
        },
    )
    configured_params = ParameterFile(
        RewrittenYaml(
            source_file=replaced_params,
            root_key=namespace,
            param_rewrites={"autostart": navigation_autostart},
            convert_types=True,
        ),
        allow_substs=True,
    )

    stdout_linebuf_envvar = SetEnvironmentVariable("RCUTILS_LOGGING_BUFFERED_STREAM", "1")

    declare_namespace_cmd = DeclareLaunchArgument(
        "namespace", default_value="", description="Top-level namespace"
    )
    declare_map_cmd = DeclareLaunchArgument(
        "map",
        default_value=_default_map_yaml(pkg_share),
        description="Full path to the map YAML file.",
    )
    declare_use_sim_time_cmd = DeclareLaunchArgument(
        "use_sim_time", default_value="false", description="Use simulation clock if true"
    )
    declare_params_file_cmd = DeclareLaunchArgument(
        "params_file",
        default_value=os.path.join(pkg_share, "config", "nav2_params.yaml"),
        description="Full path to the ROS2 parameters file to use for all launched nodes",
    )
    declare_autostart_cmd = DeclareLaunchArgument(
        "autostart", default_value="true", description="Automatically startup the nav2 stack"
    )
    declare_start_localization_cmd = DeclareLaunchArgument(
        "start_localization",
        default_value="true",
        description="Start AMCL and the static-map server. Disable when SLAM publishes /map.",
    )
    declare_navigation_autostart_cmd = DeclareLaunchArgument(
        "navigation_autostart",
        default_value="false",
        description="Automatically transition navigation lifecycle nodes.",
    )
    declare_startup_navigation_on_initial_pose_cmd = DeclareLaunchArgument(
        "startup_navigation_on_initial_pose",
        default_value="true",
        description="Start navigation lifecycle after an initial pose is received.",
    )
    declare_use_composition_cmd = DeclareLaunchArgument(
        "use_composition", default_value="False", description="Use composed bringup if True"
    )
    declare_use_respawn_cmd = DeclareLaunchArgument(
        "use_respawn",
        default_value="False",
        description="Whether to respawn if a node crashes. Applied when composition is disabled.",
    )
    declare_use_rviz_cmd = DeclareLaunchArgument(
        "use_rviz",
        default_value="false",
        description="Start RViz together with Nav2. Keep false on SBCs unless actively debugging.",
    )
    declare_rviz_config_cmd = DeclareLaunchArgument(
        "rviz_config",
        default_value=os.path.join(nav2_bringup_share, "rviz", "nav2_default_view.rviz"),
        description="Full path to the RViz config file.",
    )
    declare_log_level_cmd = DeclareLaunchArgument(
        "log_level", default_value="info", description="Logging level"
    )

    lifecycle_nodes = [
        "controller_server",
        "smoother_server",
        "planner_server",
        "route_server",
        "behavior_server",
        "velocity_smoother",
        "collision_monitor",
        "bt_navigator",
        "waypoint_follower",
    ]

    remappings = [("/tf", "tf"), ("/tf_static", "tf_static")]

    localization_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(nav2_bringup_share, "launch", "localization_launch.py")
        ),
        launch_arguments={
            "map": map_yaml,
            "use_sim_time": use_sim_time,
            "autostart": autostart,
            "params_file": replaced_params,
            "use_composition": use_composition,
            "use_respawn": use_respawn,
            "container_name": NAV2_CONTAINER_NAME,
            "log_level": log_level,
        }.items(),
        condition=IfCondition(start_localization),
    )

    nav2_container = Node(
        condition=IfCondition(use_composition),
        package="rclcpp_components",
        executable="component_container_isolated",
        name=NAV2_CONTAINER_NAME,
        output="screen",
        parameters=[{"autostart": autostart, "use_sim_time": use_sim_time}],
        arguments=["--ros-args", "--log-level", log_level],
        remappings=remappings,
    )

    load_nodes = GroupAction(
        condition=IfCondition(PythonExpression(["not ", use_composition])),
        actions=[
            SetParameter("use_sim_time", use_sim_time),
            Node(
                package="nav2_controller",
                executable="controller_server",
                output="screen",
                respawn=use_respawn,
                respawn_delay=2.0,
                parameters=[configured_params],
                arguments=["--ros-args", "--log-level", log_level],
                remappings=remappings + [("cmd_vel", "cmd_vel_nav")],
            ),
            Node(
                package="nav2_smoother",
                executable="smoother_server",
                name="smoother_server",
                output="screen",
                respawn=use_respawn,
                respawn_delay=2.0,
                parameters=[configured_params],
                arguments=["--ros-args", "--log-level", log_level],
                remappings=remappings,
            ),
            Node(
                package="nav2_planner",
                executable="planner_server",
                name="planner_server",
                output="screen",
                respawn=use_respawn,
                respawn_delay=2.0,
                parameters=[configured_params],
                arguments=["--ros-args", "--log-level", log_level],
                remappings=remappings,
            ),
            Node(
                package="nav2_route",
                executable="route_server",
                name="route_server",
                output="screen",
                respawn=use_respawn,
                respawn_delay=2.0,
                parameters=[configured_params],
                arguments=["--ros-args", "--log-level", log_level],
                remappings=remappings,
            ),
            Node(
                package="nav2_behaviors",
                executable="behavior_server",
                name="behavior_server",
                output="screen",
                respawn=use_respawn,
                respawn_delay=2.0,
                parameters=[configured_params],
                arguments=["--ros-args", "--log-level", log_level],
                remappings=remappings + [("cmd_vel", "cmd_vel_nav")],
            ),
            Node(
                package="nav2_bt_navigator",
                executable="bt_navigator",
                name="bt_navigator",
                output="screen",
                respawn=use_respawn,
                respawn_delay=2.0,
                parameters=[configured_params],
                arguments=["--ros-args", "--log-level", log_level],
                remappings=remappings,
            ),
            Node(
                package="nav2_waypoint_follower",
                executable="waypoint_follower",
                name="waypoint_follower",
                output="screen",
                respawn=use_respawn,
                respawn_delay=2.0,
                parameters=[configured_params],
                arguments=["--ros-args", "--log-level", log_level],
                remappings=remappings,
            ),
            Node(
                package="nav2_velocity_smoother",
                executable="velocity_smoother",
                name="velocity_smoother",
                output="screen",
                respawn=use_respawn,
                respawn_delay=2.0,
                parameters=[configured_params],
                arguments=["--ros-args", "--log-level", log_level],
                remappings=remappings + [("cmd_vel", "cmd_vel_nav")],
            ),
            Node(
                package="nav2_collision_monitor",
                executable="collision_monitor",
                name="collision_monitor",
                output="screen",
                respawn=use_respawn,
                respawn_delay=2.0,
                parameters=[configured_params],
                arguments=["--ros-args", "--log-level", log_level],
                remappings=remappings,
            ),
            Node(
                package="nav2_lifecycle_manager",
                executable="lifecycle_manager",
                name="lifecycle_manager_navigation",
                output="screen",
                arguments=["--ros-args", "--log-level", log_level],
                parameters=[{"autostart": navigation_autostart}, {"node_names": lifecycle_nodes}],
            ),
        ],
    )

    load_composable_nodes = GroupAction(
        condition=IfCondition(use_composition),
        actions=[
            SetParameter("use_sim_time", use_sim_time),
            LoadComposableNodes(
                target_container=(namespace, "/", NAV2_CONTAINER_NAME),
                composable_node_descriptions=[
                    ComposableNode(
                        package="nav2_controller",
                        plugin="nav2_controller::ControllerServer",
                        name="controller_server",
                        parameters=[configured_params],
                        remappings=remappings + [("cmd_vel", "cmd_vel_nav")],
                    ),
                    ComposableNode(
                        package="nav2_smoother",
                        plugin="nav2_smoother::SmootherServer",
                        name="smoother_server",
                        parameters=[configured_params],
                        remappings=remappings,
                    ),
                    ComposableNode(
                        package="nav2_planner",
                        plugin="nav2_planner::PlannerServer",
                        name="planner_server",
                        parameters=[configured_params],
                        remappings=remappings,
                    ),
                    ComposableNode(
                        package="nav2_route",
                        plugin="nav2_route::RouteServer",
                        name="route_server",
                        parameters=[configured_params],
                        remappings=remappings,
                    ),
                    ComposableNode(
                        package="nav2_behaviors",
                        plugin="behavior_server::BehaviorServer",
                        name="behavior_server",
                        parameters=[configured_params],
                        remappings=remappings + [("cmd_vel", "cmd_vel_nav")],
                    ),
                    ComposableNode(
                        package="nav2_bt_navigator",
                        plugin="nav2_bt_navigator::BtNavigator",
                        name="bt_navigator",
                        parameters=[configured_params],
                        remappings=remappings,
                    ),
                    ComposableNode(
                        package="nav2_waypoint_follower",
                        plugin="nav2_waypoint_follower::WaypointFollower",
                        name="waypoint_follower",
                        parameters=[configured_params],
                        remappings=remappings,
                    ),
                    ComposableNode(
                        package="nav2_velocity_smoother",
                        plugin="nav2_velocity_smoother::VelocitySmoother",
                        name="velocity_smoother",
                        parameters=[configured_params],
                        remappings=remappings + [("cmd_vel", "cmd_vel_nav")],
                    ),
                    ComposableNode(
                        package="nav2_collision_monitor",
                        plugin="nav2_collision_monitor::CollisionMonitor",
                        name="collision_monitor",
                        parameters=[configured_params],
                        remappings=remappings,
                    ),
                    ComposableNode(
                        package="nav2_lifecycle_manager",
                        plugin="nav2_lifecycle_manager::LifecycleManager",
                        name="lifecycle_manager_navigation",
                        parameters=[{"autostart": navigation_autostart, "node_names": lifecycle_nodes}],
                    ),
                ],
            ),
        ],
    )

    rviz_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(nav2_bringup_share, "launch", "rviz_launch.py")
        ),
        launch_arguments={"use_namespace": "false", "rviz_config": rviz_config}.items(),
        condition=IfCondition(use_rviz),
    )

    nav_startup_helper = Node(
        package="mobile_robot_nav_bringup",
        executable="initial_pose_nav_startup.py",
        name="initial_pose_nav_startup",
        output="screen",
        condition=IfCondition(startup_navigation_on_initial_pose),
    )

    ld = LaunchDescription()
    ld.add_action(stdout_linebuf_envvar)
    ld.add_action(declare_namespace_cmd)
    ld.add_action(declare_map_cmd)
    ld.add_action(declare_use_sim_time_cmd)
    ld.add_action(declare_params_file_cmd)
    ld.add_action(declare_autostart_cmd)
    ld.add_action(declare_start_localization_cmd)
    ld.add_action(declare_navigation_autostart_cmd)
    ld.add_action(declare_startup_navigation_on_initial_pose_cmd)
    ld.add_action(declare_use_composition_cmd)
    ld.add_action(declare_use_respawn_cmd)
    ld.add_action(declare_use_rviz_cmd)
    ld.add_action(declare_rviz_config_cmd)
    ld.add_action(declare_log_level_cmd)
    ld.add_action(nav2_container)
    ld.add_action(localization_launch)
    ld.add_action(nav_startup_helper)
    ld.add_action(load_nodes)
    ld.add_action(load_composable_nodes)
    ld.add_action(rviz_launch)
    return ld
