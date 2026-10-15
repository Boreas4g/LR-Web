"""Operator-facing Chinese translations for raw bookarm/navigation failures."""

from __future__ import annotations

from app.friendly_errors import (
    describe_bookarm_failure,
    describe_bookarm_state,
    describe_map_operation_failure,
    describe_task_status,
)


def test_inverse_kinematics_failure_is_explained() -> None:
    detail = (
        "MovePByJ inverse kinematics failed for target (-0.209, -0.163, 0.298, "
        "1.658, 0.0, 1.571) from joint seed (0.003, 0.182, -1.117, 0.881, -0.001, "
        "-0.000): arm inverse kinematics did not converge"
    )
    text = describe_bookarm_failure("recovery_required", detail)
    assert "机械臂无法到达目标位置" in text
    assert "超出工作空间" in text
    assert "需要人工接管恢复" in text
    assert "inverse kinematics" not in text


def test_reachability_precheck_failure_is_explained() -> None:
    detail = (
        "arm pose unreachable (arm_fine_grasp): IK did not converge for target "
        "(-0.209, -0.163, 0.298, 1.658, 0.0, 1.571) from joint seed "
        "(0.0, 0.17, -1.13, 0.87, 0.0, 0.0): arm inverse kinematics did not converge"
    )
    text = describe_bookarm_failure("failed", detail)
    assert "机械臂可达性预检未通过" in text
    assert "取书·精夹位" in text
    assert "（arm_fine_grasp）" in text
    # The failing pose is surfaced in operator units (mm/degrees).
    assert "x=-209mm" in text
    assert "z=298mm" in text
    assert "rx=95.0°" in text
    assert "未执行任何动作" in text
    # The precheck pattern wins over the generic IK-convergence translation.
    assert "超出工作空间" not in text
    assert "IK did not converge" not in text
    assert "执行失败" in text


def test_reachability_precheck_keeps_the_failed_pose_label() -> None:
    text = describe_bookarm_failure(
        "failed", "arm pose unreachable (stand_place): IK did not converge"
    )
    assert "机械臂可达性预检未通过" in text
    assert "（stand_place）" in text
    # Stand-chain poses come from slot calibration, not the book's shelf height.
    assert "书立槽位标定" in text
    assert "书籍位置/层高无关" in text


def test_reachability_precheck_joint_limit_names_the_joint() -> None:
    text = describe_bookarm_failure(
        "failed",
        "arm pose unreachable (stand_place): arm joint 5 target 2.6000 rad "
        "is outside [-2.5307, 2.5307]",
    )
    assert "机械臂可达性预检未通过" in text
    assert "放入书立·放置位" in text
    assert "第 5 轴需要 149.0°" in text
    assert "[-145.0°, 145.0°]" in text
    assert "未执行任何动作" in text


def test_joint_error_numbers_are_kept() -> None:
    text = describe_bookarm_failure(
        "recovery_required", "arm final joint error 0.0814 rad exceeds 0.0800 rad"
    )
    assert "没有精确停到位" in text
    assert "0.0814" in text
    assert "0.0800" in text


def test_empty_gripper_message_is_explained() -> None:
    text = describe_bookarm_failure(
        "recovery_required", "夹取失败：夹爪空夹（timeout，闭合到极限未检测到书本）"
    )
    assert "夹爪没有夹到书" in text
    assert "确认槽位或书架上确实有书" in text


def test_camera_disconnect_is_explained() -> None:
    detail = (
        "Device disconnected. Failed to reconnect: set_xu(...). "
        "xioctl(UVCIOC_CTRL_QUERY) failed on control 1 Last Error: Connection timed out"
    )
    text = describe_bookarm_failure("failed", detail)
    assert "深度相机 USB 连接中断" in text
    assert "重新插拔" in text
    assert "xioctl" not in text


def test_depth_failure_is_explained() -> None:
    text = describe_bookarm_failure("failed", "no valid D435 depth near pixel (860, 253)")
    assert "深度相机" in text
    assert "无遮挡" in text
    assert "执行失败" in text


def test_spine_not_detected_keeps_attempt_count() -> None:
    text = describe_bookarm_failure(
        "failed", "book spine was not detected within 3 D435 captures"
    )
    assert "3 次拍照" in text
    assert "书脊" in text


def test_take_book_step_wrapper_keeps_step_and_inner_cause() -> None:
    text = describe_bookarm_failure(
        "recovery_required", "book-grasp step rod_home failed: rod command failed"
    )
    assert "「拨杆回零」" in text
    assert "拨杆" in text


def test_reservation_conflict_is_explained() -> None:
    text = describe_bookarm_failure(
        "failed", "bookarm hardware is reserved by another controller"
    )
    assert "另一个控制端占用" in text


def test_joint_fault_code_is_explained() -> None:
    text = describe_bookarm_failure("failed", "arm joint 3 reports fault code 5")
    assert "第 3 轴" in text
    assert "故障码 5" in text


def test_uncalibrated_slot_is_explained() -> None:
    text = describe_bookarm_failure("failed", "book-stand pickup slot 2 is not calibrated")
    assert "槽位 2 尚未标定" in text


def test_already_chinese_detail_passes_through() -> None:
    # The caller's label already says 流程未成功; no redundant state suffix.
    assert describe_bookarm_failure("failed", "ik 失败") == "ik 失败"


def test_recovery_state_appended_to_chinese_detail() -> None:
    text = describe_bookarm_failure("recovery_required", "夹取失败：夹爪空夹（timeout）")
    assert "需要人工接管恢复" in text


def test_unknown_english_detail_is_kept_for_diagnosis() -> None:
    text = describe_bookarm_failure("failed", "some brand-new driver error")
    assert "some brand-new driver error" in text
    assert "执行失败" in text


def test_task_status_words() -> None:
    assert describe_task_status("failed") == "执行失败"
    assert "暂停" in describe_task_status("paused")
    assert describe_task_status("cancelled") == "已取消"
    assert describe_task_status("mystery") == "mystery"


def test_bookarm_state_labels() -> None:
    assert describe_bookarm_state("failed") == "执行失败"
    assert "人工接管" in describe_bookarm_state("recovery_required")
    assert describe_bookarm_state("mystery") == "mystery"


def test_map_not_enabled_on_robot_is_translated() -> None:
    text = describe_map_operation_failure("mapId is not enabled on this robot")
    assert text == "该地图未注册到车端，请先在车端注册该地图后重试"


def test_map_not_active_vehicle_map_is_translated() -> None:
    text = describe_map_operation_failure("mapId is not the active vehicle map")
    assert text == "该地图不是车端当前活动地图"


def test_map_operation_falls_back_to_navigation_patterns() -> None:
    text = describe_map_operation_failure("base wheel feedback is not fresh")
    assert "底盘轮速反馈数据超时" in text


def test_map_operation_unknown_reason_passes_through() -> None:
    assert describe_map_operation_failure("some new vehicle rejection") == "some new vehicle rejection"
