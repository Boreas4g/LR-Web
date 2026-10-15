"""Tests for the one-click start-all and restart-all endpoints."""

from __future__ import annotations

import stat
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.api import system as system_api
from app.config import get_settings
from app.main import app


class StartAllEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        login = self.client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "123456"}
        )
        self.assertEqual(login.status_code, 200)
        self.headers = {"Authorization": "Bearer %s" % login.json()["token"]}
        self._dirs = tempfile.TemporaryDirectory()
        self.addCleanup(self._dirs.cleanup)

    def _fake_script(self, lines: str, exit_code: int = 0) -> Path:
        script = Path(self._dirs.name) / "fake-start-all.sh"
        script.write_text(
            "#!/usr/bin/env bash\n%s\nexit %d\n" % (textwrap.dedent(lines), exit_code),
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return script

    def test_start_all_parses_step_lines(self):
        script = self._fake_script(
            """\
            echo 'STEP can0 OK can0 已上线'
            echo 'STEP bookarm-service STARTED 取还书服务已启动 (:8090)'
            """
        )
        original = system_api.START_ALL_SCRIPT
        system_api.START_ALL_SCRIPT = script
        try:
            response = self.client.post("/api/v1/system/start-all", headers=self.headers)
        finally:
            system_api.START_ALL_SCRIPT = original

        self.assertEqual(response.status_code, 200)
        view = response.json()
        self.assertTrue(view["ok"])
        self.assertEqual(
            [(step["name"], step["status"]) for step in view["steps"]],
            [("can0", "OK"), ("bookarm-service", "STARTED")],
        )

    def test_start_all_marks_failed_steps_not_ok(self):
        script = self._fake_script("echo 'STEP robot-runtime FAILED 启动失败'")
        original = system_api.START_ALL_SCRIPT
        system_api.START_ALL_SCRIPT = script
        try:
            response = self.client.post("/api/v1/system/start-all", headers=self.headers)
        finally:
            system_api.START_ALL_SCRIPT = original

        self.assertEqual(response.status_code, 200)
        view = response.json()
        self.assertFalse(view["ok"])
        self.assertEqual(view["steps"][0]["status"], "FAILED")

    def test_start_all_uses_fixed_host_agent_when_configured(self):
        output = "STEP robot-runtime OK 导航栈运行中\n"
        with patch.object(system_api, "_request_host_start_agent", return_value=(0, output)) as call:
            with patch.object(system_api, "get_settings") as get_settings:
                get_settings.return_value.host_start_agent_socket = "/run/lr-platform/start-all.sock"
                response = self.client.post("/api/v1/system/start-all", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        call.assert_called_once_with("/run/lr-platform/start-all.sock", "/v1/start-all")

    def test_start_all_allows_operator_when_auth_disabled(self):
        # 交付态（auth_enabled=False）：无登录，合成操作员身份直接通过；
        # 脚本执行入口打桩，避免测试真正触发宿主启动脚本。
        with patch.object(
            system_api, "_run_fixed_script", new=AsyncMock(return_value=(0, ""))
        ) as run:
            response = self.client.post("/api/v1/system/start-all")
        self.assertEqual(response.status_code, 200)
        run.assert_awaited_once()

    def test_start_all_requires_login_when_auth_enabled(self):
        settings = get_settings()
        original = settings.auth_enabled
        settings.auth_enabled = True
        self.addCleanup(setattr, settings, "auth_enabled", original)
        response = self.client.post("/api/v1/system/start-all")
        self.assertEqual(response.status_code, 401)


class RestartAllEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        login = self.client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "123456"}
        )
        self.assertEqual(login.status_code, 200)
        self.headers = {"Authorization": "Bearer %s" % login.json()["token"]}
        self._dirs = tempfile.TemporaryDirectory()
        self.addCleanup(self._dirs.cleanup)

    def _fake_script(self, lines: str, exit_code: int = 0) -> Path:
        script = Path(self._dirs.name) / "fake-restart-all.sh"
        script.write_text(
            "#!/usr/bin/env bash\n%s\nexit %d\n" % (textwrap.dedent(lines), exit_code),
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return script

    def test_restart_all_parses_step_lines(self):
        script = self._fake_script(
            """\
            echo 'STEP robot-runtime RESTARTED 导航栈与 Edge Gateway 已重启'
            echo 'STEP platform-web OK 已安排 3s 后重启 Web 与 control-api'
            """
        )
        original = system_api.RESTART_ALL_SCRIPT
        system_api.RESTART_ALL_SCRIPT = script
        try:
            response = self.client.post("/api/v1/system/restart-all", headers=self.headers)
        finally:
            system_api.RESTART_ALL_SCRIPT = original

        self.assertEqual(response.status_code, 200)
        view = response.json()
        self.assertTrue(view["ok"])
        self.assertEqual(
            [(step["name"], step["status"]) for step in view["steps"]],
            [("robot-runtime", "RESTARTED"), ("platform-web", "OK")],
        )

    def test_restart_all_marks_failed_steps_not_ok(self):
        script = self._fake_script("echo 'STEP robot-runtime FAILED 重启失败'")
        original = system_api.RESTART_ALL_SCRIPT
        system_api.RESTART_ALL_SCRIPT = script
        try:
            response = self.client.post("/api/v1/system/restart-all", headers=self.headers)
        finally:
            system_api.RESTART_ALL_SCRIPT = original

        self.assertEqual(response.status_code, 200)
        view = response.json()
        self.assertFalse(view["ok"])
        self.assertEqual(view["steps"][0]["status"], "FAILED")

    def test_restart_all_uses_fixed_host_agent_when_configured(self):
        output = "STEP robot-runtime RESTARTED 导航栈与 Edge Gateway 已重启\n"
        with patch.object(system_api, "_request_host_start_agent", return_value=(0, output)) as call:
            with patch.object(system_api, "get_settings") as get_settings:
                get_settings.return_value.host_start_agent_socket = "/run/lr-platform/start-all.sock"
                response = self.client.post("/api/v1/system/restart-all", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        call.assert_called_once_with("/run/lr-platform/start-all.sock", "/v1/restart-all")

    def test_restart_all_allows_operator_when_auth_disabled(self):
        # 交付态（auth_enabled=False）：无登录，合成操作员身份直接通过；
        # 脚本执行入口打桩，避免测试真正触发宿主重启脚本。
        with patch.object(
            system_api, "_run_fixed_script", new=AsyncMock(return_value=(0, ""))
        ) as run:
            response = self.client.post("/api/v1/system/restart-all")
        self.assertEqual(response.status_code, 200)
        run.assert_awaited_once()

    def test_restart_all_requires_login_when_auth_enabled(self):
        settings = get_settings()
        original = settings.auth_enabled
        settings.auth_enabled = True
        self.addCleanup(setattr, settings, "auth_enabled", original)
        response = self.client.post("/api/v1/system/restart-all")
        self.assertEqual(response.status_code, 401)


class BookarmStartPolicyTests(unittest.TestCase):
    """机械臂硬件启动确认：GET/PUT /api/v1/system/bookarm-start-policy。"""

    def setUp(self) -> None:
        self.client = TestClient(app)
        login = self.client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "123456"}
        )
        self.assertEqual(login.status_code, 200)
        self.headers = {"Authorization": "Bearer %s" % login.json()["token"]}
        self._dirs = tempfile.TemporaryDirectory()
        self.addCleanup(self._dirs.cleanup)
        self.policy_file = Path(self._dirs.name) / "bookarm-start-policy.conf"
        original = system_api.BOOKARM_START_POLICY_FILE
        system_api.BOOKARM_START_POLICY_FILE = self.policy_file
        self.addCleanup(setattr, system_api, "BOOKARM_START_POLICY_FILE", original)

    def test_get_defaults_to_disallowed_when_file_missing(self):
        response = self.client.get("/api/v1/system/bookarm-start-policy", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"allow": False})
        self.assertFalse(self.policy_file.exists())

    def test_put_persists_and_get_reads_back(self):
        response = self.client.put(
            "/api/v1/system/bookarm-start-policy", headers=self.headers, json={"allow": True}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"allow": True})
        self.assertEqual(self.policy_file.read_text(encoding="utf-8"), "true\n")

        read_back = self.client.get("/api/v1/system/bookarm-start-policy", headers=self.headers)
        self.assertEqual(read_back.json(), {"allow": True})

        revoked = self.client.put(
            "/api/v1/system/bookarm-start-policy", headers=self.headers, json={"allow": False}
        )
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(revoked.json(), {"allow": False})
        self.assertEqual(self.policy_file.read_text(encoding="utf-8"), "false\n")
        read_back = self.client.get("/api/v1/system/bookarm-start-policy", headers=self.headers)
        self.assertEqual(read_back.json(), {"allow": False})

    def test_get_treats_corrupt_file_as_disallowed(self):
        # 内容不是纯文本 true 一律按未确认处理（安全默认）。
        self.policy_file.write_text("not-json{", encoding="utf-8")
        response = self.client.get("/api/v1/system/bookarm-start-policy", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"allow": False})

    def test_put_validates_body(self):
        response = self.client.put(
            "/api/v1/system/bookarm-start-policy", headers=self.headers, json={}
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
