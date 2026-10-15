import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, GroupAction, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration


def _package_launch(package_name: str, launch_file: str) -> PythonLaunchDescriptionSource:
    return PythonLaunchDescriptionSource(
        os.path.join(get_package_share_directory(package_name), "launch", launch_file)
    )


def generate_launch_description():
    robot_decision_share = get_package_share_directory("robot_decision")

    points_file = LaunchConfiguration("points_file")
    lifttable_speed_rpm = LaunchConfiguration("lifttable_speed_rpm")
    lifttable_accel = LaunchConfiguration("lifttable_accel")
    lifttable_hold_on_shutdown = LaunchConfiguration("lifttable_hold_on_shutdown")
    image_topic = LaunchConfiguration("image_topic")
    target_image_path = LaunchConfiguration("target_image_path")
    start_after_fine_tune = LaunchConfiguration("start_after_fine_tune")
    fine_tune_done_topic = LaunchConfiguration("fine_tune_done_topic")
    lift_move_pulses = LaunchConfiguration("lift_move_pulses")
    lift_settle_sec = LaunchConfiguration("lift_settle_sec")

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "points_file",
                default_value=os.path.join(robot_decision_share, "config", "points.yaml"),
            ),
            DeclareLaunchArgument("lifttable_speed_rpm", default_value="200"),
            DeclareLaunchArgument("lifttable_accel", default_value="100"),
            DeclareLaunchArgument("lifttable_hold_on_shutdown", default_value="true"),
            DeclareLaunchArgument("image_topic", default_value="/camera/camera/color/image_raw"),
            DeclareLaunchArgument(
                "target_image_path",
                default_value=os.path.join(robot_decision_share, "target", "target2.jpg"),
            ),
            DeclareLaunchArgument("start_after_fine_tune", default_value="true"),
            DeclareLaunchArgument("fine_tune_done_topic", default_value="/fine_tune/done"),
            DeclareLaunchArgument("lift_move_pulses", default_value="145500"),
            DeclareLaunchArgument("lift_settle_sec", default_value="12.0"),
            IncludeLaunchDescription(
                _package_launch("arm_aux_devices", "lifttable.launch.py"),
                launch_arguments={
                    "speed_rpm": lifttable_speed_rpm,
                    "accel": lifttable_accel,
                    "hold_on_shutdown": lifttable_hold_on_shutdown,
                }.items(),
            ),
            GroupAction(
                scoped=True,
                forwarding=False,
                actions=[
                    IncludeLaunchDescription(
                        _package_launch("realsense2_camera", "rs_launch.py"),
                    ),
                ],
            ),
            IncludeLaunchDescription(
                _package_launch("robot_decision", "book_center_detector.launch.py"),
                launch_arguments={
                    "image_topic": image_topic,
                    "target_image_path": target_image_path,
                    "start_after_fine_tune": start_after_fine_tune,
                    "fine_tune_done_topic": fine_tune_done_topic,
                    "lift_move_pulses": lift_move_pulses,
                    "lift_settle_sec": lift_settle_sec,
                }.items(),
            ),
            IncludeLaunchDescription(
                _package_launch("robot_decision", "cruise.launch.py"),
                launch_arguments={
                    "points_file": points_file,
                    "fine_tune_done_topic": fine_tune_done_topic,
                }.items(),
            ),
        ]
    )
