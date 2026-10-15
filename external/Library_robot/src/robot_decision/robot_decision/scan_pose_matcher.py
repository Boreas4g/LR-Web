"""Match a laser scan against the occupancy map, near a seed or across the whole map."""

from __future__ import annotations

import math
import time

import numpy as np


def normalize_angle(angle: float) -> float:
    """Return an angle in the closed interval [-pi, pi]."""
    return math.atan2(math.sin(angle), math.cos(angle))


def _candidate_offsets(window: float, step: float) -> np.ndarray:
    """Return symmetric offsets around zero, always including zero itself."""
    count = max(int(round(window / step)), 0)
    return step * np.arange(-count, count + 1, dtype=float)


def _dilate_occupied(occupied: np.ndarray) -> np.ndarray:
    """Grow a boolean mask by one cell so endpoints tolerate one-cell errors."""
    padded = np.pad(occupied, 1, mode="constant", constant_values=False)
    dilated = np.zeros_like(occupied)
    for row_offset in range(3):
        for column_offset in range(3):
            dilated |= padded[
                row_offset : row_offset + occupied.shape[0],
                column_offset : column_offset + occupied.shape[1],
            ]
    return dilated


def _valid_scan_points(
    ranges,
    angle_min: float,
    angle_increment: float,
    min_range: float,
    max_range: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Return the valid ray endpoints in the scanner frame."""
    distances = np.asarray(ranges, dtype=float)
    angles = angle_min + np.arange(distances.size, dtype=float) * angle_increment
    valid = np.isfinite(distances) & (distances > min_range) & (distances < max_range)
    distances = distances[valid]
    angles = angles[valid]
    return distances * np.cos(angles), distances * np.sin(angles)


class _ScanGridMatcher:
    """Score scan endpoint clouds against a dilated occupancy mask."""

    def __init__(
        self,
        occ_grid,
        resolution: float,
        origin_x: float,
        origin_y: float,
        ranges,
        angle_min: float,
        angle_increment: float,
        min_range: float,
        max_range: float,
        occupied_threshold: int,
    ) -> None:
        self.resolution = resolution
        self.origin_x = origin_x
        self.origin_y = origin_y
        grid = np.asarray(occ_grid)
        self.dilated = _dilate_occupied(grid >= occupied_threshold) if grid.ndim == 2 else np.zeros((0, 0), dtype=bool)
        self.points_x, self.points_y = _valid_scan_points(
            ranges, angle_min, angle_increment, min_range, max_range
        )

    def is_usable(self) -> bool:
        return bool(self.dilated.size) and bool(self.dilated.any()) and self.points_x.size > 0

    def search_window(
        self,
        center_x: float,
        center_y: float,
        center_yaw: float,
        window_m: float,
        step_m: float,
        yaw_window: float,
        yaw_step: float,
        deadline: float,
    ) -> tuple[float, float, float, float]:
        """Search pose candidates around the center, stopping at the deadline.

        The hit-ratio plateau around the true pose is flat, so every candidate
        sharing the best ratio is averaged instead of returning the first
        argmax corner.  When one scored yaw sweep shows the full window would
        miss the deadline, the remaining yaw candidates are thinned (a coarser
        yaw step) so the search still finishes inside its time box.
        """
        offsets = _candidate_offsets(window_m, step_m)
        grid_xs, grid_ys = np.meshgrid(center_x + offsets, center_y + offsets)
        candidate_xs = grid_xs.ravel()
        candidate_ys = grid_ys.ravel()
        yaw_list = list(center_yaw + _candidate_offsets(yaw_window, yaw_step))
        best_ratio = -1.0
        plateau_count = 0
        plateau_sum_x = 0.0
        plateau_sum_y = 0.0
        plateau_sum_sin = 0.0
        plateau_sum_cos = 0.0
        position = 0
        while position < len(yaw_list):
            if position > 0 and time.monotonic() >= deadline:
                break
            iteration_started = time.monotonic()
            ratio, count, sum_x, sum_y = self._translation_plateau_for_yaw(
                yaw_list[position], candidate_xs, candidate_ys
            )
            if count > 0 and ratio > best_ratio + 1e-9:
                best_ratio = ratio
                plateau_count = count
                plateau_sum_x = sum_x
                plateau_sum_y = sum_y
                plateau_sum_sin = count * math.sin(yaw_list[position])
                plateau_sum_cos = count * math.cos(yaw_list[position])
            elif count > 0 and ratio >= best_ratio - 1e-9:
                plateau_count += count
                plateau_sum_x += sum_x
                plateau_sum_y += sum_y
                plateau_sum_sin += count * math.sin(yaw_list[position])
                plateau_sum_cos += count * math.cos(yaw_list[position])
            if position == 0:
                iteration_seconds = max(time.monotonic() - iteration_started, 1e-6)
                affordable = int((deadline - time.monotonic()) / iteration_seconds)
                if affordable < len(yaw_list) - 1:
                    rest = yaw_list[1:]
                    keep = min(max(affordable, 1), len(rest))
                    selected = np.unique(np.linspace(0, len(rest) - 1, keep).round().astype(int))
                    yaw_list = [yaw_list[0]] + [rest[index] for index in selected]
            position += 1
        if plateau_count == 0 or best_ratio <= 0.0:
            return center_x, center_y, center_yaw, 0.0
        return (
            plateau_sum_x / plateau_count,
            plateau_sum_y / plateau_count,
            math.atan2(plateau_sum_sin, plateau_sum_cos),
            best_ratio,
        )

    def _translation_plateau_for_yaw(
        self,
        yaw: float,
        candidate_xs: np.ndarray,
        candidate_ys: np.ndarray,
    ) -> tuple[float, int, float, float]:
        """Return the best hit ratio for one yaw plus its plateau centroid stats."""
        cos_yaw = math.cos(yaw)
        sin_yaw = math.sin(yaw)
        rotated_x = cos_yaw * self.points_x - sin_yaw * self.points_y
        rotated_y = sin_yaw * self.points_x + cos_yaw * self.points_y
        inverse_resolution = 1.0 / self.resolution
        height, width = self.dilated.shape
        best_ratio = -1.0
        plateau_count = 0
        plateau_sum_x = 0.0
        plateau_sum_y = 0.0
        # Chunk the translation axis so a dense scan cannot create an
        # unbounded translations x points temporary on the robot computer.
        for start in range(0, candidate_xs.size, 256):
            stop = min(start + 256, candidate_xs.size)
            columns = np.floor(
                (candidate_xs[start:stop, np.newaxis] + rotated_x[np.newaxis, :] - self.origin_x)
                * inverse_resolution
            ).astype(np.int64)
            rows = np.floor(
                (candidate_ys[start:stop, np.newaxis] + rotated_y[np.newaxis, :] - self.origin_y)
                * inverse_resolution
            ).astype(np.int64)
            inside = (rows >= 0) & (rows < height) & (columns >= 0) & (columns < width)
            hits = np.zeros(inside.shape, dtype=bool)
            hits[inside] = self.dilated[rows[inside], columns[inside]]
            ratios = hits.sum(axis=1) / self.points_x.size
            chunk_best = float(ratios.max())
            if chunk_best > best_ratio + 1e-9:
                plateau = ratios >= chunk_best - 1e-9
                best_ratio = chunk_best
                plateau_count = int(plateau.sum())
                plateau_sum_x = float(candidate_xs[start:stop][plateau].sum())
                plateau_sum_y = float(candidate_ys[start:stop][plateau].sum())
            elif chunk_best >= best_ratio - 1e-9:
                plateau = ratios >= best_ratio - 1e-9
                plateau_count += int(plateau.sum())
                plateau_sum_x += float(candidate_xs[start:stop][plateau].sum())
                plateau_sum_y += float(candidate_ys[start:stop][plateau].sum())
        return best_ratio, plateau_count, plateau_sum_x, plateau_sum_y


def best_pose_in_window(
    occ_grid,
    resolution: float,
    origin_x: float,
    origin_y: float,
    ranges,
    angle_min: float,
    angle_increment: float,
    seed_x: float,
    seed_y: float,
    seed_yaw: float,
    *,
    window_m: float = 1.0,
    step_m: float = 0.05,
    yaw_window: float = math.radians(60.0),
    yaw_step: float = math.radians(5.0),
    fine_window_m: float = 0.1,
    fine_step_m: float = 0.02,
    fine_yaw_window: float = math.radians(6.0),
    fine_yaw_step: float = math.radians(2.0),
    min_range: float = 0.05,
    max_range: float = 12.0,
    occupied_threshold: int = 50,
    time_budget_seconds: float = 4.0,
) -> tuple[float, float, float, float]:
    """Find the pose near the seed whose scan best matches the occupancy map.

    ``occ_grid`` follows the ``nav_msgs/OccupancyGrid`` data convention: row 0
    is the lowest map y and values >= ``occupied_threshold`` count as occupied
    (unknown cells never do).  A coarse grid search around the seed is
    followed by a finer search around the coarse winner.  Returns
    ``(x, y, yaw, ratio)`` where ``ratio`` is the fraction of valid rays whose
    endpoint lands on an occupied cell or its one-cell neighbourhood.  Falls
    back to the seed with ratio 0.0 when no meaningful match can be computed.
    """
    matcher = _ScanGridMatcher(
        occ_grid,
        resolution,
        origin_x,
        origin_y,
        ranges,
        angle_min,
        angle_increment,
        min_range,
        max_range,
        occupied_threshold,
    )
    if not math.isfinite(resolution) or resolution <= 0.0 or not matcher.is_usable():
        return seed_x, seed_y, seed_yaw, 0.0
    started = time.monotonic()
    coarse = matcher.search_window(
        seed_x,
        seed_y,
        seed_yaw,
        window_m,
        step_m,
        yaw_window,
        yaw_step,
        started + time_budget_seconds * 0.7,
    )
    if coarse[3] <= 0.0:
        return seed_x, seed_y, seed_yaw, 0.0
    fine = matcher.search_window(
        coarse[0],
        coarse[1],
        coarse[2],
        fine_window_m,
        fine_step_m,
        fine_yaw_window,
        fine_yaw_step,
        started + time_budget_seconds,
    )
    best = fine if fine is not None and fine[3] >= coarse[3] else coarse
    return best[0], best[1], normalize_angle(best[2]), best[3]


# Upper bound for the first whole-map sweep so a very large map still reaches
# the yaw loop early enough to re-plan the search inside its time box.
_MAX_COARSE_CANDIDATES = 80_000


def best_pose_on_map(
    occ_grid,
    resolution: float,
    origin_x: float,
    origin_y: float,
    ranges,
    angle_min: float,
    angle_increment: float,
    *,
    coarse_step_m: float = 0.1,
    coarse_yaw_step: float = math.radians(45.0),
    top_k: int = 5,
    min_range: float = 0.05,
    max_range: float = 12.0,
    occupied_threshold: int = 50,
    time_budget_seconds: float = 20.0,
) -> tuple[float, float, float, float]:
    """Find the scan's best pose anywhere on the map without any seed.

    Every free map cell on a ``coarse_step_m`` lattice is scored at yaw
    increments of ``coarse_yaw_step``.  Up to ``top_k`` spatially separated
    coarse winners are then refined with :func:`best_pose_in_window` and the
    best refined pose wins.  Returns ``(x, y, yaw, ratio)`` with the same
    meaning as :func:`best_pose_in_window`; ``(0.0, 0.0, 0.0, 0.0)`` when no
    meaningful match can be computed.  The yaw list is thinned whenever one
    scored sweep shows the lattice cannot finish inside the coarse share of
    the time box, and the refinement budget is split across the candidates.
    """
    matcher = _ScanGridMatcher(
        occ_grid,
        resolution,
        origin_x,
        origin_y,
        ranges,
        angle_min,
        angle_increment,
        min_range,
        max_range,
        occupied_threshold,
    )
    if not math.isfinite(resolution) or resolution <= 0.0 or not matcher.is_usable():
        return 0.0, 0.0, 0.0, 0.0
    if coarse_step_m <= 0.0 or coarse_yaw_step <= 0.0 or top_k < 1 or time_budget_seconds <= 0.0:
        return 0.0, 0.0, 0.0, 0.0
    grid = np.asarray(occ_grid)
    free = (grid >= 0) & (grid < occupied_threshold)
    cell_step = max(int(round(coarse_step_m / resolution)), 1)
    rows, columns = np.nonzero(free)
    on_lattice = (rows % cell_step == 0) & (columns % cell_step == 0)
    rows = rows[on_lattice]
    columns = columns[on_lattice]
    if rows.size == 0:
        return 0.0, 0.0, 0.0, 0.0
    candidate_xs = origin_x + (columns.astype(float) + 0.5) * resolution
    candidate_ys = origin_y + (rows.astype(float) + 0.5) * resolution
    if candidate_xs.size > _MAX_COARSE_CANDIDATES:
        selected = np.unique(
            np.linspace(0, candidate_xs.size - 1, _MAX_COARSE_CANDIDATES).round().astype(int)
        )
        candidate_xs = candidate_xs[selected]
        candidate_ys = candidate_ys[selected]

    started = time.monotonic()
    deadline = started + time_budget_seconds
    # Keep a share of the budget for refining the coarse winners.
    coarse_deadline = started + time_budget_seconds * 0.6
    yaw_count = max(int(round(math.tau / coarse_yaw_step)), 1)
    yaw_list = [index * math.tau / yaw_count for index in range(yaw_count)]
    coarse_winners: list[tuple[float, float, float, float]] = []
    position = 0
    while position < len(yaw_list):
        if position > 0 and time.monotonic() >= coarse_deadline:
            break
        iteration_started = time.monotonic()
        ratio, count, sum_x, sum_y = matcher._translation_plateau_for_yaw(
            yaw_list[position], candidate_xs, candidate_ys
        )
        if count > 0 and ratio > 0.0:
            coarse_winners.append(
                (ratio, sum_x / count, sum_y / count, normalize_angle(yaw_list[position]))
            )
        if position == 0:
            iteration_seconds = max(time.monotonic() - iteration_started, 1e-6)
            affordable = int((coarse_deadline - time.monotonic()) / iteration_seconds)
            if affordable < len(yaw_list) - 1:
                rest = yaw_list[1:]
                keep = min(max(affordable, 1), len(rest))
                selected = np.unique(np.linspace(0, len(rest) - 1, keep).round().astype(int))
                yaw_list = [yaw_list[0]] + [rest[index] for index in selected]
        position += 1
    if not coarse_winners:
        return 0.0, 0.0, 0.0, 0.0

    # Refine only spatially distinct winners so adjacent-yaw hits on the same
    # structure do not consume the whole refinement budget.
    coarse_winners.sort(reverse=True)
    spacing_m = max(coarse_step_m, resolution)
    seeds: list[tuple[float, float, float]] = []
    for _ratio, winner_x, winner_y, winner_yaw in coarse_winners:
        if all(math.hypot(winner_x - x, winner_y - y) >= spacing_m for x, y, _yaw in seeds):
            seeds.append((winner_x, winner_y, winner_yaw))
        if len(seeds) >= top_k:
            break

    # The refinement window must bridge the coarse quantization: half the yaw
    # spacing plus margin, and twice the lattice step in translation.
    fine_window = 2.0 * coarse_step_m
    fine_yaw_window = max(0.6 * (math.tau / yaw_count), math.radians(6.0))
    best_x, best_y, best_yaw, best_ratio = 0.0, 0.0, 0.0, 0.0
    for index, (seed_x, seed_y, seed_yaw) in enumerate(seeds):
        remaining = deadline - time.monotonic()
        if remaining <= 0.05:
            break
        candidate = best_pose_in_window(
            occ_grid,
            resolution,
            origin_x,
            origin_y,
            ranges,
            angle_min,
            angle_increment,
            seed_x,
            seed_y,
            seed_yaw,
            window_m=fine_window,
            step_m=min(0.02, coarse_step_m),
            yaw_window=fine_yaw_window,
            yaw_step=math.radians(2.0),
            fine_window_m=0.05,
            fine_step_m=0.01,
            fine_yaw_window=math.radians(4.0),
            fine_yaw_step=math.radians(1.0),
            min_range=min_range,
            max_range=max_range,
            occupied_threshold=occupied_threshold,
            time_budget_seconds=remaining / (len(seeds) - index),
        )
        if candidate[3] > best_ratio:
            best_x, best_y, best_yaw, best_ratio = candidate
    return best_x, best_y, normalize_angle(best_yaw), best_ratio
