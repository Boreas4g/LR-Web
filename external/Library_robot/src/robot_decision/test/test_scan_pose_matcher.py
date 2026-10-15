import math

import numpy as np

from robot_decision.scan_pose_matcher import best_pose_in_window, best_pose_on_map, normalize_angle


MAP_RESOLUTION = 0.05
MAP_ORIGIN_X = -5.0
MAP_ORIGIN_Y = -5.0
MAP_SIZE = 200


def draw_wall(grid: np.ndarray, x0: float, y0: float, x1: float, y1: float) -> None:
    """Rasterize an occupied wall segment into the synthetic map."""
    steps = max(int(math.hypot(x1 - x0, y1 - y0) / MAP_RESOLUTION) * 4, 1)
    for index in range(steps + 1):
        x = x0 + (x1 - x0) * index / steps
        y = y0 + (y1 - y0) * index / steps
        row = int((y - MAP_ORIGIN_Y) / MAP_RESOLUTION)
        column = int((x - MAP_ORIGIN_X) / MAP_RESOLUTION)
        grid[row, column] = 100


def raycast_scan(
    pose_x: float,
    pose_y: float,
    pose_yaw: float,
    walls: list[tuple[tuple[float, float], tuple[float, float]]],
    ray_count: int = 360,
) -> tuple[float, float, np.ndarray]:
    """Generate a synthetic 360 degree scan from a ground-truth pose."""
    angle_min = -math.pi
    angle_increment = 2.0 * math.pi / ray_count
    ranges = np.full(ray_count, np.inf)
    for index in range(ray_count):
        direction = pose_yaw + angle_min + index * angle_increment
        direction_x = math.cos(direction)
        direction_y = math.sin(direction)
        nearest = math.inf
        for (x0, y0), (x1, y1) in walls:
            segment_x = x1 - x0
            segment_y = y1 - y0
            denominator = direction_x * segment_y - direction_y * segment_x
            if abs(denominator) < 1e-12:
                continue
            distance = ((x0 - pose_x) * segment_y - (y0 - pose_y) * segment_x) / denominator
            fraction = ((x0 - pose_x) * direction_y - (y0 - pose_y) * direction_x) / denominator
            if 0.05 < distance < 12.0 and 0.0 <= fraction <= 1.0:
                nearest = min(nearest, distance)
        ranges[index] = nearest
    return angle_min, angle_increment, ranges


def test_best_pose_in_window_recovers_ground_truth_from_a_rough_seed() -> None:
    grid = np.zeros((MAP_SIZE, MAP_SIZE), dtype=np.int16)
    draw_wall(grid, 1.0, -2.0, 1.0, 2.0)
    walls = [((1.0, -2.0), (1.0, 2.0))]
    truth_x, truth_y, truth_yaw = 0.2, 0.3, 0.4
    angle_min, angle_increment, ranges = raycast_scan(truth_x, truth_y, truth_yaw, walls)
    seed_x = truth_x + 0.4
    seed_y = truth_y - 0.35
    seed_yaw = truth_yaw + math.radians(25.0)

    x, y, yaw, ratio = best_pose_in_window(
        grid,
        MAP_RESOLUTION,
        MAP_ORIGIN_X,
        MAP_ORIGIN_Y,
        ranges,
        angle_min,
        angle_increment,
        seed_x,
        seed_y,
        seed_yaw,
    )

    assert ratio > 0.8
    assert abs(x - truth_x) < 0.05
    assert abs(y - truth_y) < 0.05
    assert abs(math.degrees(normalize_angle(yaw - truth_yaw))) < 3.0


def test_best_pose_in_window_returns_zero_ratio_for_an_empty_map() -> None:
    grid = np.zeros((MAP_SIZE, MAP_SIZE), dtype=np.int16)
    angle_min, angle_increment, ranges = raycast_scan(0.0, 0.0, 0.0, [])

    x, y, yaw, ratio = best_pose_in_window(
        grid,
        MAP_RESOLUTION,
        MAP_ORIGIN_X,
        MAP_ORIGIN_Y,
        ranges,
        angle_min,
        angle_increment,
        0.1,
        -0.2,
        0.3,
    )

    assert (x, y, yaw, ratio) == (0.1, -0.2, 0.3, 0.0)


# An asymmetric U-shaped room gives the whole-map search a unique solution; a
# single wall or a square room is genuinely ambiguous for any scan matcher.
U_ROOM_WALLS = [
    ((1.0, -2.0), (1.0, 2.0)),
    ((-3.0, -2.0), (-3.0, 2.0)),
    ((-3.0, 2.0), (1.0, 2.0)),
]


def test_best_pose_on_map_recovers_ground_truth_without_a_seed() -> None:
    grid = np.zeros((MAP_SIZE, MAP_SIZE), dtype=np.int16)
    for (x0, y0), (x1, y1) in U_ROOM_WALLS:
        draw_wall(grid, x0, y0, x1, y1)
    truth_x, truth_y, truth_yaw = 0.2, 0.3, 0.4
    angle_min, angle_increment, ranges = raycast_scan(truth_x, truth_y, truth_yaw, U_ROOM_WALLS)

    x, y, yaw, ratio = best_pose_on_map(
        grid,
        MAP_RESOLUTION,
        MAP_ORIGIN_X,
        MAP_ORIGIN_Y,
        ranges,
        angle_min,
        angle_increment,
    )

    assert ratio > 0.8
    assert abs(x - truth_x) < 0.06
    assert abs(y - truth_y) < 0.06
    assert abs(math.degrees(normalize_angle(yaw - truth_yaw))) < 3.0


def test_best_pose_on_map_returns_zero_ratio_for_an_empty_map() -> None:
    grid = np.zeros((MAP_SIZE, MAP_SIZE), dtype=np.int16)
    angle_min, angle_increment, ranges = raycast_scan(0.0, 0.0, 0.0, [])

    x, y, yaw, ratio = best_pose_on_map(
        grid,
        MAP_RESOLUTION,
        MAP_ORIGIN_X,
        MAP_ORIGIN_Y,
        ranges,
        angle_min,
        angle_increment,
    )

    assert (x, y, yaw, ratio) == (0.0, 0.0, 0.0, 0.0)
