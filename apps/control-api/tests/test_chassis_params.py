"""Tests for the chassis-params debug API and its surgical file editors."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.api import chassis_params
from app.config import get_settings
from app.main import app

YAML_SAMPLE = """\
amcl:
  ros__parameters:
    # Accuracy-oriented AMCL tuning for the N150 mini PC.
    max_particles: 1500
    min_particles: 400
    sigma_hit: 0.2  # keep this inline comment
    do_beamskip: false
    max_beams: 120

velocity_smoother:
  ros__parameters:
    max_velocity: [0.30, 0.0, 0.8]
    max_decel: [-0.35, 0.0, -1.20]

controller_server:
  ros__parameters:
    FollowPath:
      plugin: "dwb_core::DWBLocalPlanner"
      max_vel_x: 0.30
"""

LAUNCH_SAMPLE = '''\
def generate_launch_description():
    laser_static_tf = Node(
        package="tf2_ros",
        name="base_to_front_laser_tf",
        arguments=[
            "--x",
            "0.215",
            "--yaw",
            "0.78539816339",
        ],
    )
    back_laser_static_tf = Node(
        package="tf2_ros",
        name="base_to_back_laser_tf",
        arguments=[
            "--x",
            "-0.215",
            "--yaw",
            "-2.3561944901",
        ],
    )
'''

PY_DICT_SAMPLE = '''\
def generate_launch_description():
    return LaunchDescription(
        [
            Node(
                # Keep safety-relevant defaults here.
                parameters=[
                    {
                        "enabled": True,
                        "x_tolerance": 0.05,
                        "k_x": 0.6,  # inline note stays
                        "timeout_sec": 90.0,
                    }
                ],
            ),
        ]
    )
'''


class YamlEditorTests(unittest.TestCase):
    def test_nested_scalar_edit_preserves_comments_and_layout(self):
        result = chassis_params.set_yaml_value(
            YAML_SAMPLE, ("amcl", "ros__parameters", "max_particles"), 2000, "int"
        )
        self.assertIn("    max_particles: 2000\n", result)
        self.assertIn("# Accuracy-oriented AMCL tuning for the N150 mini PC.", result)
        self.assertIn("sigma_hit: 0.2  # keep this inline comment", result)
        self.assertIn("min_particles: 400", result)
        # The rest of the document is byte-identical apart from the one line.
        before = YAML_SAMPLE.splitlines()
        after = result.splitlines()
        self.assertEqual(len(before), len(after))
        changed = [index for index, pair in enumerate(zip(before, after)) if pair[0] != pair[1]]
        self.assertEqual(len(changed), 1)

    def test_float_edit_preserves_inline_comment(self):
        result = chassis_params.set_yaml_value(
            YAML_SAMPLE, ("amcl", "ros__parameters", "sigma_hit"), 0.35, "float"
        )
        self.assertIn("sigma_hit: 0.35  # keep this inline comment", result)

    def test_bool_and_float_formatting(self):
        result = chassis_params.set_yaml_value(
            YAML_SAMPLE, ("amcl", "ros__parameters", "do_beamskip"), True, "bool"
        )
        self.assertIn("do_beamskip: true", result)
        result = chassis_params.set_yaml_value(
            YAML_SAMPLE, ("controller_server", "ros__parameters", "FollowPath", "max_vel_x"), 1, "float"
        )
        self.assertIn("max_vel_x: 1.0", result)

    def test_flow_list_element_edit_keeps_other_elements(self):
        result = chassis_params.set_yaml_value(
            YAML_SAMPLE, ("velocity_smoother", "ros__parameters", "max_velocity", 0), 0.45, "float"
        )
        self.assertIn("max_velocity: [0.45, 0.0, 0.8]", result)
        result = chassis_params.set_yaml_value(
            YAML_SAMPLE, ("velocity_smoother", "ros__parameters", "max_decel", 2), -0.9, "float"
        )
        self.assertIn("max_decel: [-0.35, 0.0, -0.9]", result)

    def test_missing_key_raises(self):
        with self.assertRaises(chassis_params.YamlPathNotFoundError):
            chassis_params.set_yaml_value(YAML_SAMPLE, ("amcl", "ros__parameters", "no_such_key"), 1, "int")
        with self.assertRaises(chassis_params.YamlPathNotFoundError):
            chassis_params.set_yaml_value(YAML_SAMPLE, ("no_such_section", "key"), 1, "int")
        with self.assertRaises(chassis_params.YamlPathNotFoundError):
            chassis_params.set_yaml_value(
                YAML_SAMPLE, ("velocity_smoother", "ros__parameters", "max_velocity", 9), 0.1, "float"
            )

    def test_get_yaml_value_reads_nested_and_list(self):
        self.assertEqual(
            chassis_params.get_yaml_value(YAML_SAMPLE, ("amcl", "ros__parameters", "max_particles")), 1500
        )
        self.assertEqual(
            chassis_params.get_yaml_value(
                YAML_SAMPLE, ("velocity_smoother", "ros__parameters", "max_velocity", 2)
            ),
            0.8,
        )
        with self.assertRaises(chassis_params.YamlPathNotFoundError):
            chassis_params.get_yaml_value(YAML_SAMPLE, ("amcl", "ros__parameters", "missing"))


class LaunchTfEditorTests(unittest.TestCase):
    def test_front_and_back_edits_are_independent(self):
        result = chassis_params.set_launch_tf_value(
            LAUNCH_SAMPLE, "base_to_front_laser_tf", "x", 0.3
        )
        self.assertIn('"0.3",', result)
        self.assertIn('"-0.215",', result)
        result = chassis_params.set_launch_tf_value(
            LAUNCH_SAMPLE, "base_to_back_laser_tf", "yaw", -2.0
        )
        self.assertIn('"-2.0",', result)
        self.assertIn('"0.78539816339",', result)

    def test_get_launch_tf_value(self):
        self.assertAlmostEqual(
            chassis_params.get_launch_tf_value(LAUNCH_SAMPLE, "base_to_front_laser_tf", "x"), 0.215
        )
        self.assertAlmostEqual(
            chassis_params.get_launch_tf_value(LAUNCH_SAMPLE, "base_to_back_laser_tf", "yaw"),
            -2.3561944901,
        )

    def test_missing_node_or_flag_raises(self):
        with self.assertRaises(chassis_params.LaunchTfNotFoundError):
            chassis_params.set_launch_tf_value(LAUNCH_SAMPLE, "no_such_tf", "x", 0.1)
        with self.assertRaises(chassis_params.LaunchTfNotFoundError):
            chassis_params.set_launch_tf_value(LAUNCH_SAMPLE, "base_to_front_laser_tf", "z", 0.1)


class PyDictEditorTests(unittest.TestCase):
    def test_numeric_edit_preserves_comments_and_comma(self):
        result = chassis_params.set_py_dict_value(PY_DICT_SAMPLE, "x_tolerance", 0.08, "float")
        self.assertIn('"x_tolerance": 0.08,', result)
        self.assertIn("# Keep safety-relevant defaults here.", result)
        self.assertIn('"k_x": 0.6,  # inline note stays', result)
        self.assertIn('"timeout_sec": 90.0,', result)
        # Only the target line changes.
        before = PY_DICT_SAMPLE.splitlines()
        after = result.splitlines()
        changed = [i for i, pair in enumerate(zip(before, after)) if pair[0] != pair[1]]
        self.assertEqual(len(changed), 1)

    def test_int_and_get_roundtrip(self):
        result = chassis_params.set_py_dict_value(PY_DICT_SAMPLE, "timeout_sec", 120, "float")
        self.assertIn('"timeout_sec": 120.0,', result)
        self.assertEqual(chassis_params.get_py_dict_value(PY_DICT_SAMPLE, "k_x"), 0.6)
        self.assertIs(chassis_params.get_py_dict_value(PY_DICT_SAMPLE, "enabled"), True)

    def test_missing_key_raises(self):
        with self.assertRaises(chassis_params.PyDictKeyNotFoundError):
            chassis_params.set_py_dict_value(PY_DICT_SAMPLE, "no_such_key", 1.0, "float")
        with self.assertRaises(chassis_params.PyDictKeyNotFoundError):
            chassis_params.get_py_dict_value(PY_DICT_SAMPLE, "no_such_key")


class FootprintHelperTests(unittest.TestCase):
    def test_parse_and_format_roundtrip(self):
        length, width = chassis_params.parse_footprint_size(
            "[[0.27, 0.33], [0.27, -0.33], [-0.27, -0.33], [-0.27, 0.33]]"
        )
        self.assertAlmostEqual(length, 0.54)
        self.assertAlmostEqual(width, 0.66)
        self.assertEqual(
            chassis_params.format_footprint(0.6, 0.7),
            "[[0.3, 0.35], [0.3, -0.35], [-0.3, -0.35], [-0.3, 0.35]]",
        )
        # 4-decimal rounding avoids float noise on halves.
        self.assertIn("0.275", chassis_params.format_footprint(0.55, 0.66))

    def test_parse_rejects_bad_shapes(self):
        with self.assertRaises(ValueError):
            chassis_params.parse_footprint_size("[[0.1, 0.2]]")
        with self.assertRaises(ValueError):
            chassis_params.parse_footprint_size("not-a-list")

    def test_quoted_string_yaml_edit_keeps_quotes(self):
        text = 'global_costmap:\n  footprint: "[[0.27, 0.33]]"  # body\n'
        result = chassis_params.set_yaml_value(
            text, ("global_costmap", "footprint"), "[[0.3, 0.35]]", "string"
        )
        self.assertIn('footprint: "[[0.3, 0.35]]"  # body', result)


# Fine-tune parameters the current vehicle runtime predates: they are
# registered in PARAMS but absent from the real on-robot launch file.
VEHICLE_UNSUPPORTED_PARAMS = {
    "finetune.min_linear_speed",
    "finetune.min_angular_speed",
    "finetune.max_initial_translation_error_m",
    "finetune.max_initial_yaw_error_rad",
    "finetune.error_ema_alpha",
}


def test_registry_marks_missing_params_unavailable_against_real_config_files():
    """Params absent from the real on-robot files degrade to unavailable
    (null value + reason) instead of breaking the registry resolution."""
    view = chassis_params._load_params()
    by_key = {item.key: item for item in view.params}
    # Every registered parameter is still reported — none is dropped.
    assert set(by_key) == {param.key for param in chassis_params.PARAMS}

    unavailable = {key for key, item in by_key.items() if not item.available}
    assert unavailable == VEHICLE_UNSUPPORTED_PARAMS
    for key in unavailable:
        assert by_key[key].value is None
        assert by_key[key].unavailable_reason == chassis_params.UNAVAILABLE_REASON

    # Available params resolve to a concrete value, except the body size which
    # may legitimately be None when the two footprint levels disagree.
    for key, item in by_key.items():
        if key in unavailable or key in ("body.length_m", "body.width_m"):
            continue
        assert item.available, key
        assert item.unavailable_reason is None, key
        assert item.value is not None, key


class ChassisParamsApiTests(unittest.TestCase):
    """API tests run against temporary copies of the real config files."""

    def setUp(self) -> None:
        self.client = TestClient(app)
        login = self.client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "123456"}
        )
        self.assertEqual(login.status_code, 200)
        self.headers = {"Authorization": "Bearer %s" % login.json()["token"]}

        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        root = Path(self._dir.name)
        self.nav2 = root / "nav2_params.yaml"
        self.ddsm = root / "ddsm_hat_diff_drive.yaml"
        self.launch = root / "full_navigation.launch.py"
        self.finetune = root / "book_shelf_fine_tune.launch.py"
        shutil.copy(chassis_params.NAV2_PARAMS_FILE, self.nav2)
        shutil.copy(chassis_params.DDSM_PARAMS_FILE, self.ddsm)
        shutil.copy(chassis_params.NAVIGATION_LAUNCH_FILE, self.launch)
        shutil.copy(chassis_params.FINE_TUNE_LAUNCH_FILE, self.finetune)

        # Install-tree copies start absent; individual tests opt into
        # dual-write by creating them under install_root.
        self.install_root = root / "install"
        self._patches = [
            patch.object(chassis_params, "NAV2_PARAMS_FILE", self.nav2),
            patch.object(chassis_params, "DDSM_PARAMS_FILE", self.ddsm),
            patch.object(chassis_params, "NAVIGATION_LAUNCH_FILE", self.launch),
            patch.object(chassis_params, "FINE_TUNE_LAUNCH_FILE", self.finetune),
            patch.dict(
                chassis_params.INSTALL_FILES,
                {
                    "nav2": self.install_root / "nav2_params.yaml",
                    "ddsm": self.install_root / "ddsm_hat_diff_drive.yaml",
                    "launch": self.install_root / "full_navigation.launch.py",
                    "finetune": self.install_root / "book_shelf_fine_tune.launch.py",
                },
            ),
        ]
        for item in self._patches:
            item.start()
            self.addCleanup(item.stop)

    def _make_install_copy(self, source: str) -> Path:
        """Create the install-tree counterpart of one source file for dual-write tests."""
        src = {
            "nav2": self.nav2,
            "ddsm": self.ddsm,
            "launch": self.launch,
            "finetune": self.finetune,
        }[source]
        target = self.install_root / src.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, target)
        return target

    def test_get_allows_operator_when_auth_disabled(self):
        # 交付态（auth_enabled=False）：无登录，合成操作员身份直接通过。
        response = self.client.get("/api/v1/debug/chassis-params")
        self.assertEqual(response.status_code, 200)

    def test_get_requires_login_when_auth_enabled(self):
        settings = get_settings()
        original = settings.auth_enabled
        settings.auth_enabled = True
        self.addCleanup(setattr, settings, "auth_enabled", original)
        response = self.client.get("/api/v1/debug/chassis-params")
        self.assertEqual(response.status_code, 401)

    def test_get_returns_grouped_params_matching_files(self):
        response = self.client.get("/api/v1/debug/chassis-params", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        params = {item["key"]: item for item in response.json()["params"]}
        self.assertEqual(len(params), len(chassis_params.PARAMS))

        groups = {item["group"] for item in params.values()}
        self.assertEqual(
            groups,
            {
                "车辆与轮系",
                "雷达与坐标变换",
                "导航速度链",
                "到位判定",
                "速度平滑",
                "碰撞监控",
                "AMCL",
                "代价地图",
                "车体尺寸",
                "到位精调",
            },
        )

        self.assertAlmostEqual(params["chassis.wheel_radius"]["value"], 0.05025)
        self.assertEqual(params["chassis.max_rpm"]["value"], 330)
        self.assertEqual(params["amcl.max_particles"]["value"], 1500)
        self.assertAlmostEqual(params["nav.max_vel_x"]["value"], 0.30)
        self.assertAlmostEqual(params["smoother.max_velocity.theta"]["value"], 0.8)
        self.assertAlmostEqual(params["tf.front.x"]["value"], 0.235)
        self.assertAlmostEqual(params["tf.back.yaw"]["value"], -2.3561944901)
        self.assertAlmostEqual(params["body.length_m"]["value"], 0.58)
        self.assertAlmostEqual(params["body.width_m"]["value"], 0.58)
        self.assertAlmostEqual(params["body.footprint_padding_global"]["value"], 0.01)
        self.assertAlmostEqual(params["body.footprint_padding_local"]["value"], 0.01)
        self.assertAlmostEqual(params["finetune.x_tolerance"]["value"], 0.05)
        self.assertAlmostEqual(params["finetune.timeout_sec"]["value"], 180.0)

        # Registered but absent from the current vehicle files: unavailable.
        unavailable = {key for key, item in params.items() if not item["available"]}
        self.assertEqual(unavailable, VEHICLE_UNSUPPORTED_PARAMS)
        self.assertFalse(params["finetune.error_ema_alpha"]["available"])
        self.assertIsNone(params["finetune.error_ema_alpha"]["value"])
        self.assertEqual(
            params["finetune.error_ema_alpha"]["unavailable_reason"],
            chassis_params.UNAVAILABLE_REASON,
        )
        for item in params.values():
            for field in (
                "key",
                "group",
                "label",
                "unit",
                "type",
                "min",
                "max",
                "step",
                "value",
                "available",
                "unavailable_reason",
            ):
                self.assertIn(field, item)

    def test_put_rejects_out_of_range_value(self):
        response = self.client.put(
            "/api/v1/debug/chassis-params",
            headers=self.headers,
            json={"values": {"nav.max_vel_x": 9.9}},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("超出允许范围", response.json()["detail"])

    def test_put_rejects_unknown_key_and_bad_type(self):
        response = self.client.put(
            "/api/v1/debug/chassis-params",
            headers=self.headers,
            json={"values": {"no.such.key": 1}},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("未知参数", response.json()["detail"])

        response = self.client.put(
            "/api/v1/debug/chassis-params",
            headers=self.headers,
            json={"values": {"chassis.max_rpm": "abc"}},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("需要数值", response.json()["detail"])

        response = self.client.put(
            "/api/v1/debug/chassis-params",
            headers=self.headers,
            json={"values": {"chassis.max_rpm": 330.5}},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("需要整数", response.json()["detail"])

    def test_put_writes_values_back_and_preserves_comments(self):
        response = self.client.put(
            "/api/v1/debug/chassis-params",
            headers=self.headers,
            json={
                "values": {
                    "chassis.wheel_radius": 0.055,
                    "amcl.max_particles": 2000,
                    "smoother.max_velocity.x": 0.45,
                    "tf.front.x": 0.25,
                    "tf.back.yaw": -2.4,
                }
            },
        )
        self.assertEqual(response.status_code, 200)

        ddsm_text = self.ddsm.read_text(encoding="utf-8")
        # Both wheel_radius sections stay consistent; comments survive.
        self.assertEqual(ddsm_text.count("wheel_radius: 0.055"), 2)
        self.assertIn("# Update these two values to match the chassis geometry.", ddsm_text)

        nav2_text = self.nav2.read_text(encoding="utf-8")
        self.assertIn("max_particles: 2000", nav2_text)
        self.assertIn("max_velocity: [0.45, 0.0, 0.8]", nav2_text)
        self.assertIn("# Accuracy-oriented AMCL tuning for the N150 mini PC.", nav2_text)

        launch_text = self.launch.read_text(encoding="utf-8")
        self.assertIn('"0.25",', launch_text)
        self.assertIn('"-2.4",', launch_text)
        self.assertIn('"-0.235",', launch_text)  # back laser untouched

        refreshed = response.json()["params"]
        values = {item["key"]: item["value"] for item in refreshed}
        self.assertAlmostEqual(values["chassis.wheel_radius"], 0.055)
        self.assertEqual(values["amcl.max_particles"], 2000)
        self.assertAlmostEqual(values["tf.front.x"], 0.25)

    def test_put_body_size_rewrites_both_footprint_levels(self):
        response = self.client.put(
            "/api/v1/debug/chassis-params",
            headers=self.headers,
            json={"values": {"body.length_m": 0.6, "body.width_m": 0.7}},
        )
        self.assertEqual(response.status_code, 200)
        nav2_text = self.nav2.read_text(encoding="utf-8")
        expected = 'footprint: "[[0.3, 0.35], [0.3, -0.35], [-0.3, -0.35], [-0.3, 0.35]]"'
        self.assertEqual(nav2_text.count(expected), 2)  # global + local
        self.assertIn("# Accuracy-oriented rolling costmap for the N150 mini PC.", nav2_text)
        values = {item["key"]: item["value"] for item in response.json()["params"]}
        self.assertAlmostEqual(values["body.length_m"], 0.6)
        self.assertAlmostEqual(values["body.width_m"], 0.7)

    def test_put_body_length_alone_keeps_current_width(self):
        response = self.client.put(
            "/api/v1/debug/chassis-params",
            headers=self.headers,
            json={"values": {"body.length_m": 0.6}},
        )
        self.assertEqual(response.status_code, 200)
        nav2_text = self.nav2.read_text(encoding="utf-8")
        self.assertEqual(nav2_text.count('footprint: "[[0.3, 0.29], [0.3, -0.29], [-0.3, -0.29], [-0.3, 0.29]]"'), 2)

    def test_put_footprint_padding_levels_are_independent(self):
        response = self.client.put(
            "/api/v1/debug/chassis-params",
            headers=self.headers,
            json={"values": {"body.footprint_padding_global": 0.05}},
        )
        self.assertEqual(response.status_code, 200)
        nav2_text = self.nav2.read_text(encoding="utf-8")
        self.assertEqual(nav2_text.count("footprint_padding: 0.05"), 1)
        self.assertEqual(nav2_text.count("footprint_padding: 0.01"), 1)  # local untouched

    def test_inconsistent_footprints_report_empty_value(self):
        nav2_text = self.nav2.read_text(encoding="utf-8")
        # Break only the local level so the two levels disagree.
        broken = nav2_text.replace(
            'footprint: "[[0.29, 0.29], [0.29, -0.29], [-0.29, -0.29], [-0.29, 0.29]]"',
            'footprint: "[[0.20, 0.30], [0.20, -0.30], [-0.20, -0.30], [-0.20, 0.30]]"',
            1,
        )
        self.assertNotEqual(broken, nav2_text)
        self.nav2.write_text(broken, encoding="utf-8")

        response = self.client.get("/api/v1/debug/chassis-params", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        params = {item["key"]: item for item in response.json()["params"]}
        self.assertIsNone(params["body.length_m"]["value"])
        self.assertIsNone(params["body.width_m"]["value"])
        # Other params are unaffected.
        self.assertEqual(params["amcl.max_particles"]["value"], 1500)

        # Writing a single dimension is rejected until both are submitted.
        response = self.client.put(
            "/api/v1/debug/chassis-params",
            headers=self.headers,
            json={"values": {"body.length_m": 0.6}},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("footprint", response.json()["detail"])
        # Submitting both heals the inconsistency in one request.
        response = self.client.put(
            "/api/v1/debug/chassis-params",
            headers=self.headers,
            json={"values": {"body.length_m": 0.54, "body.width_m": 0.66}},
        )
        self.assertEqual(response.status_code, 200)

    def test_put_finetune_param_writes_python_dict_line(self):
        response = self.client.put(
            "/api/v1/debug/chassis-params",
            headers=self.headers,
            json={"values": {"finetune.x_tolerance": 0.08, "finetune.stage_timeout_sec": 45}},
        )
        self.assertEqual(response.status_code, 200)
        text = self.finetune.read_text(encoding="utf-8")
        self.assertIn('"x_tolerance": 0.08,', text)
        self.assertIn('"stage_timeout_sec": 45.0,', text)
        self.assertIn("# This node has no private credentials.", text)
        values = {item["key"]: item["value"] for item in response.json()["params"]}
        self.assertAlmostEqual(values["finetune.x_tolerance"], 0.08)

    def test_put_unavailable_param_is_rejected(self):
        # 当前车端版本不支持的参数（实机配置缺失）不可写入。
        response = self.client.put(
            "/api/v1/debug/chassis-params",
            headers=self.headers,
            json={"values": {"finetune.min_linear_speed": 0.02}},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("无法写入", response.json()["detail"])

    def test_put_writes_install_tree_copy_when_present(self):
        # 运行时加载 install 树：写入必须同时落到 src 与 install 两份。
        install_ddsm = self._make_install_copy("ddsm")
        install_finetune = self._make_install_copy("finetune")
        response = self.client.put(
            "/api/v1/debug/chassis-params",
            headers=self.headers,
            json={"values": {"chassis.wheel_radius": 0.055, "finetune.x_tolerance": 0.08}},
        )
        self.assertEqual(response.status_code, 200)
        for text in (
            self.ddsm.read_text(encoding="utf-8"),
            install_ddsm.read_text(encoding="utf-8"),
        ):
            self.assertEqual(text.count("wheel_radius: 0.055"), 2)
        for text in (
            self.finetune.read_text(encoding="utf-8"),
            install_finetune.read_text(encoding="utf-8"),
        ):
            self.assertIn('"x_tolerance": 0.08,', text)

    def test_put_without_install_tree_copy_only_writes_src(self):
        # install 侧对应文件不存在时保持单写，不报错。
        response = self.client.put(
            "/api/v1/debug/chassis-params",
            headers=self.headers,
            json={"values": {"chassis.max_rpm": 300}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("max_rpm: 300", self.ddsm.read_text(encoding="utf-8"))
        self.assertFalse((self.install_root / "ddsm_hat_diff_drive.yaml").exists())

    def test_put_rejects_out_of_range_new_params(self):
        for values in (
            {"body.length_m": 5.0},
            {"body.footprint_padding_local": 0.5},
            {"finetune.error_ema_alpha": 2.0},
            {"finetune.max_initial_yaw_error_rad": 3.0},
        ):
            response = self.client.put(
                "/api/v1/debug/chassis-params", headers=self.headers, json={"values": values}
            )
            self.assertEqual(response.status_code, 422, values)
            self.assertIn("超出允许范围", response.json()["detail"])

    def test_apply_returns_502_when_restart_fails(self):
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="sudo: a password is required")
        with patch.object(chassis_params, "_restart_robot_runtime", return_value=failed):
            response = self.client.post("/api/v1/debug/chassis-params/apply", headers=self.headers)
        self.assertEqual(response.status_code, 502)
        self.assertIn("重启导航运行时失败", response.json()["detail"])
        self.assertIn("sudo: a password is required", response.json()["detail"])

    def test_apply_returns_ok_on_success(self):
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch.object(chassis_params, "_restart_robot_runtime", return_value=done):
            response = self.client.post("/api/v1/debug/chassis-params/apply", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})


if __name__ == "__main__":
    unittest.main()
