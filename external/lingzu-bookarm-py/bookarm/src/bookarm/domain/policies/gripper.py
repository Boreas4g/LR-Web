"""Grasp-outcome policy for the monitored gripper close."""

from __future__ import annotations

from typing import Any, Dict


# ArmService.close_gripper_monitored ends in exactly one of three reasons:
# "target_reached" and "object_contact" carry a book; "timeout" means the
# fingers travelled the whole close stroke without ever meeting one.
GRASP_EMPTY_REASON = "timeout"

# 2026-10-04: 空夹检测现场误判较多（槽位有书也判 timeout），按现场决定先关闭，
# 后续再调试恢复。恢复方式：把开关改回 True 并重启 lr-bookarm-takeout。
# 关闭期间空夹会被当作夹到书继续流程（压测空跑一轮，无安全风险）。
GRASP_CONTACT_CHECK_ENABLED = False


def ensure_grasp_contact(result: Dict[str, Any], *, scenario: str) -> Dict[str, Any]:
    """Pass a successful grasp through; fail the stage on an empty close.

    The leading phrase stays constant so the control station's translation
    layer can recognize it; ``scenario`` points the operator at the specific
    shelf slot, stand slot, or chain step that found no book.
    """

    if not GRASP_CONTACT_CHECK_ENABLED:
        return result
    reason = result.get("reason") if isinstance(result, dict) else None
    if reason == GRASP_EMPTY_REASON:
        raise RuntimeError("gripper closed without contact (%s)" % scenario)
    return result
