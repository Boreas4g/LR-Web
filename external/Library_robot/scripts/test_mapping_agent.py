#!/usr/bin/env python3
"""mapping_agent 的单元测试：保存幂等、标注持久化、单条删除、SLAM 会话防护。

运行方式（导入 rclpy 需要 ROS 环境；测试不创建 ROS 节点、不调用 systemctl、
不访问任何硬件，ROS/systemd/文件系统全部 mock 到内存或 tmp 路径）：

    source /opt/ros/jazzy/setup.bash
    python3 -m pytest scripts/test_mapping_agent.py -q
"""

from __future__ import annotations

import contextlib
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

try:
    import mapping_agent
except SystemExit:
    pytest.skip("需要 source /opt/ros/jazzy/setup.bash（导入 rclpy）", allow_module_level=True)


# ---------------------------------------------------------------------------
# 测试辅助：不创建 ROS 节点的 RosInterface、内存 systemd、本机临时 HTTP 服务
# ---------------------------------------------------------------------------


def make_ros(marks_path):
    """绕过 __init__（不建 rclpy 节点），只初始化被测方法依赖的属性。"""

    ros = mapping_agent.RosInterface.__new__(mapping_agent.RosInterface)
    ros._lock = threading.Lock()
    ros._jog_linear = 0.0
    ros._jog_angular = 0.0
    ros._jog_last_at = 0.0
    ros._out_linear = 0.0
    ros._out_angular = 0.0
    ros._latest_map = None
    ros._latest_map_at = 0.0
    ros._mode_cache = "transition"
    ros._session = mapping_agent.MappingSession(marks_path)
    return ros


class FakeUnits:
    """内存版 systemd：记录单元启停状态与建图栈 InvocationID。"""

    def __init__(self) -> None:
        self.active = {mapping_agent.RUNTIME_UNIT: True, mapping_agent.MAPPING_UNIT: False}
        self.invocation = "inv-initial"
        self.starts = 0

    def is_active(self, unit: str) -> bool:
        return self.active.get(unit, False)

    def run(self, *args: str) -> None:
        action, unit = args
        self.active[unit] = action == "start"
        if unit == mapping_agent.MAPPING_UNIT and action == "start":
            self.starts += 1
            self.invocation = f"inv-start-{self.starts}"


@contextlib.contextmanager
def running_server(ros):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), mapping_agent.MappingAgentHandler)
    httpd.daemon_threads = True
    httpd.ros = ros  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2.0)


def request(method: str, url: str, payload: dict | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def sample_mark(label: str | None = None, x: float = 1.0) -> dict:
    return {"x": x, "y": 2.0, "yaw": 0.5, "label": label, "at": 123.0}


def fake_saver(calls: list, map_id: str = "MAP-ABCDEF012345"):
    def _save() -> dict:
        calls.append(map_id)
        return {
            "ok": True,
            "mapId": map_id,
            "yamlPath": f"/var/lib/lr-robot/maps/{map_id}/map.yaml",
            "pgmPath": f"/var/lib/lr-robot/maps/{map_id}/map.pgm",
            "yamlB64": "eQ==",
            "pgmB64": "cA==",
        }

    return _save


# ---------------------------------------------------------------------------
# 要求 1：保存幂等
# ---------------------------------------------------------------------------


def test_save_is_idempotent_within_mapping_session(tmp_path, monkeypatch):
    marks_path = tmp_path / "marks.json"
    ros = make_ros(marks_path)
    ros._session.add_mark(sample_mark("书架点 1"))
    assert marks_path.exists()
    calls: list = []
    monkeypatch.setattr(mapping_agent, "save_current_map", fake_saver(calls))

    with running_server(ros) as base:
        status, first = request("POST", base + "/mapping/save", {"name": "一楼"})
        assert status == 200
        assert first["ok"] is True
        assert first["alreadySaved"] is False
        assert first["mapId"] == "MAP-ABCDEF012345"
        assert [m["label"] for m in first["marks"]] == ["书架点 1"]
        assert len(calls) == 1
        # 保存成功后标注清空且持久化文件删除
        assert ros.marks() == []
        assert not marks_path.exists()

        # 网络重试/重复点击：直接返回首次结果，不重复 map_saver/注册/生成 ID
        status, second = request("POST", base + "/mapping/save", {})
        assert status == 200
        assert second["alreadySaved"] is True
        assert second["mapId"] == first["mapId"]
        assert second["yamlB64"] == first["yamlB64"]
        assert second["marks"] == first["marks"]
        assert len(calls) == 1


def test_save_cache_resets_on_next_mapping_start(tmp_path, monkeypatch):
    ros = make_ros(tmp_path / "marks.json")
    units = FakeUnits()
    monkeypatch.setattr(mapping_agent, "unit_active", units.is_active)
    monkeypatch.setattr(mapping_agent, "run_systemctl", units.run)
    monkeypatch.setattr(mapping_agent, "unit_invocation_id", lambda _unit: units.invocation)
    calls: list = []
    monkeypatch.setattr(mapping_agent, "save_current_map", fake_saver(calls))

    mapping_agent.switch_to_mapping(ros)  # navigation -> mapping，开启会话
    with running_server(ros) as base:
        _, first = request("POST", base + "/mapping/save", {})
        assert first["alreadySaved"] is False
        request("POST", base + "/mapping/save", {})
        assert len(calls) == 1

        mapping_agent.switch_to_navigation(ros)
        mapping_agent.switch_to_mapping(ros)  # 新会话：保存缓存随 /mapping/start 重置
        _, third = request("POST", base + "/mapping/save", {})
        assert third["alreadySaved"] is False
        assert len(calls) == 2


def test_mapping_start_idempotent_path_keeps_session(tmp_path, monkeypatch):
    """已处于建图模式时 /mapping/start 幂等返回，不重置会话（沿用既有语义）。"""

    ros = make_ros(tmp_path / "marks.json")
    units = FakeUnits()
    units.active[mapping_agent.MAPPING_UNIT] = True
    units.active[mapping_agent.RUNTIME_UNIT] = False
    monkeypatch.setattr(mapping_agent, "unit_active", units.is_active)
    monkeypatch.setattr(mapping_agent, "run_systemctl", units.run)
    monkeypatch.setattr(mapping_agent, "unit_invocation_id", lambda _unit: units.invocation)
    ros._session.add_mark(sample_mark("书架点 1"))
    ros._session.record_saved({"mapId": "MAP-KEEP00000000"})

    mapping_agent.switch_to_mapping(ros)  # 幂等早退
    assert [m["label"] for m in ros.marks()] == ["书架点 1"]
    assert ros.saved_map_response()["mapId"] == "MAP-KEEP00000000"
    assert units.starts == 0  # 未重复启动建图栈


def test_mapping_start_clears_marks_and_file(tmp_path, monkeypatch):
    marks_path = tmp_path / "marks.json"
    ros = make_ros(marks_path)
    units = FakeUnits()
    monkeypatch.setattr(mapping_agent, "unit_active", units.is_active)
    monkeypatch.setattr(mapping_agent, "run_systemctl", units.run)
    monkeypatch.setattr(mapping_agent, "unit_invocation_id", lambda _unit: units.invocation)
    ros._session.add_mark(sample_mark("旧会话点"))
    assert marks_path.exists()

    mapping_agent.switch_to_mapping(ros)
    assert ros.marks() == []
    assert not marks_path.exists()
    assert ros.slam_restarted() is False


# ---------------------------------------------------------------------------
# 要求 2：marks 持久化
# ---------------------------------------------------------------------------


def test_marks_persist_and_restore_round_trip(tmp_path):
    marks_path = tmp_path / "mapping-marks.json"
    session = mapping_agent.MappingSession(marks_path)
    session.add_mark(sample_mark(None, x=1.0))
    session.add_mark(sample_mark("东侧架", x=3.0))

    payload = json.loads(marks_path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert [m["label"] for m in payload["marks"]] == ["书架点 1", "东侧架"]
    # 原子写：同目录无临时文件残留
    assert [p.name for p in tmp_path.iterdir()] == [marks_path.name]

    restored = mapping_agent.MappingSession(marks_path)
    assert restored.restore_marks() == 2
    assert [m["label"] for m in restored.marks()] == ["书架点 1", "东侧架"]


def test_marks_persistence_degrades_to_memory_when_directory_missing(tmp_path, capsys):
    marks_path = tmp_path / "missing-dir" / "marks.json"
    session = mapping_agent.MappingSession(marks_path)
    mark, count = session.add_mark(sample_mark(None))
    assert count == 1
    assert mark["label"] == "书架点 1"
    assert not marks_path.exists()
    assert "退化为仅内存" in capsys.readouterr().err
    # 内存视图仍完整可用
    assert len(session.marks()) == 1


def test_clear_marks_persists_empty_list(tmp_path):
    marks_path = tmp_path / "marks.json"
    session = mapping_agent.MappingSession(marks_path)
    session.add_mark(sample_mark())
    assert session.clear_marks() == 1
    payload = json.loads(marks_path.read_text(encoding="utf-8"))
    assert payload["marks"] == []


def test_clear_marks_with_delete_file_removes_persistence(tmp_path):
    marks_path = tmp_path / "marks.json"
    session = mapping_agent.MappingSession(marks_path)
    session.add_mark(sample_mark())
    session.clear_marks(delete_file=True)
    assert not marks_path.exists()
    assert session.marks() == []


def test_restore_tolerates_missing_and_corrupt_file(tmp_path, capsys):
    session = mapping_agent.MappingSession(tmp_path / "marks.json")
    assert session.restore_marks() == 0  # 文件缺失

    empty_path = tmp_path / "empty.json"
    empty_path.write_text("", encoding="utf-8")
    session = mapping_agent.MappingSession(empty_path)
    assert session.restore_marks() == 0  # 安装时预建的空占位文件，不应告警
    assert capsys.readouterr().err == ""

    corrupt_path = tmp_path / "corrupt.json"
    corrupt_path.write_text("not json {", encoding="utf-8")
    session = mapping_agent.MappingSession(corrupt_path)
    assert session.restore_marks() == 0
    assert session.marks() == []
    assert "损坏" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 要求 3：DELETE /mapping/marks/{index}
# ---------------------------------------------------------------------------


def test_delete_mark_endpoint(tmp_path):
    marks_path = tmp_path / "marks.json"
    ros = make_ros(marks_path)
    ros._session.add_mark(sample_mark("A", x=1.0))
    ros._session.add_mark(sample_mark("B", x=2.0))

    with running_server(ros) as base:
        status, body = request("DELETE", base + "/mapping/marks/0")
        assert status == 200
        assert body["ok"] is True
        assert body["removed"]["label"] == "A"
        assert body["count"] == 1

        # 持久化文件同步更新
        payload = json.loads(marks_path.read_text(encoding="utf-8"))
        assert [m["label"] for m in payload["marks"]] == ["B"]
        _, listing = request("GET", base + "/mapping/marks")
        assert [m["label"] for m in listing["marks"]] == ["B"]

        # 越界与非数字均 404
        status, body = request("DELETE", base + "/mapping/marks/5")
        assert status == 404
        assert "detail" in body
        status, _ = request("DELETE", base + "/mapping/marks/abc")
        assert status == 404


# ---------------------------------------------------------------------------
# 要求 4：SLAM 会话防护
# ---------------------------------------------------------------------------


def test_slam_restart_detection_clears_marks_and_latches_flag(tmp_path):
    marks_path = tmp_path / "marks.json"
    session = mapping_agent.MappingSession(marks_path)
    assert session.track_slam_session("navigation", None) is False
    assert session.track_slam_session("mapping", "inv-a") is False
    assert session.track_slam_session("mapping", "inv-a") is False

    session.add_mark(sample_mark("失效点"))
    assert session.track_slam_session("mapping", "inv-b") is True
    assert session.slam_restarted() is True
    assert session.marks() == []
    assert not marks_path.exists()  # 持久化文件一并删除

    # 标记在会话内保持锁存，离开建图模式才复位
    assert session.track_slam_session("mapping", "inv-b") is False
    assert session.slam_restarted() is True
    assert session.track_slam_session("navigation", None) is False
    assert session.slam_restarted() is False


def test_slam_tracking_tolerates_invocation_query_failure(tmp_path):
    session = mapping_agent.MappingSession(tmp_path / "marks.json")
    assert session.track_slam_session("mapping", "inv-a") is False
    # 查询失败（None）不误判重启，也不清空既有绑定之后的检测能力
    assert session.track_slam_session("mapping", None) is False
    assert session.slam_restarted() is False
    assert session.track_slam_session("mapping", "inv-b") is True


def test_refresh_mode_detects_slam_restart(tmp_path, monkeypatch):
    ros = make_ros(tmp_path / "marks.json")
    monkeypatch.setattr(mapping_agent, "current_mode", lambda: "mapping")
    invocations = iter(["inv-a", "inv-b"])
    monkeypatch.setattr(mapping_agent, "unit_invocation_id", lambda _unit: next(invocations))
    ros._session.add_mark(sample_mark("失效点"))

    ros.refresh_mode()  # 首次绑定 inv-a
    assert ros.slam_restarted() is False
    assert len(ros.marks()) == 1
    ros.refresh_mode()  # inv-b：建图栈重启
    assert ros.slam_restarted() is True
    assert ros.marks() == []


def test_status_reports_slam_restarted(tmp_path, monkeypatch):
    ros = make_ros(tmp_path / "marks.json")
    monkeypatch.setattr(mapping_agent, "current_mode", lambda: "mapping")

    with running_server(ros) as base:
        _, body = request("GET", base + "/mapping/status")
        assert body["mode"] == "mapping"
        assert body["slamRestarted"] is False

        ros._session.track_slam_session("mapping", "inv-a")
        ros._session.track_slam_session("mapping", "inv-b")
        _, body = request("GET", base + "/mapping/status")
        assert body["slamRestarted"] is True
