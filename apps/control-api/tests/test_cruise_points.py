from datetime import UTC, datetime

from app.map_files import parse_cruise_points_yaml
from app.schemas import MapOrigin, MapVersion, User
from app.store import InMemoryStore


def test_cruise_points_are_converted_to_map_pixels(tmp_path) -> None:
    points_file = tmp_path / "points.yaml"
    points_file.write_text(
        """points:
  reading_area:
    frame_id: map
    x: 1.0
    y: 2.0
    yaw: 1.57
  ignored_frame:
    frame_id: odom
    x: 1.0
    y: 2.0
    yaw: 0.0
""",
        encoding="utf-8",
    )
    point = parse_cruise_points_yaml(points_file.read_bytes())[0]
    assert point.name == "reading_area"

    store = InMemoryStore()
    store.cruise_points_file = points_file
    # 注入总开关默认关闭；这里模拟控制站已配置批准副本的部署态。
    store.cruise_points_enabled = True
    map_version = MapVersion(
        id="MAP-TEST",
        name="测试地图",
        version="v1",
        yamlFile="test.yaml",
        pgmFile="test.pgm",
        width=100,
        height=100,
        resolution=0.1,
        origin=MapOrigin(x=-1.0, y=-2.0, yaw=0.0),
        mode="trinary",
        negate=False,
        occupiedThreshold=0.65,
        freeThreshold=0.196,
        status="draft",
        createdAt=datetime.now(UTC),
        createdBy="admin",
    )

    annotations = store._add_cruise_annotations(map_version, User(username="admin", name="管理员", role="admin"))

    assert len(annotations) == 1
    assert annotations[0].kind == "cruise"
    assert annotations[0].pixelX == 20
    assert annotations[0].pixelY == 59
    assert annotations[0].mapX == 1.0
    assert annotations[0].mapY == 2.0
