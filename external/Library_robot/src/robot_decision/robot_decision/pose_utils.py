import math

from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped, Quaternion


def yaw_to_quaternion(yaw: float) -> Quaternion:
    quaternion = Quaternion()
    quaternion.x = 0.0
    quaternion.y = 0.0
    quaternion.z = math.sin(yaw * 0.5)
    quaternion.w = math.cos(yaw * 0.5)
    return quaternion


def make_pose_stamped(node, frame_id: str, x: float, y: float, z: float, yaw: float) -> PoseStamped:
    pose = PoseStamped()
    pose.header.stamp = node.get_clock().now().to_msg()
    pose.header.frame_id = frame_id
    pose.pose.position.x = x
    pose.pose.position.y = y
    pose.pose.position.z = z
    pose.pose.orientation = yaw_to_quaternion(yaw)
    return pose


def make_initial_pose(node, frame_id: str, x: float, y: float, z: float, yaw: float) -> PoseWithCovarianceStamped:
    pose = PoseWithCovarianceStamped()
    pose.header.stamp = node.get_clock().now().to_msg()
    pose.header.frame_id = frame_id
    pose.pose.pose.position.x = x
    pose.pose.pose.position.y = y
    pose.pose.pose.position.z = z
    pose.pose.pose.orientation = yaw_to_quaternion(yaw)
    pose.pose.covariance[0] = 0.25
    pose.pose.covariance[7] = 0.25
    pose.pose.covariance[35] = 0.06853891945200942
    return pose
