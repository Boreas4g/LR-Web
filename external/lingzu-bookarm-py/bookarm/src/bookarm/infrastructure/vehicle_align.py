"""车辆缓速脉冲对齐服务的车端 HTTP 客户端（bookarm 侧）。

车端 robot_decision.vehicle_nudge_node 在 127.0.0.1:18091 暴露极简 HTTP 控制面；
本模块只做受限调用：定长脉冲（方向 + 毫秒）、停止、状态查询。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict


class VehicleAlignClient:
    """127.0.0.1 本机车端缓速脉冲服务客户端。"""

    def __init__(self, base_url: str = "http://127.0.0.1:18091", timeout_s: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = float(timeout_s)

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, ValueError) as error:
            raise RuntimeError(
                "车辆对齐服务不可达（%s）：%s；请确认 lr-robot-runtime 已启动"
                % (self.base_url, error)
            ) from error
        if not data.get("ok", False):
            raise RuntimeError("车辆对齐服务拒绝执行：%s" % data.get("error", "未知原因"))
        return data

    def pulse(self, direction: str, duration_ms: int) -> Dict[str, Any]:
        """发一个定长缓速脉冲；direction: forward/backward。"""

        if direction not in ("forward", "backward"):
            raise ValueError("direction 必须为 forward/backward")
        return self._post("/pulse", {"direction": direction, "duration_ms": int(duration_ms)})

    def stop(self) -> Dict[str, Any]:
        return self._post("/stop", {})

    def status(self) -> Dict[str, Any]:
        try:
            with urllib.request.urlopen(
                self.base_url + "/status", timeout=self.timeout_s
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, ValueError) as error:
            raise RuntimeError(
                "车辆对齐服务不可达（%s）：%s；请确认 lr-robot-runtime 已启动"
                % (self.base_url, error)
            ) from error
