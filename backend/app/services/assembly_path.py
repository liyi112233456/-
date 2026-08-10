from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from scipy.spatial.transform import Rotation, Slerp
from shapely.geometry import LineString, Point, box
from shapely.strtree import STRtree

from .ifc_geometry import Rebar

Progress = Callable[[str, float, str], None]


def _norm(value: np.ndarray, default=(0.0, 0.0, 1.0)) -> np.ndarray:
    vector = np.asarray(value, dtype=float)
    length = float(np.linalg.norm(vector))
    return vector / length if length > 1e-12 else np.asarray(default, dtype=float)


def _arc_point(points: np.ndarray, fraction: float) -> np.ndarray:
    lengths = np.linalg.norm(np.diff(points, axis=0), axis=1)
    total = float(lengths.sum())
    target = float(np.clip(fraction, 0.0, 1.0)) * total
    elapsed = 0.0
    for index, length in enumerate(lengths):
        if elapsed + length >= target or index == len(lengths) - 1:
            u = 0.0 if length < 1e-12 else (target - elapsed) / length
            return points[index] + u * (points[index + 1] - points[index])
        elapsed += float(length)
    return points[-1].copy()


def _segment_distances_batch(
    p0: np.ndarray, p1: np.ndarray, q0: np.ndarray, q1: np.ndarray
) -> np.ndarray:
    """Exact closest distance for paired batches of 3-D segments."""
    u = p1 - p0
    v = q1 - q0
    uu = np.einsum("ij,ij->i", u, u)
    vv = np.einsum("ij,ij->i", v, v)
    eps = 1e-18
    candidates: list[np.ndarray] = []

    def add(t: np.ndarray, s: np.ndarray) -> None:
        pp = p0 + t[:, None] * u
        qq = q0 + s[:, None] * v
        candidates.append(np.einsum("ij,ij->i", pp - qq, pp - qq))

    r = p0 - q0
    uv = np.einsum("ij,ij->i", u, v)
    ur = np.einsum("ij,ij->i", u, r)
    vr = np.einsum("ij,ij->i", v, r)
    den = uu * vv - uv * uv
    t = np.divide(uv * vr - ur * vv, den, out=np.zeros_like(den), where=np.abs(den) > eps)
    s = np.divide(uu * vr - uv * ur, den, out=np.zeros_like(den), where=np.abs(den) > eps)
    valid = (t >= 0) & (t <= 1) & (s >= 0) & (s <= 1) & (np.abs(den) > eps)
    pp = p0 + np.where(valid, t, 0.0)[:, None] * u
    qq = q0 + np.where(valid, s, 0.0)[:, None] * v
    interior = np.einsum("ij,ij->i", pp - qq, pp - qq)
    interior[~valid] = np.inf
    candidates.append(interior)

    s0 = np.clip(
        np.divide(
            np.einsum("ij,ij->i", p0 - q0, v),
            vv,
            out=np.zeros_like(vv),
            where=vv > eps,
        ),
        0,
        1,
    )
    add(np.zeros_like(s0), s0)
    s1 = np.clip(
        np.divide(
            np.einsum("ij,ij->i", p1 - q0, v),
            vv,
            out=np.zeros_like(vv),
            where=vv > eps,
        ),
        0,
        1,
    )
    add(np.ones_like(s1), s1)
    t0 = np.clip(
        np.divide(
            np.einsum("ij,ij->i", q0 - p0, u),
            uu,
            out=np.zeros_like(uu),
            where=uu > eps,
        ),
        0,
        1,
    )
    add(t0, np.zeros_like(t0))
    t1 = np.clip(
        np.divide(
            np.einsum("ij,ij->i", q1 - p0, u),
            uu,
            out=np.zeros_like(uu),
            where=uu > eps,
        ),
        0,
        1,
    )
    add(t1, np.ones_like(t1))
    return np.sqrt(np.maximum(np.min(np.vstack(candidates), axis=0), 0.0))


@dataclass
class PoseState:
    position: np.ndarray
    quaternion: np.ndarray

    def rotation(self) -> Rotation:
        return Rotation.from_quat(self.quaternion)


def _pose_angle(a: PoseState, b: PoseState) -> float:
    return float(np.linalg.norm((a.rotation().inv() * b.rotation()).as_rotvec()))


def _interpolate_pose(a: PoseState, b: PoseState, u: float) -> PoseState:
    u = float(np.clip(u, 0.0, 1.0))
    rotations = Rotation.from_quat(np.stack([a.quaternion, b.quaternion]))
    rotation = Slerp([0.0, 1.0], rotations)([u])[0]
    return PoseState(a.position + u * (b.position - a.position), rotation.as_quat())


def _transform_axis(bar: Rebar, pivot: np.ndarray, state: PoseState) -> np.ndarray:
    return state.rotation().apply(bar.axis - pivot) + state.position


class CapsuleCollisionWorld:
    """Static final-position capsules filtered by installation step."""

    def __init__(self, rebars: list[Rebar], step_by_bar: dict[int, int], clearance_mm: float):
        self.rebars = rebars
        self.clearance_mm = float(clearance_mm)
        self.owner: list[int] = []
        self.a: list[np.ndarray] = []
        self.b: list[np.ndarray] = []
        self.radius: list[float] = []
        geoms = []
        for bar in rebars:
            for start, end in zip(bar.axis[:-1], bar.axis[1:]):
                self.owner.append(bar.index)
                self.a.append(start)
                self.b.append(end)
                self.radius.append(bar.radius)
                xy0, xy1 = start[:2], end[:2]
                if float(np.linalg.norm(xy1 - xy0)) < 1e-10:
                    geoms.append(Point(float(xy0[0]), float(xy0[1])))
                else:
                    geoms.append(LineString([xy0, xy1]))
        self.owner_array = np.asarray(self.owner, dtype=np.int32)
        self.a_array = np.asarray(self.a, dtype=float)
        self.b_array = np.asarray(self.b, dtype=float)
        self.radius_array = np.asarray(self.radius, dtype=float)
        self.owner_step = np.asarray(
            [step_by_bar[int(owner)] for owner in self.owner_array], dtype=np.int32
        )
        self.max_radius = float(max(self.radius, default=0.0))
        self.tree = STRtree(geoms) if geoms else None
        self.pose_checks = 0
        self.segment_tests = 0

    def _candidate_indices(
        self, start: np.ndarray, end: np.ndarray, moving_radius: float, step: int, moving_bar: int
    ) -> np.ndarray:
        if self.tree is None:
            return np.empty(0, dtype=np.int64)
        pad = moving_radius + self.max_radius + max(0.0, self.clearance_mm)
        lo = np.minimum(start[:2], end[:2]) - pad
        hi = np.maximum(start[:2], end[:2]) + pad
        indices = np.asarray(
            self.tree.query(box(float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1]))),
            dtype=np.int64,
        )
        if len(indices) == 0:
            return indices
        installed = (self.owner_step[indices] < step) & (self.owner_array[indices] != moving_bar)
        indices = indices[installed]
        if len(indices) == 0:
            return indices
        threshold = moving_radius + self.radius_array[indices] + self.clearance_mm
        zlo = np.minimum(self.a_array[indices, 2], self.b_array[indices, 2])
        zhi = np.maximum(self.a_array[indices, 2], self.b_array[indices, 2])
        moving_lo = min(float(start[2]), float(end[2]))
        moving_hi = max(float(start[2]), float(end[2]))
        return indices[(zhi + threshold >= moving_lo) & (zlo - threshold <= moving_hi)]

    def contacts(
        self, bar: Rebar, axis: np.ndarray, step: int
    ) -> dict[int, float]:
        result: dict[int, float] = {}
        for start, end in zip(axis[:-1], axis[1:]):
            indices = self._candidate_indices(start, end, bar.radius, step, bar.index)
            if len(indices) == 0:
                continue
            count = len(indices)
            distances = _segment_distances_batch(
                np.repeat(start[None, :], count, axis=0),
                np.repeat(end[None, :], count, axis=0),
                self.a_array[indices],
                self.b_array[indices],
            )
            thresholds = bar.radius + self.radius_array[indices] + self.clearance_mm
            for candidate, distance, threshold in zip(indices, distances, thresholds):
                if distance < threshold - 1e-7:
                    owner = int(self.owner_array[candidate])
                    result[owner] = min(result.get(owner, math.inf), float(distance))
        return result

    def check_pose(
        self,
        bar: Rebar,
        pivot: np.ndarray,
        state: PoseState,
        step: int,
        target_contacts: dict[int, float],
        near_goal: bool,
    ) -> tuple[bool, Optional[dict], float]:
        self.pose_checks += 1
        axis = _transform_axis(bar, pivot, state)
        minimum_clearance = math.inf
        for segment_index, (start, end) in enumerate(zip(axis[:-1], axis[1:])):
            indices = self._candidate_indices(start, end, bar.radius, step, bar.index)
            if len(indices) == 0:
                continue
            self.segment_tests += len(indices)
            count = len(indices)
            distances = _segment_distances_batch(
                np.repeat(start[None, :], count, axis=0),
                np.repeat(end[None, :], count, axis=0),
                self.a_array[indices],
                self.b_array[indices],
            )
            thresholds = bar.radius + self.radius_array[indices] + self.clearance_mm
            clearances = distances - thresholds
            minimum_clearance = min(minimum_clearance, float(np.min(clearances)))
            colliding = np.flatnonzero(clearances < -1e-7)
            for local in colliding:
                candidate = int(indices[local])
                owner = int(self.owner_array[candidate])
                allowed = (
                    near_goal
                    and owner in target_contacts
                    and float(distances[local]) >= target_contacts[owner] - 0.5
                )
                if allowed:
                    continue
                return False, {
                    "moving_segment": segment_index,
                    "obstacle_bar_index": owner,
                    "distance_mm": float(distances[local]),
                    "required_mm": float(thresholds[local]),
                    "penetration_mm": float(-clearances[local]),
                }, minimum_clearance
        return True, None, minimum_clearance


def _edge_is_free(
    world: CapsuleCollisionWorld,
    bar: Rebar,
    pivot: np.ndarray,
    start: PoseState,
    end: PoseState,
    step: int,
    target_contacts: dict[int, float],
    translation_step: float,
    rotation_step_rad: float,
    goal: PoseState,
) -> tuple[bool, Optional[dict], int, float]:
    distance = float(np.linalg.norm(end.position - start.position))
    angle = _pose_angle(start, end)
    sample_count = max(
        1,
        int(math.ceil(distance / translation_step)),
        int(math.ceil(angle / rotation_step_rad)),
    )
    minimum_clearance = math.inf
    for sample_index in range(sample_count + 1):
        state = _interpolate_pose(start, end, sample_index / sample_count)
        near_goal = (
            float(np.linalg.norm(state.position - goal.position)) <= 1.5 * translation_step
            and _pose_angle(state, goal) <= 1.5 * rotation_step_rad
        )
        free, hit, clearance = world.check_pose(
            bar, pivot, state, step, target_contacts, near_goal
        )
        minimum_clearance = min(minimum_clearance, clearance)
        if not free:
            return False, hit, sample_index + 1, minimum_clearance
    return True, None, sample_count + 1, minimum_clearance


def _direction_candidates(
    bar: Rebar, preferred: np.ndarray, model_center: np.ndarray
) -> list[np.ndarray]:
    raw = [_norm(preferred)]
    inward = model_center - bar.axis.mean(0)
    if np.linalg.norm(inward) > 1e-9:
        raw.append(_norm(inward))
    for x, y, z in (
        (1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1),
        (1, 1, 0), (1, -1, 0), (-1, 1, 0), (-1, -1, 0),
        (1, 0, 1), (1, 0, -1), (-1, 0, 1), (-1, 0, -1),
        (0, 1, 1), (0, 1, -1), (0, -1, 1), (0, -1, -1),
    ):
        raw.append(_norm(np.array([x, y, z], dtype=float)))
    result: list[np.ndarray] = []
    for direction in raw:
        if not any(float(np.dot(direction, existing)) > 0.996 for existing in result):
            result.append(direction)
    return result


def _outside_state(
    bar: Rebar,
    pivot: np.ndarray,
    entry_direction: np.ndarray,
    model_min: np.ndarray,
    model_max: np.ndarray,
    margin: float,
) -> PoseState:
    outward = -_norm(entry_direction)
    corners = np.array([
        [x, y, z]
        for x in (model_min[0], model_max[0])
        for y in (model_min[1], model_max[1])
        for z in (model_min[2], model_max[2])
    ])
    travel = float(np.max(corners @ outward) - np.min(bar.axis @ outward))
    turn_radius = float(np.max(np.linalg.norm(bar.axis - pivot, axis=1)))
    travel = max(margin, travel + margin) + turn_radius
    return PoseState(pivot + outward * travel, np.array([0.0, 0.0, 0.0, 1.0]))


def _rotation_between(source: np.ndarray, target: np.ndarray) -> Rotation:
    source = _norm(source, (1, 0, 0))
    target = _norm(target, (1, 0, 0))
    dot = float(np.clip(np.dot(source, target), -1.0, 1.0))
    if dot > 1.0 - 1e-10:
        return Rotation.identity()
    if dot < -1.0 + 1e-10:
        seed = np.array([1.0, 0.0, 0.0]) if abs(source[0]) < 0.8 else np.array([0.0, 1.0, 0.0])
        return Rotation.from_rotvec(_norm(np.cross(source, seed)) * math.pi)
    axis = _norm(np.cross(source, target))
    return Rotation.from_rotvec(axis * math.acos(dot))


def _smooth_path(
    states: list[PoseState],
    edge_check: Callable[[PoseState, PoseState], tuple[bool, Optional[dict], int, float]],
) -> list[PoseState]:
    if len(states) <= 2:
        return states
    result = [states[0]]
    index = 0
    while index < len(states) - 1:
        selected = index + 1
        for candidate in range(len(states) - 1, index, -1):
            if edge_check(states[index], states[candidate])[0]:
                selected = candidate
                break
        result.append(states[selected])
        index = selected
    return result


def _rrt_path(
    start: PoseState,
    goal: PoseState,
    iterations: int,
    rng: np.random.Generator,
    bounds_min: np.ndarray,
    bounds_max: np.ndarray,
    translation_step: float,
    rotation_step_rad: float,
    rotation_scale: float,
    edge_check: Callable[[PoseState, PoseState], tuple[bool, Optional[dict], int, float]],
) -> Optional[list[PoseState]]:
    nodes = [start]
    parents = [-1]
    for iteration in range(iterations):
        if iteration % 5 == 0:
            sample = goal
        else:
            sample = PoseState(
                rng.uniform(bounds_min, bounds_max),
                Rotation.random(random_state=rng).as_quat(),
            )
        distances = [
            float(np.linalg.norm(node.position - sample.position))
            + rotation_scale * _pose_angle(node, sample)
            for node in nodes
        ]
        nearest_index = int(np.argmin(distances))
        nearest = nodes[nearest_index]
        delta = sample.position - nearest.position
        linear = float(np.linalg.norm(delta))
        p = sample.position if linear <= translation_step else nearest.position + delta * (translation_step / linear)
        relative = nearest.rotation().inv() * sample.rotation()
        angle = float(np.linalg.norm(relative.as_rotvec()))
        if angle <= rotation_step_rad:
            q = sample.quaternion
        else:
            q = (nearest.rotation() * Rotation.from_rotvec(relative.as_rotvec() * (rotation_step_rad / angle))).as_quat()
        new_state = PoseState(np.asarray(p, dtype=float), np.asarray(q, dtype=float))
        if not edge_check(nearest, new_state)[0]:
            continue
        nodes.append(new_state)
        parents.append(nearest_index)
        new_index = len(nodes) - 1
        if edge_check(new_state, goal)[0]:
            path = [goal]
            cursor = new_index
            while cursor >= 0:
                path.append(nodes[cursor])
                cursor = parents[cursor]
            path.reverse()
            return _smooth_path(path, edge_check)
    return None


def _serialize_state(state: PoseState) -> dict:
    return {
        "position_mm": np.round(state.position, 5).tolist(),
        "quaternion_xyzw": np.round(state.quaternion, 9).tolist(),
    }


def _plan_one(
    world: CapsuleCollisionWorld,
    bar: Rebar,
    row: dict,
    model_min: np.ndarray,
    model_max: np.ndarray,
    model_center: np.ndarray,
    cfg: dict,
    rng: np.random.Generator,
) -> dict:
    step = int(row["installation_step"])
    pivot = _arc_point(bar.axis, float(cfg.get("grasp_fraction", 0.5)))
    goal = PoseState(pivot.copy(), np.array([0.0, 0.0, 0.0, 1.0]))
    translation_step = float(cfg.get("assembly_translation_step_mm", 75.0))
    rotation_step_rad = math.radians(float(cfg.get("assembly_rotation_step_deg", 7.5)))
    target_contacts = world.contacts(bar, bar.axis, step)
    pose_samples = 0
    minimum_clearance = math.inf
    first_hit: Optional[dict] = None

    def edge_check(a: PoseState, b: PoseState):
        nonlocal pose_samples, minimum_clearance, first_hit
        result = _edge_is_free(
            world, bar, pivot, a, b, step, target_contacts,
            translation_step, rotation_step_rad, goal,
        )
        pose_samples += result[2]
        minimum_clearance = min(minimum_clearance, result[3])
        if not result[0] and first_hit is None:
            first_hit = result[1]
        return result

    preferred = _norm(np.asarray(row.get("entry_direction", [0, 0, -1]), dtype=float), (0, 0, -1))
    directions = _direction_candidates(bar, preferred, model_center)
    margin = float(cfg.get("outside_margin_mm", 800.0))
    best_failed: Optional[list[PoseState]] = None

    for direction in directions:
        start = _outside_state(bar, pivot, direction, model_min, model_max, margin)
        direct = [start, goal]
        best_failed = best_failed or direct
        if edge_check(start, goal)[0]:
            return {
                "installation_step": step,
                "bar_index": bar.index,
                "status": "collision_free",
                "path_type": "straight_se3",
                "entry_direction": np.round(direction, 8).tolist(),
                "pivot_local_mm": np.round(pivot, 5).tolist(),
                "target_contact_bar_count": len(target_contacts),
                "checked_pose_count": pose_samples,
                "minimum_clearance_mm": None if math.isinf(minimum_clearance) else minimum_clearance,
                "control_poses": [_serialize_state(state) for state in direct],
            }

    # Deterministic curved paths rotate the rigid bar outside the cage, translate
    # through a dogleg, then blend back to the exact installed pose.
    principal = _norm(bar.axis[-1] - bar.axis[0], (1, 0, 0))
    for direction in directions[:8]:
        start = _outside_state(bar, pivot, direction, model_min, model_max, margin)
        base_rotation = _rotation_between(principal, direction)
        for angle_deg in (0.0, 45.0, -45.0, 90.0):
            object_rotation = Rotation.from_rotvec(direction * math.radians(angle_deg)) * base_rotation
            rotated_start = PoseState(start.position.copy(), object_rotation.as_quat())
            side_seed = np.array([0.0, 0.0, 1.0])
            if abs(float(np.dot(side_seed, direction))) > 0.85:
                side_seed = np.array([0.0, 1.0, 0.0])
            side = _norm(np.cross(direction, side_seed))
            bend = min(
                max(2.0 * translation_step, 4.0 * bar.radius),
                0.2 * float(np.linalg.norm(model_max - model_min)),
            )
            gate = PoseState(
                pivot - direction * max(2.0 * translation_step, margin * 0.25) + side * bend,
                object_rotation.as_quat(),
            )
            candidate = [start, rotated_start, gate, goal]
            if all(edge_check(a, b)[0] for a, b in zip(candidate[:-1], candidate[1:])):
                candidate = _smooth_path(candidate, edge_check)
                return {
                    "installation_step": step,
                    "bar_index": bar.index,
                    "status": "collision_free",
                    "path_type": "curved_rotating_se3",
                    "entry_direction": np.round(direction, 8).tolist(),
                    "pivot_local_mm": np.round(pivot, 5).tolist(),
                    "target_contact_bar_count": len(target_contacts),
                    "checked_pose_count": pose_samples,
                    "minimum_clearance_mm": None if math.isinf(minimum_clearance) else minimum_clearance,
                    "control_poses": [_serialize_state(state) for state in candidate],
                }

    iterations = int(cfg.get("assembly_rrt_iterations", 350))
    if iterations > 0:
        expansion = margin + float(np.max(np.linalg.norm(bar.axis - pivot, axis=1)))
        bounds_min = model_min - expansion
        bounds_max = model_max + expansion
        per_start = max(1, iterations // min(3, len(directions)))
        for direction in directions[:3]:
            start = _outside_state(bar, pivot, direction, model_min, model_max, margin)
            path = _rrt_path(
                start,
                goal,
                per_start,
                rng,
                bounds_min,
                bounds_max,
                max(translation_step, 2.0 * bar.radius),
                max(rotation_step_rad, math.radians(5.0)),
                max(translation_step, 0.2 * bar.length),
                edge_check,
            )
            if path:
                return {
                    "installation_step": step,
                    "bar_index": bar.index,
                    "status": "collision_free",
                    "path_type": "rrt_curved_rotating_se3",
                    "entry_direction": np.round(direction, 8).tolist(),
                    "pivot_local_mm": np.round(pivot, 5).tolist(),
                    "target_contact_bar_count": len(target_contacts),
                    "checked_pose_count": pose_samples,
                    "minimum_clearance_mm": None if math.isinf(minimum_clearance) else minimum_clearance,
                    "control_poses": [_serialize_state(state) for state in path],
                }

    failed_path = best_failed or [goal]
    return {
        "installation_step": step,
        "bar_index": bar.index,
        "status": "collision_detected",
        "path_type": "no_safe_path_found",
        "entry_direction": np.round(preferred, 8).tolist(),
        "pivot_local_mm": np.round(pivot, 5).tolist(),
        "target_contact_bar_count": len(target_contacts),
        "checked_pose_count": pose_samples,
        "minimum_clearance_mm": None if math.isinf(minimum_clearance) else minimum_clearance,
        "first_collision": first_hit,
        "control_poses": [_serialize_state(state) for state in failed_path],
    }


def plan_assembly_paths(
    out_dir: Path,
    rebars: list[Rebar],
    sequence: list[dict],
    cfg: dict,
    progress: Optional[Progress] = None,
) -> tuple[dict[int, dict], dict]:
    cb = progress or (lambda *_: None)
    out_dir.mkdir(parents=True, exist_ok=True)
    by_index = {bar.index: bar for bar in rebars}
    step_by_bar = {int(row["bar_index"]): int(row["installation_step"]) for row in sequence}
    preinstalled_rows = [row for row in sequence if bool(row.get("preinstalled", False))]
    pending_rows = [row for row in sequence if not bool(row.get("preinstalled", False))]
    model_min = np.min([bar.bbox_min for bar in rebars], axis=0)
    model_max = np.max([bar.bbox_max for bar in rebars], axis=0)
    model_center = (model_min + model_max) * 0.5
    world = CapsuleCollisionWorld(rebars, step_by_bar, float(cfg.get("clearance_mm", 1.0)))
    rng = np.random.default_rng(int(cfg.get("assembly_random_seed", 17)))
    paths: list[dict] = []
    by_bar: dict[int, dict] = {}

    for index, row in enumerate(pending_rows):
        path = _plan_one(
            world, by_index[int(row["bar_index"])], row,
            model_min, model_max, model_center, cfg, rng,
        )
        paths.append(path)
        by_bar[int(path["bar_index"])] = path
        row["entry_direction"] = path["entry_direction"]
        if index % max(1, len(pending_rows) // 40) == 0:
            cb(
                "collision",
                0.89 + 0.075 * index / max(1, len(pending_rows)),
                f"SE(3) collision checking {index + 1}/{len(pending_rows)}",
            )

    feasible = sum(path["status"] == "collision_free" for path in paths)
    failed = len(paths) - feasible
    path_types: dict[str, int] = {}
    for path in paths:
        path_types[path["path_type"]] = path_types.get(path["path_type"], 0) + 1
    summary = {
        "planner": "rigid_rebar_discrete_se3",
        "total_bar_count": len(sequence),
        "preinstalled_bar_count": len(preinstalled_rows),
        "simulated_bar_count": len(pending_rows),
        "bar_count": len(paths),
        "collision_free_count": feasible,
        "collision_detected_count": failed,
        "all_paths_collision_free": failed == 0,
        "path_type_distribution": path_types,
        "pose_check_count": world.pose_checks,
        "segment_pair_test_count": world.segment_tests,
        "translation_discretization_mm": float(cfg.get("assembly_translation_step_mm", 75.0)),
        "rotation_discretization_deg": float(cfg.get("assembly_rotation_step_deg", 7.5)),
        "collision_model": "centerline capsules; preinstalled bars are fixed step-0 obstacles; target-pose contacts retained",
        "rigid_body_dof": 6,
    }
    payload = {
        "units": "mm",
        "frame": "IFC model coordinates",
        "representation": "rigid bar pose at grasp pivot; quaternion order xyzw",
        "summary": summary,
        "paths": paths,
    }
    (out_dir / "assembly_paths.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    report = {
        "summary": summary,
        "failed_paths": [
            {
                "installation_step": path["installation_step"],
                "bar_index": path["bar_index"],
                "first_collision": path.get("first_collision"),
            }
            for path in paths
            if path["status"] != "collision_free"
        ],
    }
    (out_dir / "collision_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (out_dir / "assembly_path_waypoints.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        fields = [
            "installation_step", "bar_index", "path_status", "path_type", "waypoint",
            "x_mm", "y_mm", "z_mm", "qx", "qy", "qz", "qw",
        ]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for path in paths:
            for waypoint, pose in enumerate(path["control_poses"]):
                p = pose["position_mm"]
                q = pose["quaternion_xyzw"]
                writer.writerow({
                    "installation_step": path["installation_step"],
                    "bar_index": path["bar_index"],
                    "path_status": path["status"],
                    "path_type": path["path_type"],
                    "waypoint": waypoint,
                    "x_mm": p[0], "y_mm": p[1], "z_mm": p[2],
                    "qx": q[0], "qy": q[1], "qz": q[2], "qw": q[3],
                })
    cb("collision", 0.97, f"SE(3) collision check complete: {feasible}/{len(paths)} feasible")
    return by_bar, summary
