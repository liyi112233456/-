from __future__ import annotations

import csv
import json
import math
from collections import defaultdict, deque
from pathlib import Path
from typing import Callable, Iterable, Optional

import numpy as np
from shapely.geometry import LineString, Point, box
from shapely.ops import nearest_points
from shapely.strtree import STRtree

from .ifc_geometry import Rebar, TypeAxis, rebar_display_id

Progress = Callable[[str, float, str], None]

AXIS_MAP = {"x": 0, "y": 1, "z": 2}


def _line_or_point(P2: np.ndarray):
    if len(P2) < 2 or float(np.ptp(P2, axis=0).max()) < 1e-9:
        return Point(float(P2[0, 0]), float(P2[0, 1]))
    return LineString(P2)


def _axis_value_at_projected_point(P: np.ndarray, axis: int, target: np.ndarray) -> float:
    transverse = [k for k in range(3) if k != axis]
    best = math.inf
    value = float(P[0, axis])
    for a, b in zip(P[:-1], P[1:]):
        u = b[transverse] - a[transverse]
        den = float(u @ u)
        t = 0.0 if den < 1e-14 else float(np.clip(((target - a[transverse]) @ u) / den, 0.0, 1.0))
        q = a[transverse] + t * u
        dist = float(np.linalg.norm(q - target))
        if dist < best:
            best = dist
            value = float(a[axis] + t * (b[axis] - a[axis]))
    return value


def build_coordinate_blockers(
    rebars: list[Rebar], axes: list[int], clearance_mm: float, progress: Optional[Progress] = None
) -> tuple[list[list[set[int]]], list[list[np.ndarray]], dict]:
    cb = progress or (lambda *_: None)
    n = len(rebars)
    blockers: list[list[set[int]]] = [[set() for _ in range(2 * len(axes))] for _ in range(n)]
    directions: list[list[np.ndarray]] = [[] for _ in range(n)]
    for i in range(n):
        for axis in axes:
            d = np.zeros(3); d[axis] = 1.0
            directions[i].extend([d.copy(), -d.copy()])

    pair_tests = 0
    projected_contacts = 0
    for aa, axis in enumerate(axes):
        transverse = [k for k in range(3) if k != axis]
        geoms = [_line_or_point(b.axis[:, transverse]) for b in rebars]
        tree = STRtree(geoms)
        max_radius = max(b.radius for b in rebars) + max(0.0, clearance_mm)
        for i, geom in enumerate(geoms):
            minx, miny, maxx, maxy = geom.bounds
            pad = rebars[i].radius + max_radius
            candidates = tree.query(box(minx - pad, miny - pad, maxx + pad, maxy + pad))
            for j_raw in candidates:
                j = int(j_raw)
                if j <= i:
                    continue
                pair_tests += 1
                threshold = max(0.05, rebars[i].radius + rebars[j].radius + clearance_mm)
                dist = float(geom.distance(geoms[j]))
                if dist >= threshold:
                    continue
                projected_contacts += 1
                pg, qg = nearest_points(geom, geoms[j])
                pxy = np.array([pg.x, pg.y])
                qxy = np.array([qg.x, qg.y])
                pi = _axis_value_at_projected_point(rebars[i].axis, axis, pxy)
                pj = _axis_value_at_projected_point(rebars[j].axis, axis, qxy)
                delta = pj - pi
                allowance = math.sqrt(max(0.0, threshold * threshold - dist * dist))
                kp = 2 * aa
                km = kp + 1
                eps = 1e-7
                if delta > eps and delta + eps >= allowance:
                    blockers[i][kp].add(j)
                if delta < -eps and -delta + eps >= allowance:
                    blockers[i][km].add(j)
                if -delta > eps and -delta + eps >= allowance:
                    blockers[j][kp].add(i)
                if -delta < -eps and delta + eps >= allowance:
                    blockers[j][km].add(i)
            if i % max(1, n // 20) == 0:
                axis_name = "XYZ"[axis]
                frac = (aa + i / max(1, n)) / max(1, len(axes))
                cb("topology", 0.54 + 0.24 * frac, f"构建 {axis_name} 向扫掠拓扑 {i}/{n}")
    return blockers, directions, {"pair_tests": pair_tests, "projected_contacts": projected_contacts}



def _closest_params_2d_batch(p0: np.ndarray, p1: np.ndarray, q0: np.ndarray, q1: np.ndarray):
    """Vectorized exact closest points for batches of 2-D segment pairs."""
    u = p1 - p0
    v = q1 - q0
    uu = np.einsum("ij,ij->i", u, u)
    vv = np.einsum("ij,ij->i", v, v)
    eps = 1e-18

    candidates_t = []
    candidates_s = []
    candidates_d2 = []

    def add(t, w):
        pp = p0 + t[:, None] * u
        qq = q0 + w[:, None] * v
        candidates_t.append(t)
        candidates_s.append(w)
        candidates_d2.append(np.einsum("ij,ij->i", pp - qq, pp - qq))

    # Interior/interior candidate.
    r = p0 - q0
    uv = np.einsum("ij,ij->i", u, v)
    ur = np.einsum("ij,ij->i", u, r)
    vr = np.einsum("ij,ij->i", v, r)
    den = uu * vv - uv * uv
    t = np.divide(uv * vr - ur * vv, den, out=np.zeros_like(den), where=np.abs(den) > eps)
    w = np.divide(uu * vr - uv * ur, den, out=np.zeros_like(den), where=np.abs(den) > eps)
    valid = (t >= 0) & (t <= 1) & (w >= 0) & (w <= 1) & (np.abs(den) > eps)
    t_int = np.where(valid, t, 0.0)
    w_int = np.where(valid, w, 0.0)
    pp = p0 + t_int[:, None] * u
    qq = q0 + w_int[:, None] * v
    d2 = np.einsum("ij,ij->i", pp - qq, pp - qq)
    d2[~valid] = np.inf
    candidates_t.append(t_int); candidates_s.append(w_int); candidates_d2.append(d2)

    # Each endpoint projected onto the opposite segment.
    w0 = np.clip(np.divide(np.einsum("ij,ij->i", p0 - q0, v), vv, out=np.zeros_like(vv), where=vv > eps), 0, 1)
    add(np.zeros_like(w0), w0)
    w1 = np.clip(np.divide(np.einsum("ij,ij->i", p1 - q0, v), vv, out=np.zeros_like(vv), where=vv > eps), 0, 1)
    add(np.ones_like(w1), w1)
    t0 = np.clip(np.divide(np.einsum("ij,ij->i", q0 - p0, u), uu, out=np.zeros_like(uu), where=uu > eps), 0, 1)
    add(t0, np.zeros_like(t0))
    t1 = np.clip(np.divide(np.einsum("ij,ij->i", q1 - p0, u), uu, out=np.zeros_like(uu), where=uu > eps), 0, 1)
    add(t1, np.ones_like(t1))

    stack = np.vstack(candidates_d2)
    choice = np.argmin(stack, axis=0)
    cols = np.arange(len(choice))
    t_all = np.vstack(candidates_t)[choice, cols]
    w_all = np.vstack(candidates_s)[choice, cols]
    d2_all = stack[choice, cols]
    return np.sqrt(np.maximum(d2_all, 0.0)), t_all, w_all


def build_segment_blockers_fast(
    rebars: list[Rebar], axes: list[int], clearance_mm: float, progress: Optional[Progress] = None
) -> tuple[list[list[set[int]]], list[list[np.ndarray]], dict]:
    """Fast projected-topology graph using segment STRtrees.

    Segment pairs generate candidate *bar pairs*.  For each bar pair and projection,
    only the globally nearest projected contact is retained.  This matches the
    topological meaning of a bar-to-bar blocking relation and avoids false cycles
    caused by counting many local contacts between the same two curved bars.
    """
    cb = progress or (lambda *_: None)
    n = len(rebars)
    blockers: list[list[set[int]]] = [[set() for _ in range(2 * len(axes))] for _ in range(n)]
    directions: list[list[np.ndarray]] = [[] for _ in range(n)]
    for i in range(n):
        for axis in axes:
            d = np.zeros(3); d[axis] = 1.0
            directions[i].extend([d.copy(), -d.copy()])

    owner = []
    A3 = []
    B3 = []
    for i, bar in enumerate(rebars):
        for a, b in zip(bar.axis[:-1], bar.axis[1:]):
            owner.append(i); A3.append(a); B3.append(b)
    owner = np.asarray(owner, dtype=np.int32)
    A3 = np.asarray(A3, dtype=float)
    B3 = np.asarray(B3, dtype=float)
    radii = np.asarray([b.radius for b in rebars], dtype=float)
    max_threshold = float(2 * radii.max() + max(clearance_mm, 0.0) + 1e-6)
    stats = {"segment_count": len(owner), "bulk_candidate_pairs": 0, "nearest_bar_contacts": 0, "directed_edges": 0}

    for aa, axis in enumerate(axes):
        transverse = [k for k in range(3) if k != axis]
        A2 = A3[:, transverse]
        B2 = B3[:, transverse]
        geoms = [Point(float(a[0]), float(a[1])) if np.linalg.norm(b-a) < 1e-10 else LineString([a, b]) for a, b in zip(A2, B2)]
        cb("topology", 0.54 + 0.24 * aa / max(1, len(axes)), f"{'XYZ'[axis]} 向线段空间索引：{len(geoms)} 条")
        tree = STRtree(geoms)
        pairs = tree.query(geoms, predicate="dwithin", distance=max_threshold)
        mask = pairs[0] < pairs[1]
        ia = pairs[0, mask].astype(np.int64, copy=False)
        ib = pairs[1, mask].astype(np.int64, copy=False)
        different = owner[ia] != owner[ib]
        ia = ia[different]; ib = ib[different]
        stats["bulk_candidate_pairs"] += int(len(ia))

        keys_all = []
        dist_all = []
        delta_all = []
        threshold_all = []
        batch_size = 250_000
        for batch_no, start in enumerate(range(0, len(ia), batch_size)):
            sa = ia[start:start+batch_size]
            sb = ib[start:start+batch_size]
            dist, t, u = _closest_params_2d_batch(A2[sa], B2[sa], A2[sb], B2[sb])
            oi = owner[sa].astype(np.int64)
            oj = owner[sb].astype(np.int64)
            threshold = radii[oi] + radii[oj] + clearance_mm
            contact = dist < threshold
            if np.any(contact):
                sa = sa[contact]; sb = sb[contact]
                dist = dist[contact]; t = t[contact]; u = u[contact]
                oi = oi[contact]; oj = oj[contact]; threshold = threshold[contact]
                pi = A3[sa, axis] + t * (B3[sa, axis] - A3[sa, axis])
                pj = A3[sb, axis] + u * (B3[sb, axis] - A3[sb, axis])
                delta = pj - pi
                lo = np.minimum(oi, oj)
                hi = np.maximum(oi, oj)
                norm_delta = np.where(oi <= oj, delta, -delta)  # depth(hi)-depth(lo)
                keys_all.append(lo * n + hi)
                dist_all.append(dist)
                delta_all.append(norm_delta)
                threshold_all.append(threshold)
            cb("topology", 0.54 + 0.24 * (aa + min(1.0, (start+batch_size)/max(1,len(ia)))) / max(1, len(axes)),
               f"{'XYZ'[axis]} 向精检 {min(start+batch_size,len(ia))}/{len(ia)}")

        if not keys_all:
            continue
        keys = np.concatenate(keys_all)
        dists = np.concatenate(dist_all)
        deltas = np.concatenate(delta_all)
        thresholds = np.concatenate(threshold_all)
        order = np.lexsort((dists, keys))
        keys_sorted = keys[order]
        first = np.r_[True, keys_sorted[1:] != keys_sorted[:-1]]
        sel = order[first]
        keys = keys[sel]; dists = dists[sel]; deltas = deltas[sel]; thresholds = thresholds[sel]
        stats["nearest_bar_contacts"] += int(len(keys))
        lo = keys // n
        hi = keys % n
        allowance = np.sqrt(np.maximum(0.0, thresholds * thresholds - dists * dists))
        pos = (deltas > 1e-7) & (deltas + 1e-7 >= allowance)
        neg = (deltas < -1e-7) & (-deltas + 1e-7 >= allowance)
        kp = 2 * aa; km = kp + 1
        for src, dst in zip(lo[pos], hi[pos]):
            blockers[int(src)][kp].add(int(dst))
            blockers[int(dst)][km].add(int(src))
        for src, dst in zip(lo[neg], hi[neg]):
            blockers[int(src)][km].add(int(dst))
            blockers[int(dst)][kp].add(int(src))
        stats["directed_edges"] += int(2 * (np.count_nonzero(pos) + np.count_nonzero(neg)))
    return blockers, directions, stats

def _exit_travel_mm(rebars: list[Rebar], bar_index: int, direction: np.ndarray) -> float:
    d = np.asarray(direction, dtype=float)
    d /= max(float(np.linalg.norm(d)), 1e-12)
    model_min = np.min([bar.bbox_min for bar in rebars], axis=0)
    model_max = np.max([bar.bbox_max for bar in rebars], axis=0)
    corners = np.array([
        [x, y, z]
        for x in (model_min[0], model_max[0])
        for y in (model_min[1], model_max[1])
        for z in (model_min[2], model_max[2])
    ])
    bar = rebars[bar_index]
    return max(0.0, float(np.max(corners @ d) - np.min(bar.axis @ d) + 2.0 * bar.radius))


def peel_without_forcing(
    blockers: list[list[set[int]]],
    rebars: list[Rebar],
    active_init: Optional[set[int]] = None,
    directions: Optional[list[list[np.ndarray]]] = None,
) -> tuple[list[int], list[int], set[int]]:
    active = set(range(len(rebars))) if active_init is None else set(active_init)
    counts: list[list[int]] = []
    reverse: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for i, candidates in enumerate(blockers):
        row: list[int] = []
        for k, blocked_by in enumerate(candidates):
            relevant = blocked_by & active
            row.append(len(relevant))
            for j in relevant:
                reverse[j].append((i, k))
        counts.append(row)
    queue = deque(i for i in active if blockers[i] and any(c == 0 for c in counts[i]))
    queued = set(queue)
    removal: list[int] = []
    chosen: list[int] = []
    while queue:
        i = queue.popleft()
        queued.discard(i)
        if i not in active:
            continue
        feasible = [k for k, count in enumerate(counts[i]) if count == 0]
        if not feasible:
            continue
        if directions and i < len(directions):
            k = min(
                feasible,
                key=lambda kk: (
                    _exit_travel_mm(rebars, i, directions[i][kk]),
                    kk,
                ),
            )
        else:
            k = min(feasible)
        active.remove(i)
        removal.append(i)
        chosen.append(k)
        for other, direction_index in reverse[i]:
            if other in active:
                counts[other][direction_index] -= 1
                if counts[other][direction_index] == 0 and other not in queued:
                    queue.append(other)
                    queued.add(other)
    return removal, chosen, active

def _closest_params_2d(p1: np.ndarray, p2: np.ndarray, q1: np.ndarray, q2: np.ndarray) -> tuple[float, float, float]:
    # Closest points between 2-D segments; return distance and segment parameters.
    u = p2 - p1; v = q2 - q1; w = p1 - q1
    a = float(u @ u); b = float(u @ v); c = float(v @ v); d = float(u @ w); e = float(v @ w)
    eps = 1e-12
    D = a * c - b * b
    sN = D; sD = D; tN = D; tD = D
    if D < eps:
        sN = 0.0; sD = 1.0; tN = e; tD = c
    else:
        sN = b * e - c * d; tN = a * e - b * d
        if sN < 0.0:
            sN = 0.0; tN = e; tD = c
        elif sN > sD:
            sN = sD; tN = e + b; tD = c
    if tN < 0.0:
        tN = 0.0
        if -d < 0.0: sN = 0.0
        elif -d > a: sN = sD
        else: sN = -d; sD = a
    elif tN > tD:
        tN = tD
        if -d + b < 0.0: sN = 0.0
        elif -d + b > a: sN = sD
        else: sN = -d + b; sD = a
    sc = 0.0 if abs(sN) < eps else sN / max(sD, eps)
    tc = 0.0 if abs(tN) < eps else tN / max(tD, eps)
    delta = w + sc * u - tc * v
    return float(np.linalg.norm(delta)), float(sc), float(tc)


def _orthonormal_plane(d: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    d = d / max(np.linalg.norm(d), 1e-12)
    seed = np.array([1.0, 0.0, 0.0]) if abs(d[0]) < 0.8 else np.array([0.0, 1.0, 0.0])
    e1 = np.cross(d, seed); e1 /= max(np.linalg.norm(e1), 1e-12)
    e2 = np.cross(d, e1)
    return e1, e2, d


def sweep_blocks_arbitrary(P: np.ndarray, Q: np.ndarray, d: np.ndarray, threshold: float) -> bool:
    e1, e2, d = _orthonormal_plane(d)
    P2 = np.column_stack([P @ e1, P @ e2])
    Q2 = np.column_stack([Q @ e1, Q @ e2])
    Pd = P @ d
    Qd = Q @ d
    threshold2 = threshold * threshold
    for i in range(len(P) - 1):
        for j in range(len(Q) - 1):
            dist, u, v = _closest_params_2d(P2[i], P2[i + 1], Q2[j], Q2[j + 1])
            if dist >= threshold:
                continue
            delta = (Qd[j] + v * (Qd[j + 1] - Qd[j])) - (Pd[i] + u * (Pd[i + 1] - Pd[i]))
            allowance = math.sqrt(max(0.0, threshold2 - dist * dist))
            if delta > 1e-7 and delta + 1e-7 >= allowance:
                return True
    return False


def core_candidate_directions(bar: Rebar, center: np.ndarray, dense: bool = False) -> list[np.ndarray]:
    raw: list[np.ndarray] = []
    for axis in range(3):
        d = np.zeros(3); d[axis] = 1.0
        raw.extend([d, -d])
    if len(bar.axis) > 1:
        for t in (bar.axis[1] - bar.axis[0], bar.axis[-1] - bar.axis[-2]):
            if np.linalg.norm(t) > 1e-9:
                raw.extend([t, -t])
        _, _, vh = np.linalg.svd(bar.axis - bar.axis.mean(0), full_matrices=False)
        raw.extend([vh[0], -vh[0]])
    radial = bar.axis.mean(0) - center
    if np.linalg.norm(radial) > 1e-9:
        raw.extend([radial, -radial])
    if dense:
        for x in (-1, 0, 1):
            for y in (-1, 0, 1):
                for z in (-1, 0, 1):
                    if x or y or z:
                        raw.append(np.array([x, y, z], dtype=float))
    dirs: list[np.ndarray] = []
    for d in raw:
        n = np.linalg.norm(d)
        if n < 1e-9:
            continue
        d = d / n
        if not any(float(d @ e) > 0.995 for e in dirs):
            dirs.append(d)
    return dirs


def build_core_blockers(rebars: list[Rebar], core: set[int], clearance_mm: float, dense: bool = False):
    center = np.mean([rebars[i].axis.mean(0) for i in core], axis=0)
    blockers: list[list[set[int]]] = [[] for _ in rebars]
    directions: list[list[np.ndarray]] = [[] for _ in rebars]
    core_list = sorted(core)
    for i in core_list:
        dirs = core_candidate_directions(rebars[i], center, dense=dense)
        directions[i] = dirs
        for d in dirs:
            B: set[int] = set()
            for j in core_list:
                if i == j:
                    continue
                threshold = max(0.05, rebars[i].radius + rebars[j].radius + clearance_mm)
                if sweep_blocks_arbitrary(rebars[i].axis, rebars[j].axis, d, threshold):
                    B.add(j)
            blockers[i].append(B)
    return blockers, directions


def plan_installation(
    rebars: list[Rebar], candidate_axes: list[str], clearance_mm: float, progress: Optional[Progress] = None
) -> tuple[list[dict], dict]:
    """Plan a removable 3-D topology and reverse it into an installation order.

    Each bar is first tested along every configured positive and negative world
    axis. Cyclic cores are retried with bar tangents, principal directions,
    radial directions and (for small cores) dense diagonal directions. The
    resulting removal motions are reversed to obtain installation approaches.
    """
    cb = progress or (lambda *_: None)
    if not rebars:
        return [], {
            "planner_mode": "multi_direction_spatial_topology",
            "sequence_source": "automatic",
            "strict_graph_feasible": True,
        }

    axes: list[int] = []
    for name in candidate_axes:
        axis = AXIS_MAP.get(str(name).lower())
        if axis is not None and axis not in axes:
            axes.append(axis)
    if not axes:
        axes = [2, 1, 0]

    cb("topology", 0.54, "Build projected blockers for all configured 3-D directions")
    blockers, directions, graph_stats = build_segment_blockers_fast(
        rebars, axes, clearance_mm, progress=cb
    )
    removal, chosen, core = peel_without_forcing(
        blockers, rebars, directions=directions
    )
    chosen_direction: dict[int, np.ndarray] = {
        bar_index: directions[bar_index][direction_index]
        for bar_index, direction_index in zip(removal, chosen)
    }
    initial_core_size = len(core)
    forced: set[int] = set()

    while core:
        if len(core) <= 64:
            cb(
                "topology",
                0.79,
                f"Resolve cyclic core with arbitrary directions: {len(core)} bars",
            )
            active_blockers, active_directions = build_core_blockers(
                rebars, core, clearance_mm, dense=len(core) <= 24
            )
        else:
            active_blockers, active_directions = blockers, directions

        peeled, selected, remaining = peel_without_forcing(
            active_blockers,
            rebars,
            active_init=core,
            directions=active_directions,
        )
        for bar_index, direction_index in zip(peeled, selected):
            chosen_direction[bar_index] = active_directions[bar_index][direction_index]
        removal.extend(peeled)
        core = remaining
        if not core:
            break

        # A topological cycle is not a proof that no curved SE(3) path exists.
        # Break it deterministically and let the downstream path planner certify
        # (or reject) the actual rigid-body motion.
        candidates: list[tuple[int, float, int, int]] = []
        for bar_index in sorted(core):
            for direction_index, blocked_by in enumerate(active_blockers[bar_index]):
                count = len(blocked_by & core)
                travel = _exit_travel_mm(
                    rebars, bar_index, active_directions[bar_index][direction_index]
                )
                candidates.append((count, travel, bar_index, direction_index))
        _, _, bar_index, direction_index = min(candidates)
        forced.add(bar_index)
        chosen_direction[bar_index] = active_directions[bar_index][direction_index]
        removal.append(bar_index)
        core.remove(bar_index)

    if len(removal) != len(rebars):
        raise RuntimeError(
            f"Topology planner returned {len(removal)} of {len(rebars)} bars"
        )

    sequence: list[dict] = []
    direction_counts: dict[str, int] = defaultdict(int)
    for step, bar_index in enumerate(reversed(removal), 1):
        bar = rebars[bar_index]
        entry = -chosen_direction[bar_index]
        entry /= max(float(np.linalg.norm(entry)), 1e-12)
        direction_key = ",".join(f"{value:.3f}" for value in entry)
        direction_counts[direction_key] += 1
        sequence.append({
            "installation_step": step,
            "bar_index": bar_index,
            "entity_id": bar.entity_id,
            "guid": bar.guid,
            "name": bar.name,
            "tag": bar.tag,
            "length_mm": bar.length,
            "radius_mm": bar.radius,
            "entry_direction": entry.tolist(),
            "forced_core_resolution": bar_index in forced,
            "preinstalled": False,
            "installation_status": "pending",
        })

    stats = {
        "planner_mode": "multi_direction_spatial_topology",
        "sequence_source": "automatic",
        "candidate_axes": ["XYZ"[axis] for axis in axes],
        "tested_direction_count": 2 * len(axes),
        "initial_core_size": initial_core_size,
        "forced_core_steps": len(forced),
        "strict_graph_feasible": not forced,
        "direction_distribution": dict(direction_counts),
        "graph": graph_stats,
        "certification_scope": (
            "capsule-axis projected sweep topology; actual path feasibility is "
            "reported by the downstream discrete SE(3) collision checker"
        ),
        "complexity": "segment spatial index plus bounded cyclic-core refinement",
    }
    cb("sequence", 0.88, f"Generated a multi-direction order for {len(sequence)} bars")
    return sequence, stats

def save_planning_outputs(
    out_dir: Path,
    rebars: list[Rebar],
    type_axes: dict[int, TypeAxis],
    sequence: list[dict],
    meta: dict,
    planner_stats: dict,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    axes_payload = {
        "units": "mm",
        "bars": [
            {
                "index": b.index,
                "entity_id": b.entity_id,
                "guid": b.guid,
                "name": b.name,
                "tag": b.tag,
                "map_id": b.map_id,
                "radius_mm": b.radius,
                "length_mm": b.length,
                "axis": np.round(b.axis, 5).tolist(),
            }
            for b in rebars
        ],
    }
    (out_dir / "rebar_axes.json").write_text(json.dumps(axes_payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    preinstalled_rows = [row for row in sequence if bool(row.get("preinstalled", False))]
    pending_rows = [row for row in sequence if not bool(row.get("preinstalled", False))]
    with (out_dir / "installation_sequence.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(["installation_step", "installation_status", "preinstalled", "bar_index", "entity_id", "guid", "name", "tag", "length_mm", "radius_mm", "entry_direction", "forced_core_resolution"])
        for row in sequence:
            writer.writerow([
                row["installation_step"], row.get("installation_status", "pending"), int(bool(row.get("preinstalled", False))),
                row["bar_index"], row["entity_id"], row["guid"], row["name"], row["tag"],
                f"{row['length_mm']:.5f}", f"{row['radius_mm']:.5f}", ";".join(f"{x:.8f}" for x in row["entry_direction"]),
                int(row["forced_core_resolution"]),
            ])
    # Browser-optimized combined payload.
    viewer = {
        "units": "mm",
        "bars": [
            {
                "i": b.index,
                "n": rebar_display_id(b),
                "r": round(b.radius, 4),
                "p": np.round(b.axis, 3).tolist(),
            }
            for b in rebars
        ],
        "initial_installed": [int(r["bar_index"]) for r in preinstalled_rows],
        "sequence": [{"i": r["bar_index"], "d": [round(x, 6) for x in r["entry_direction"]]} for r in pending_rows],
    }
    (out_dir / "viewer_model.json").write_text(json.dumps(viewer, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    bbox_min = np.min([b.bbox_min for b in rebars], axis=0)
    bbox_max = np.max([b.bbox_max for b in rebars], axis=0)
    summary = {
        **meta,
        "rebar_count": len(rebars),
        "preinstalled_bar_count": len(preinstalled_rows),
        "simulated_installation_bar_count": len(pending_rows),
        "type_count": len(type_axes),
        "axis_total_length_m": float(sum(b.length for b in rebars) / 1000.0),
        "diameter_mm": {
            "min": float(2 * min(b.radius for b in rebars)),
            "median": float(2 * np.median([b.radius for b in rebars])),
            "max": float(2 * max(b.radius for b in rebars)),
        },
        "bbox_mm": {"min": bbox_min.tolist(), "max": bbox_max.tolist()},
        "planner": planner_stats,
        "output_files": [
            "rebar_axes.json", "installation_sequence.csv", "viewer_model.json",
            *([
                "assembly_paths.json", "collision_report.json",
                "assembly_path_waypoints.csv",
            ] if (out_dir / "assembly_paths.json").is_file() else []),
        ],
    }
    (out_dir / "planning_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def save_mesh_group_outputs(
    out_dir: Path,
    rebars: list[Rebar],
    type_axes: dict[int, TypeAxis],
    meta: dict,
    resolved: dict,
    group_paths: dict,
) -> dict:
    """Write the additive group-mode artifacts without changing legacy formats."""
    out_dir.mkdir(parents=True, exist_ok=True)
    axes_payload = {
        "units": "mm",
        "bars": [
            {
                "index": b.index,
                "entity_id": b.entity_id,
                "guid": b.guid,
                "name": b.name,
                "tag": b.tag,
                "map_id": b.map_id,
                "radius_mm": b.radius,
                "length_mm": b.length,
                "axis": np.round(b.axis, 5).tolist(),
            }
            for b in rebars
        ],
    }
    (out_dir / "rebar_axes.json").write_text(
        json.dumps(axes_payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    (out_dir / "mesh_groups.json").write_text(
        json.dumps(resolved, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "mesh_group_paths.json").write_text(
        json.dumps(group_paths, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    groups = list(resolved.get("groups") or [])
    groups.sort(key=lambda group: int(group.get("installation_step", group.get("step", 0))))

    with (out_dir / "mesh_group_collisions.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as stream:
        writer = csv.writer(stream)
        writer.writerow([
            "installation_step", "group_id", "group_name", "phase", "phase_label",
            "moving_bar_index", "moving_bar_bim_id", "obstacle_group_id",
            "obstacle_bar_index", "obstacle_bar_bim_id",
            "maximum_collision_distance_mm", "axis_distance_mm",
            "required_distance_mm", "sample_hit_count", "first_animation_fraction",
            "last_animation_fraction", "worst_collision_position_mm",
            "worst_pose_position_mm", "worst_pose_quaternion_xyzw",
        ])
        for path in group_paths.get("paths", []):
            for collision in path.get("collisions", []):
                pose = collision.get("collision_pose") or {}
                writer.writerow([
                    path.get("installation_step", ""),
                    path.get("group_id", ""),
                    path.get("name", ""),
                    collision.get("phase", ""),
                    collision.get("phase_label", ""),
                    collision.get("moving_bar_index", ""),
                    collision.get("moving_bar_bim_id", ""),
                    collision.get("obstacle_group_id", ""),
                    collision.get("obstacle_bar_index", ""),
                    collision.get("obstacle_bar_bim_id", ""),
                    collision.get("maximum_collision_distance_mm", ""),
                    collision.get("axis_distance_mm", ""),
                    collision.get("required_distance_mm", ""),
                    collision.get("sample_hit_count", 0),
                    collision.get("first_animation_fraction", ""),
                    collision.get("last_animation_fraction", ""),
                    ";".join(str(value) for value in collision.get("collision_position_mm", [])),
                    ";".join(str(value) for value in pose.get("position_mm", [])),
                    ";".join(str(value) for value in pose.get("quaternion_xyzw", [])),
                ])

    def group_status(group: dict) -> str:
        if bool(group.get("preinstalled", False)):
            return "preinstalled"
        return str(group.get("installation_status", group.get("status", "pending")))

    def axis_point(group: dict) -> list[float]:
        axis = group.get("rotation_axis") or {}
        return [float(value) for value in axis.get("point_mm", [0.0, 0.0, 0.0])]

    def axis_direction(group: dict) -> list[float]:
        axis = group.get("rotation_axis") or {}
        return [float(value) for value in axis.get("direction", resolved.get("longitudinal_axis", [1.0, 0.0, 0.0]))]

    with (out_dir / "mesh_group_sequence.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as stream:
        writer = csv.writer(stream)
        writer.writerow([
            "installation_step", "installation_status", "preinstalled",
            "group_id", "group_name", "bar_count", "bar_indices", "bim_ids",
            "plane_angle_deg", "rotation_axis_point_mm",
            "rotation_axis_direction", "staging_clearance_mm",
        ])
        for group in groups:
            indices = [int(value) for value in group.get("bar_indices", [])]
            writer.writerow([
                int(group.get("installation_step", group.get("step", 0))),
                group_status(group),
                int(bool(group.get("preinstalled", False))),
                group.get("group_id", ""),
                group.get("name", ""),
                len(indices),
                ";".join(str(value) for value in indices),
                ";".join(str(value) for value in group.get("bim_ids", [])),
                group.get("plane_angle_deg", ""),
                ";".join(f"{value:.8f}" for value in axis_point(group)),
                ";".join(f"{value:.8f}" for value in axis_direction(group)),
                group.get("staging_clearance_mm", resolved.get("staging_clearance_mm", 800.0)),
            ])

    preinstalled_groups = [group for group in groups if bool(group.get("preinstalled", False))]
    pending_groups = [group for group in groups if not bool(group.get("preinstalled", False))]
    initial_installed = [
        int(index)
        for group in preinstalled_groups
        for index in group.get("bar_indices", [])
    ]

    def viewer_group(group: dict) -> dict:
        return {
            "group_id": group.get("group_id"),
            "name": group.get("name", ""),
            "installation_step": int(group.get("installation_step", group.get("step", 0))),
            "installation_status": group_status(group),
            "preinstalled": bool(group.get("preinstalled", False)),
            "bar_indices": [int(value) for value in group.get("bar_indices", [])],
            "plane_angle_deg": group.get("plane_angle_deg", 0.0),
            "plane_fit": group.get("plane_fit", {}),
            "rotation_axis": group.get("rotation_axis", {}),
            "pivot_local_mm": group.get("pivot_local_mm", [0.0, 0.0, 0.0]),
            "control_poses": group.get("control_poses", []),
            "phases": group.get("phases", []),
        }

    viewer_groups = [viewer_group(group) for group in groups]
    viewer = {
        "units": "mm",
        "assembly_unit": "mesh_group",
        "model_fingerprint": resolved.get("model_fingerprint"),
        "frame": group_paths.get("frame", resolved.get("axes", {})),
        "top_elevation_mm": resolved.get("top_elevation_mm"),
        "staging_clearance_mm": resolved.get("staging_clearance_mm", 800.0),
        "bars": [
            {
                "i": b.index,
                "n": rebar_display_id(b),
                "r": round(b.radius, 4),
                "p": np.round(b.axis, 3).tolist(),
            }
            for b in rebars
        ],
        "groups": viewer_groups,
        "initial_installed": initial_installed,
        "sequence": [viewer_group(group) for group in pending_groups],
        "group_paths": group_paths.get("paths", []),
    }
    (out_dir / "viewer_model.json").write_text(
        json.dumps(viewer, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    preinstalled_bar_count = len(initial_installed)
    bbox_min = np.min([b.bbox_min for b in rebars], axis=0)
    bbox_max = np.max([b.bbox_max for b in rebars], axis=0)
    collision_summary = group_paths.get("summary", {})
    planner_stats = {
        "planner_mode": "manual_visual_mesh_group_sequence",
        "sequence_source": "visual_groups",
        "group_count": len(groups),
        "preinstalled_group_count": len(preinstalled_groups),
        "pending_group_count": len(pending_groups),
        "strict_complete_unique_coverage": True,
        "certification_scope": (
            "user-defined rigid mesh groups; feasibility is evaluated only along "
            "the prescribed vertical-drop and fixed-axis rotation path"
        ),
    }
    summary = {
        **meta,
        "rebar_count": len(rebars),
        "preinstalled_bar_count": preinstalled_bar_count,
        "simulated_installation_bar_count": len(rebars) - preinstalled_bar_count,
        "mesh_group_count": len(groups),
        "preinstalled_mesh_group_count": len(preinstalled_groups),
        "pending_mesh_group_count": len(pending_groups),
        "simulated_mesh_group_count": int(collision_summary.get("simulated_group_count", len(pending_groups))),
        "not_evaluated_mesh_group_count": int(collision_summary.get("not_evaluated_group_count", 0)),
        "type_count": len(type_axes),
        "axis_total_length_m": float(sum(b.length for b in rebars) / 1000.0),
        "diameter_mm": {
            "min": float(2 * min(b.radius for b in rebars)),
            "median": float(2 * np.median([b.radius for b in rebars])),
            "max": float(2 * max(b.radius for b in rebars)),
        },
        "bbox_mm": {"min": bbox_min.tolist(), "max": bbox_max.tolist()},
        "planner": planner_stats,
        "assembly_collision": collision_summary,
        "robot": {
            "supported": False,
            "reason": "mesh_group_gripper_and_tcp_not_defined",
            "message": "钢筋网片组尚未定义多点夹具和 TCP，未生成 ABB、KUKA 或 UR 程序。",
        },
        "output_files": [
            "rebar_axes.json", "mesh_groups.json", "mesh_group_sequence.csv",
            "mesh_group_paths.json", "mesh_group_collisions.csv", "viewer_model.json",
        ],
    }
    (out_dir / "planning_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary
