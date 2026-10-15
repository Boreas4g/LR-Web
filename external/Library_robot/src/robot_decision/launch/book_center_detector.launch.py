import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    pkg_share = get_package_share_directory("robot_decision")

    image_topic = LaunchConfiguration("image_topic")
    target_image_path = LaunchConfiguration("target_image_path")
    start_after_fine_tune = LaunchConfiguration("start_after_fine_tune")
    fine_tune_done_topic = LaunchConfiguration("fine_tune_done_topic")
    lift_move_pulses = LaunchConfiguration("lift_move_pulses")
    lift_settle_sec = LaunchConfiguration("lift_settle_sec")
    match_conf_threshold = LaunchConfiguration("match_conf_threshold")
    center_tol_ratio = LaunchConfiguration("center_tol_ratio")

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "image_topic",
                default_value="/camera/camera/color/image_raw",
            ),
            DeclareLaunchArgument(
                "target_image_path",
                default_value=os.path.join(pkg_share, "target", "target2.jpg"),
            ),
            DeclareLaunchArgument("start_after_fine_tune", default_value="true"),
            DeclareLaunchArgument("fine_tune_done_topic", default_value="/fine_tune/done"),
            DeclareLaunchArgument("lift_move_pulses", default_value="145500"),
            DeclareLaunchArgument("lift_settle_sec", default_value="12.0"),
            DeclareLaunchArgument("match_conf_threshold", default_value="0.65"),
            DeclareLaunchArgument("center_tol_ratio", default_value="0.03"),
            Node(
                package="robot_decision",
                executable="book_center_detector_node",
                name="book_center_detector_node",
                output="screen",
                parameters=[
                    {
                        "image_topic": image_topic,
                        "target_image_path": target_image_path,
                        "start_after_fine_tune": ParameterValue(
                            start_after_fine_tune,
                            value_type=bool,
                        ),
                        "fine_tune_done_topic": fine_tune_done_topic,
                        "lift_move_pulses": ParameterValue(lift_move_pulses, value_type=int),
                        "lift_settle_sec": ParameterValue(lift_settle_sec, value_type=float),
                        "match_conf_threshold": ParameterValue(
                            match_conf_threshold,
                            value_type=float,
                        ),
                        "center_tol_ratio": ParameterValue(center_tol_ratio, value_type=float),
                    }
                ],
            ),
        ]
    )
