from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import numpy as np
from scipy.spatial.transform import Rotation, Slerp
from shapely.geometry import LineString, Point, box
from shapely.strtree import STRtree

from .assembly_path import _segment_distances_batch
from .ifc_geometry import Rebar, rebar_display_id


EPS = 1.0e-9
MISSING = object()


def _finite_number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _optional_finite(mapping: dict, key: str, label: str) -> Optional[float]:
    value = mapping.get(key, MISSING)
    if value is MISSING or value is None or value == "":
        return None
    result = _finite_number(value)
    if result is None:
        raise ValueError(f"{label}必须是有限数值")
    return result


def _strict_integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是整数")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是整数") from exc
    if not math.isfinite(numeric) or not numeric.is_integer():
        raise ValueError(f"{label}必须是整数")
    return int(numeric)


def _unit(value: Iterable[float], fallback: Iterable[float] | None = None) -> np.ndarray:
    vector = np.asarray(list(value), dtype=float)
    if vector.shape != (3,) or not np.all(np.isfinite(vector)):
        if fallback is None:
            raise ValueError("方向向量必须包含 3 个有限数值")
        vector = np.asarray(list(fallback), dtype=float)
    length = float(np.linalg.norm(vector))
    if length <= EPS:
        if fallback is None:
            raise ValueError("方向向量长度不能为 0")
        vector = np.asarray(list(fallback), dtype=float)
        length = float(np.linalg.norm(vector))
    return vector / max(length, EPS)


def _round_vector(value: np.ndarray, digits: int = 8) -> list[float]:
    return np.round(np.asarray(value, dtype=float), digits).tolist()


def rebar_model_fingerprint(rebars: list[Rebar]) -> str:
    """Return a stable IFC-rebar identity fingerprint.

    Deliberately avoid intermediate sampled vertices so changing the display
    simplification tolerance does not invalidate a visual grouping made from
    the same IFC entities.
    """
    rows = []
    for bar in sorted(rebars, key=lambda item: item.index):
        start = np.asarray(bar.axis[0], dtype=float)
        end = np.asarray(bar.axis[-1], dtype=float)
        endpoint_pair = sorted(
            [np.round(start, 1).tolist(), np.round(end, 1).tolist()]
        )
        # Endpoint geometry is insensitive to intermediate display resampling,
        # while still detecting placement/shape edits that keep IFC IDs/names.
        rows.append(
            [
                int(bar.index),
                int(bar.entity_id),
                str(bar.guid or ""),
                str(bar.name or ""),
                str(bar.tag or ""),
                int(bar.map_id),
                round(float(bar.radius), 4),
                endpoint_pair,
            ]
        )
    encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _deterministic_axis_sign(axis: np.ndarray) -> np.ndarray:
    axis = _unit(axis)
    dominant = int(np.argmax(np.abs(axis)))
    return -axis if axis[dominant] < 0.0 else axis


def _principal_longitudinal_axis(rebars: list[Rebar]) -> np.ndarray:
    points = np.concatenate([bar.axis for bar in rebars], axis=0)
    centered = points - np.mean(points, axis=0)
    covariance = centered.T @ centered / max(1, len(points))
    values, vectors = np.linalg.eigh(covariance)
    return _deterministic_axis_sign(vectors[:, int(np.argmax(values))])


def _coordinate_frame(rebars: list[Rebar], payload: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    requested = payload.get("longitudinal_axis")
    if isinstance(requested, str) and requested.strip().lower() == "auto":
        requested = None
    longitudinal = (
        _principal_longitudinal_axis(rebars)
        if requested in (None, [], {})
        else _deterministic_axis_sign(_unit(requested))
    )
    if "vertical_axis" in payload and payload.get("vertical_axis") not in (None, [], ""):
        vertical = _unit(payload["vertical_axis"])
    else:
        vertical = _unit((0.0, 0.0, 1.0))
    vertical = vertical - longitudinal * float(np.dot(vertical, longitudinal))
    if float(np.linalg.norm(vertical)) <= 1.0e-6:
        choices = np.eye(3)
        seed = choices[int(np.argmin(np.abs(choices @ longitudinal)))]
        vertical = seed - longitudinal * float(np.dot(seed, longitudinal))
    vertical = _unit(vertical)
    if float(np.dot(vertical, np.array([0.0, 0.0, 1.0]))) < 0.0:
        vertical = -vertical
    transverse = _unit(np.cross(vertical, longitudinal))
    vertical = _unit(np.cross(longitudinal, transverse))
    return longitudinal, transverse, vertical


def _segments_for_bars(bars: list[Rebar]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    starts: list[np.ndarray] = []
    ends: list[np.ndarray] = []
    lengths: list[float] = []
    owners: list[int] = []
    for bar in bars:
        for start, end in zip(bar.axis[:-1], bar.axis[1:]):
            length = float(np.linalg.norm(end - start))
            if length <= EPS:
                continue
            starts.append(np.asarray(start, dtype=float))
            ends.append(np.asarray(end, dtype=float))
            lengths.append(length)
            owners.append(int(bar.index))
    if not starts:
        raise ValueError("网片组没有可用于平面拟合的有效钢筋轴线段")
    return (
        np.asarray(starts, dtype=float),
        np.asarray(ends, dtype=float),
        np.asarray(lengths, dtype=float),
        np.asarray(owners, dtype=np.int32),
    )


def _line_from_weighted_points(points: np.ndarray, weights: np.ndarray) -> tuple[np.ndarray, float]:
    total = float(np.sum(weights))
    center = np.sum(points * weights[:, None], axis=0) / max(total, EPS)
    centered = points - center
    covariance = (centered * weights[:, None]).T @ centered / max(total, EPS)
    values, vectors = np.linalg.eigh(covariance)
    direction = vectors[:, int(np.argmax(values))]
    if float(np.linalg.norm(direction)) <= EPS:
        direction = np.array([1.0, 0.0])
    direction = direction / max(float(np.linalg.norm(direction)), EPS)
    normal = np.array([-direction[1], direction[0]], dtype=float)
    return normal, float(np.dot(normal, center))


def _candidate_lines(
    a2: np.ndarray,
    b2: np.ndarray,
    lengths: np.ndarray,
) -> list[tuple[np.ndarray, float]]:
    candidates: list[tuple[np.ndarray, float]] = []
    projected = b2 - a2
    projected_length = np.linalg.norm(projected, axis=1)
    usable = np.flatnonzero(projected_length > 1.0e-5)
    if len(usable):
        order = usable[np.argsort(lengths[usable])[::-1]]
        if len(order) > 384:
            order = order[np.linspace(0, len(order) - 1, 384).astype(int)]
        for index in order:
            direction = projected[index] / projected_length[index]
            normal = np.array([-direction[1], direction[0]], dtype=float)
            candidates.append((normal, float(np.dot(normal, 0.5 * (a2[index] + b2[index])))))

    endpoint_points = np.vstack([a2, b2])
    endpoint_weights = np.concatenate([lengths * 0.5, lengths * 0.5])
    candidates.append(_line_from_weighted_points(endpoint_points, endpoint_weights))

    # Deterministic point-pair hypotheses cover groups made only from bars that
    # are longitudinal and therefore project to points in the cross-section.
    mid = 0.5 * (a2 + b2)
    if len(mid) >= 2:
        sample_count = min(48, len(mid))
        sample_indices = np.linspace(0, len(mid) - 1, sample_count).astype(int)
        sampled = mid[sample_indices]
        for i in range(len(sampled)):
            for j in range(i + 1, len(sampled)):
                direction = sampled[j] - sampled[i]
                norm = float(np.linalg.norm(direction))
                if norm <= 1.0e-5:
                    continue
                direction /= norm
                normal = np.array([-direction[1], direction[0]], dtype=float)
                candidates.append((normal, float(np.dot(normal, 0.5 * (sampled[i] + sampled[j])))))
                if len(candidates) >= 768:
                    return candidates
    return candidates


def _select_contiguous_main_segments(
    bars: list[Rebar],
    normal: np.ndarray,
    offset: float,
    transverse: np.ndarray,
    vertical: np.ndarray,
    tolerance: float,
) -> tuple[dict[int, list[list[int]]], list[tuple[int, int]], float, int]:
    masks: dict[int, list[list[int]]] = {}
    selected: list[tuple[int, int]] = []
    selected_length = 0.0
    excluded_count = 0
    for bar in bars:
        points2 = np.column_stack([bar.axis @ transverse, bar.axis @ vertical])
        distances = np.abs(points2 @ normal - offset)
        segment_lengths = np.linalg.norm(np.diff(bar.axis, axis=0), axis=1)
        inlier = np.maximum(distances[:-1], distances[1:]) <= tolerance
        runs: list[tuple[float, int, int]] = []
        start: Optional[int] = None
        for index, accepted in enumerate(np.r_[inlier, False]):
            if accepted and start is None:
                start = index
            elif not accepted and start is not None:
                end = index
                runs.append((float(np.sum(segment_lengths[start:end])), start, end))
                start = None
        if runs:
            _, run_start, run_end = max(runs, key=lambda row: (row[0], row[2] - row[1]))
        else:
            segment_distance = 0.5 * (distances[:-1] + distances[1:])
            run_start = int(np.argmin(segment_distance))
            run_end = run_start + 1
        masks[int(bar.index)] = [[int(run_start), int(run_end)]]
        for segment_index in range(run_start, run_end):
            selected.append((int(bar.index), int(segment_index)))
            selected_length += float(segment_lengths[segment_index])
        excluded_count += max(0, len(segment_lengths) - (run_end - run_start))
    return masks, selected, selected_length, excluded_count


def _fit_group_plane(
    bars: list[Rebar],
    transverse: np.ndarray,
    vertical: np.ndarray,
) -> dict:
    starts, ends, lengths, _ = _segments_for_bars(bars)
    a2 = np.column_stack([starts @ transverse, starts @ vertical])
    b2 = np.column_stack([ends @ transverse, ends @ vertical])
    tolerance = max(20.0, 4.0 * float(np.median([bar.radius for bar in bars])))
    best: Optional[tuple[float, float, np.ndarray, float, np.ndarray]] = None
    for normal, offset in _candidate_lines(a2, b2, lengths):
        normal = normal / max(float(np.linalg.norm(normal)), EPS)
        endpoint_distance = np.maximum(
            np.abs(a2 @ normal - offset),
            np.abs(b2 @ normal - offset),
        )
        inlier = endpoint_distance <= tolerance
        score = float(np.sum(lengths[inlier]))
        residual = (
            float(np.average(endpoint_distance[inlier], weights=lengths[inlier]))
            if np.any(inlier)
            else math.inf
        )
        key = (score, -residual)
        if best is None or key > (best[0], best[1]):
            best = (score, -residual, normal, offset, inlier)
    if best is None:
        raise ValueError("无法为网片组生成平面候选")
    _, _, normal, offset, inlier = best

    # Refit twice with length-weighted inlier endpoints.
    for _ in range(2):
        if not np.any(inlier):
            break
        points = np.vstack([a2[inlier], b2[inlier]])
        weights = np.concatenate([lengths[inlier] * 0.5, lengths[inlier] * 0.5])
        normal, offset = _line_from_weighted_points(points, weights)
        endpoint_distance = np.maximum(
            np.abs(a2 @ normal - offset),
            np.abs(b2 @ normal - offset),
        )
        inlier = endpoint_distance <= tolerance

    masks, selected, selected_length, excluded_count = _select_contiguous_main_segments(
        bars, normal, offset, transverse, vertical, tolerance
    )
    bar_by_index = {bar.index: bar for bar in bars}
    selected_starts: list[np.ndarray] = []
    selected_ends: list[np.ndarray] = []
    selected_lengths: list[float] = []
    for bar_index, segment_index in selected:
        bar = bar_by_index[bar_index]
        start = bar.axis[segment_index]
        end = bar.axis[segment_index + 1]
        selected_starts.append(start)
        selected_ends.append(end)
        selected_lengths.append(float(np.linalg.norm(end - start)))
    ss = np.asarray(selected_starts, dtype=float)
    ee = np.asarray(selected_ends, dtype=float)
    sw = np.asarray(selected_lengths, dtype=float)
    selected2a = np.column_stack([ss @ transverse, ss @ vertical])
    selected2b = np.column_stack([ee @ transverse, ee @ vertical])
    points = np.vstack([selected2a, selected2b])
    weights = np.concatenate([sw * 0.5, sw * 0.5])
    normal, offset = _line_from_weighted_points(points, weights)
    residuals = np.concatenate(
        [np.abs(selected2a @ normal - offset), np.abs(selected2b @ normal - offset)]
    )
    residual = float(np.sqrt(np.average(residuals * residuals, weights=weights)))
    total_length = float(np.sum(lengths))
    ratio = selected_length / max(total_length, EPS)
    midpoint = 0.5 * (ss + ee)
    centroid = np.sum(midpoint * sw[:, None], axis=0) / max(float(np.sum(sw)), EPS)
    tangent2 = np.array([normal[1], -normal[0]], dtype=float)
    warnings: list[str] = []
    if ratio < 0.15:
        warnings.append("主体段占比低，请在三维预览中检查弯头识别结果")
    if residual > tolerance:
        warnings.append("网片平面残差超过拟合容差，请人工修正平面角度")
    return {
        "normal_2d": normal,
        "tangent_2d": tangent2,
        "offset_mm": float(offset),
        "centroid_mm": centroid,
        "tolerance_mm": float(tolerance),
        "rms_residual_mm": residual,
        "main_body_length_ratio": float(ratio),
        "confidence": float(np.clip(ratio * math.exp(-residual / max(tolerance, EPS)), 0.0, 1.0)),
        "main_body_segments": {str(key): value for key, value in masks.items()},
        "excluded_segment_count": int(excluded_count),
        "warnings": warnings,
    }


def _rotation_axis_point(
    centroid: np.ndarray,
    angle_rad: float,
    top_elevation: float,
    longitudinal: np.ndarray,
    transverse: np.ndarray,
    vertical: np.ndarray,
) -> np.ndarray:
    final2 = np.array([float(np.dot(centroid, transverse)), float(np.dot(centroid, vertical))])
    if abs(angle_rad) <= 1.0e-7:
        axis2 = final2
    else:
        c = math.cos(-angle_rad)
        s = math.sin(-angle_rad)
        rotation2 = np.array([[c, -s], [s, c]], dtype=float)
        desired2 = np.array([final2[0], float(top_elevation)], dtype=float)
        matrix = np.eye(2) - rotation2
        axis2 = np.linalg.solve(matrix, desired2 - rotation2 @ final2)
    longitudinal_coordinate = float(np.dot(centroid, longitudinal))
    return longitudinal * longitudinal_coordinate + transverse * axis2[0] + vertical * axis2[1]


def _serialize_pose(position: np.ndarray, quaternion: np.ndarray, label: str) -> dict:
    return {
        "label": label,
        "position_mm": _round_vector(position, 5),
        "quaternion_xyzw": _round_vector(quaternion, 9),
    }


def _normal_angle(longitudinal: np.ndarray, vertical: np.ndarray, normal: np.ndarray) -> float:
    angle = math.atan2(
        float(np.dot(longitudinal, np.cross(vertical, normal))),
        float(np.clip(np.dot(vertical, normal), -1.0, 1.0)),
    )
    if abs(abs(angle) - math.pi) <= 1.0e-7:
        return math.pi
    return angle


def resolve_mesh_groups(
    rebars: list[Rebar],
    payload: dict,
    model_fingerprint: str | None = None,
) -> dict:
    """Validate and resolve a visual mesh-group definition.

    Hooks are excluded only from plane fitting.  The returned poses always
    transform the original complete axes.
    """
    if not rebars:
        raise ValueError("IFC 模型中没有可用钢筋")
    if not isinstance(payload, dict) or payload.get("mode") != "mesh_groups":
        raise ValueError("网片组 JSON 的 mode 必须是 mesh_groups")
    if int(payload.get("schema_version", 0)) != 2:
        raise ValueError("网片组 JSON 的 schema_version 必须是 2")
    fingerprint = model_fingerprint or rebar_model_fingerprint(rebars)
    supplied_fingerprint = str(payload.get("model_fingerprint") or "").strip()
    if not supplied_fingerprint:
        raise ValueError("网片组缺少 IFC 模型指纹，请重新解析并分组")
    if supplied_fingerprint != fingerprint:
        raise ValueError("网片组来自不同的 IFC 模型，请重新解析并分组")

    raw_groups = payload.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValueError("至少需要一个非空钢筋网片组")
    by_index = {int(bar.index): bar for bar in rebars}
    expected = set(by_index)
    seen: dict[int, str] = {}
    group_ids: set[str] = set()
    steps: set[int] = set()
    validated: list[dict] = []
    for position, raw in enumerate(raw_groups, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {position} 个网片组格式无效")
        group_id = str(raw.get("group_id") or f"G{position:03d}").strip()
        if not group_id or group_id in group_ids:
            raise ValueError(f"网片组 ID 为空或重复: {group_id!r}")
        group_ids.add(group_id)
        step = _strict_integer(
            raw.get("installation_step", position),
            f"网片组 {group_id} 的安装顺序",
        )
        if step <= 0 or step in steps:
            raise ValueError(f"网片组 {group_id} 的安装顺序必须为唯一正整数")
        steps.add(step)
        raw_indices = raw.get("bar_indices")
        if not isinstance(raw_indices, list) or not raw_indices:
            raise ValueError(f"网片组 {group_id} 不能为空")
        indices: list[int] = []
        local: set[int] = set()
        for value in raw_indices:
            index = _strict_integer(value, f"网片组 {group_id} 的钢筋索引")
            if index not in expected:
                raise ValueError(f"网片组 {group_id} 包含 IFC 中不存在的钢筋索引 {index}")
            if index in local:
                raise ValueError(f"网片组 {group_id} 内重复包含钢筋 {index}")
            if index in seen:
                raise ValueError(f"钢筋 {index} 同时属于网片组 {seen[index]} 和 {group_id}")
            local.add(index)
            seen[index] = group_id
            indices.append(index)
        status_raw = str(
            raw.get("installation_status", raw.get("status", "pending"))
        ).strip().lower()
        installed_statuses = {"preinstalled", "installed", "已安装", "完成"}
        pending_statuses = {"pending", "not_installed", "uninstalled", "待安装", "未安装"}
        if status_raw not in installed_statuses | pending_statuses:
            raise ValueError(f"网片组 {group_id} 的安装状态无效: {status_raw!r}")
        explicit_preinstalled = raw.get("preinstalled", MISSING)
        if explicit_preinstalled is not MISSING and not isinstance(explicit_preinstalled, bool):
            raise ValueError(f"网片组 {group_id} 的 preinstalled 必须是布尔值")
        preinstalled = (
            (bool(explicit_preinstalled) if explicit_preinstalled is not MISSING else False)
            or status_raw in installed_statuses
        )
        validated.append(
            {
                "raw": raw,
                "group_id": group_id,
                "name": str(raw.get("name") or f"网片组 {position}"),
                "installation_step": step,
                "installation_status": "preinstalled" if preinstalled else "pending",
                "preinstalled": preinstalled,
                "bar_indices": indices,
            }
        )
    missing = sorted(expected - set(seen))
    if missing:
        preview = ", ".join(map(str, missing[:12]))
        suffix = "…" if len(missing) > 12 else ""
        raise ValueError(f"仍有 {len(missing)} 根钢筋未分组: {preview}{suffix}")

    validated.sort(key=lambda group: group["installation_step"])
    longitudinal, transverse, vertical = _coordinate_frame(rebars, payload)
    all_points = np.concatenate([bar.axis for bar in rebars], axis=0)
    model_center = np.median(all_points, axis=0)
    fits: list[dict] = []
    for group in validated:
        bars = [by_index[index] for index in group["bar_indices"]]
        fit = _fit_group_plane(bars, transverse, vertical)
        normal = fit["normal_2d"][0] * transverse + fit["normal_2d"][1] * vertical
        radial = fit["centroid_mm"] - model_center
        radial -= longitudinal * float(np.dot(radial, longitudinal))
        if float(np.dot(normal, radial)) < 0.0:
            normal = -normal
            fit["normal_2d"] = -fit["normal_2d"]
            fit["offset_mm"] = -float(fit["offset_mm"])
        automatic_angle = _normal_angle(longitudinal, vertical, _unit(normal))
        override = _optional_finite(
            group["raw"], "plane_angle_deg", f"网片组 {group['group_id']} 的最终平面角"
        )
        if override is not None and not -180.0 <= override <= 180.0:
            raise ValueError(f"网片组 {group['group_id']} 的最终平面角必须在 -180° 到 180° 之间")
        angle = automatic_angle if override is None else math.radians(override)
        chosen_normal = Rotation.from_rotvec(longitudinal * angle).apply(vertical)
        fit["automatic_plane_angle_deg"] = math.degrees(automatic_angle)
        fit["normal"] = _round_vector(normal)
        fit["chosen_normal"] = _round_vector(chosen_normal)
        fit["centroid_mm"] = _round_vector(fit["centroid_mm"], 5)
        fit["normal_2d"] = np.round(np.asarray(fit["normal_2d"], dtype=float), 9).tolist()
        fit["tangent_2d"] = np.round(np.asarray(fit["tangent_2d"], dtype=float), 9).tolist()
        fit["source"] = "manual" if override is not None else "automatic"
        fits.append({"fit": fit, "angle": angle, "centroid": np.asarray(fit["centroid_mm"], dtype=float)})

    top_override = _optional_finite(payload, "top_elevation_mm", "顶面标高")
    if top_override is None:
        top_candidates = [
            float(np.dot(item["centroid"], vertical))
            for item in fits
            if abs(math.degrees(item["angle"])) <= 20.0
        ]
        top_elevation = max(top_candidates) if top_candidates else float(np.quantile(all_points @ vertical, 0.99))
        top_source = "automatic"
    else:
        top_elevation = top_override
        top_source = "manual"
    default_clearance = _optional_finite(payload, "staging_clearance_mm", "默认初始抬高距离")
    if default_clearance is None:
        default_clearance = _optional_finite(
            payload, "default_staging_clearance_mm", "默认初始抬高距离"
        )
    default_clearance = 800.0 if default_clearance is None else default_clearance
    if default_clearance < 0.0:
        raise ValueError("网片初始抬高距离不能为负数")

    resolved_groups: list[dict] = []
    for group, fit_info in zip(validated, fits):
        raw = group.pop("raw")
        fit = fit_info["fit"]
        angle = float(fit_info["angle"])
        centroid = fit_info["centroid"]
        auto_axis = _rotation_axis_point(
            centroid, angle, top_elevation, longitudinal, transverse, vertical
        )
        axis_value = raw.get("rotation_axis", MISSING)
        if axis_value is MISSING or axis_value is None:
            raw_axis = {}
        elif not isinstance(axis_value, dict):
            raise ValueError(f"网片组 {group['group_id']} 的旋转轴必须是对象")
        else:
            raw_axis = axis_value
        direction_value = raw_axis.get("direction")
        if direction_value not in (None, [], ""):
            supplied_direction = _unit(direction_value)
            if abs(float(np.dot(supplied_direction, longitudinal))) < 0.999:
                raise ValueError(f"网片组 {group['group_id']} 的旋转轴必须平行箱梁纵向")
        point_value = raw_axis.get("point_mm", MISSING)
        axis_manually_positioned = False
        if point_value is not MISSING and point_value is not None and point_value != "":
            if not isinstance(point_value, (list, tuple)) or len(point_value) != 3:
                raise ValueError(f"网片组 {group['group_id']} 的旋转轴点必须包含 3 个有限数值")
            try:
                point = np.asarray(point_value, dtype=float)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"网片组 {group['group_id']} 的旋转轴点无效") from exc
            if not np.all(np.isfinite(point)):
                raise ValueError(f"网片组 {group['group_id']} 的旋转轴点无效")
            axis_manually_positioned = True
        else:
            transverse_value = _optional_finite(
                raw_axis, "transverse_mm", f"网片组 {group['group_id']} 的旋转轴横向坐标"
            )
            elevation_value = _optional_finite(
                raw_axis, "elevation_mm", f"网片组 {group['group_id']} 的旋转轴标高"
            )
            point = auto_axis.copy()
            if transverse_value is not None:
                point += transverse * (transverse_value - float(np.dot(point, transverse)))
            if elevation_value is not None:
                point += vertical * (elevation_value - float(np.dot(point, vertical)))
            axis_manually_positioned = transverse_value is not None or elevation_value is not None
        clearance = _optional_finite(
            raw, "staging_clearance_mm", f"网片组 {group['group_id']} 的初始抬高距离"
        )
        clearance = default_clearance if clearance is None else clearance
        if clearance < 0.0:
            raise ValueError(f"网片组 {group['group_id']} 的抬高距离不能为负数")
        transition_rotation = Rotation.from_rotvec(-longitudinal * angle)
        transition_quaternion = transition_rotation.as_quat()
        identity = np.array([0.0, 0.0, 0.0, 1.0])
        control_poses = [
            _serialize_pose(point + vertical * clearance, transition_quaternion, "水平初始态"),
            _serialize_pose(point, transition_quaternion, "顶面过渡态"),
            _serialize_pose(point, identity, "IFC 最终安装态"),
        ]
        bars = [by_index[index] for index in group["bar_indices"]]
        resolved_groups.append(
            {
                **group,
                "bim_ids": [rebar_display_id(bar) for bar in bars],
                "plane_angle_deg": float(math.degrees(angle)),
                "plane_fit": fit,
                "rotation_axis": {
                    "point_mm": _round_vector(point, 5),
                    "direction": _round_vector(longitudinal),
                    "transverse_mm": float(np.dot(point, transverse)),
                    "elevation_mm": float(np.dot(point, vertical)),
                    "source": "manual" if axis_manually_positioned else "automatic",
                },
                "pivot_local_mm": _round_vector(point, 5),
                "staging_clearance_mm": float(clearance),
                "phases": [
                    {"name": "vertical_descent", "label": "竖直下降", "from_pose": 0, "to_pose": 1},
                    {"name": "fixed_axis_rotation", "label": "绕纵轴旋转", "from_pose": 1, "to_pose": 2},
                ],
                "control_poses": control_poses,
            }
        )

    return {
        "mode": "mesh_groups",
        "schema_version": 2,
        "units": "mm",
        "model_fingerprint": fingerprint,
        "longitudinal_axis": _round_vector(longitudinal),
        "transverse_axis": _round_vector(transverse),
        "vertical_axis": _round_vector(vertical),
        "axes": {
            "longitudinal": _round_vector(longitudinal),
            "transverse": _round_vector(transverse),
            "vertical": _round_vector(vertical),
        },
        "top_elevation_mm": float(top_elevation),
        "top_elevation_source": top_source,
        "staging_clearance_mm": float(default_clearance),
        "group_count": len(resolved_groups),
        "groups": resolved_groups,
    }


def mesh_group_sequence(resolved: dict) -> list[dict]:
    return sorted(
        list(resolved.get("groups") or []),
        key=lambda group: int(group.get("installation_step", 0)),
    )


@dataclass
class _Pose:
    position: np.ndarray
    quaternion: np.ndarray

    def rotation(self) -> Rotation:
        return Rotation.from_quat(self.quaternion)


def _pose_from_json(value: dict) -> _Pose:
    return _Pose(
        np.asarray(value["position_mm"], dtype=float),
        np.asarray(value["quaternion_xyzw"], dtype=float),
    )


def _interpolate(a: _Pose, b: _Pose, fraction: float) -> _Pose:
    u = float(np.clip(fraction, 0.0, 1.0))
    rotations = Rotation.from_quat(np.stack([a.quaternion, b.quaternion]))
    rotation = Slerp([0.0, 1.0], rotations)([u])[0]
    return _Pose(a.position + u * (b.position - a.position), rotation.as_quat())


def _pose_angle(a: _Pose, b: _Pose) -> float:
    return float(np.linalg.norm((a.rotation().inv() * b.rotation()).as_rotvec()))


def _transformed_axis(bar: Rebar, pivot: np.ndarray, pose: _Pose) -> np.ndarray:
    return pose.rotation().apply(bar.axis - pivot) + pose.position


def _closest_segment_points(
    p0: np.ndarray, p1: np.ndarray, q0: np.ndarray, q1: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Closest points on two finite 3-D segments."""
    u = p1 - p0
    v = q1 - q0
    w = p0 - q0
    a = float(np.dot(u, u))
    b = float(np.dot(u, v))
    c = float(np.dot(v, v))
    d = float(np.dot(u, w))
    e = float(np.dot(v, w))
    denominator = a * c - b * b
    if a <= EPS and c <= EPS:
        return p0.copy(), q0.copy()
    if a <= EPS:
        s, t = 0.0, float(np.clip(e / max(c, EPS), 0.0, 1.0))
    elif c <= EPS:
        s, t = float(np.clip(-d / max(a, EPS), 0.0, 1.0)), 0.0
    else:
        s = float(np.clip((b * e - c * d) / denominator, 0.0, 1.0)) if abs(denominator) > EPS else 0.0
        t = float((b * s + e) / c)
        if t < 0.0:
            t = 0.0
            s = float(np.clip(-d / a, 0.0, 1.0))
        elif t > 1.0:
            t = 1.0
            s = float(np.clip((b - d) / a, 0.0, 1.0))
    return p0 + s * u, q0 + t * v


class _GroupCollisionWorld:
    def __init__(
        self,
        rebars: list[Rebar],
        group_by_bar: dict[int, str],
        rank_by_group: dict[str, int],
        clearance_mm: float,
    ) -> None:
        self.clearance_mm = float(clearance_mm)
        self.group_by_bar = group_by_bar
        self.rank_by_group = rank_by_group
        self.display_by_bar = {
            int(bar.index): rebar_display_id(bar) for bar in rebars
        }
        self.owner: list[int] = []
        self.owner_segment: list[int] = []
        self.owner_group: list[str] = []
        self.a: list[np.ndarray] = []
        self.b: list[np.ndarray] = []
        self.radius: list[float] = []
        geoms = []
        for bar in rebars:
            group_id = group_by_bar[int(bar.index)]
            for segment_index, (start, end) in enumerate(zip(bar.axis[:-1], bar.axis[1:])):
                self.owner.append(int(bar.index))
                self.owner_segment.append(int(segment_index))
                self.owner_group.append(group_id)
                self.a.append(np.asarray(start, dtype=float))
                self.b.append(np.asarray(end, dtype=float))
                self.radius.append(float(bar.radius))
                if float(np.linalg.norm(end[:2] - start[:2])) <= EPS:
                    geoms.append(Point(float(start[0]), float(start[1])))
                else:
                    geoms.append(LineString([start[:2], end[:2]]))
        self.owner_array = np.asarray(self.owner, dtype=np.int32)
        self.owner_segment_array = np.asarray(self.owner_segment, dtype=np.int32)
        self.a_array = np.asarray(self.a, dtype=float)
        self.b_array = np.asarray(self.b, dtype=float)
        self.radius_array = np.asarray(self.radius, dtype=float)
        self.rank_array = np.asarray(
            [rank_by_group[group_id] for group_id in self.owner_group], dtype=np.int32
        )
        self.max_radius = float(max(self.radius, default=0.0))
        self.tree = STRtree(geoms) if geoms else None
        self.pose_checks = 0
        self.segment_tests = 0

    def candidates(
        self,
        start: np.ndarray,
        end: np.ndarray,
        moving_radius: float,
        current_rank: int,
    ) -> np.ndarray:
        if self.tree is None:
            return np.empty(0, dtype=np.int64)
        pad = moving_radius + self.max_radius + max(0.0, self.clearance_mm)
        low = np.minimum(start[:2], end[:2]) - pad
        high = np.maximum(start[:2], end[:2]) + pad
        indices = np.asarray(
            self.tree.query(box(float(low[0]), float(low[1]), float(high[0]), float(high[1]))),
            dtype=np.int64,
        )
        if not len(indices):
            return indices
        indices = indices[self.rank_array[indices] < current_rank]
        if not len(indices):
            return indices
        threshold = moving_radius + self.radius_array[indices] + self.clearance_mm
        obstacle_low = np.minimum(self.a_array[indices, 2], self.b_array[indices, 2])
        obstacle_high = np.maximum(self.a_array[indices, 2], self.b_array[indices, 2])
        moving_low = min(float(start[2]), float(end[2]))
        moving_high = max(float(start[2]), float(end[2]))
        return indices[
            (obstacle_high + threshold >= moving_low)
            & (obstacle_low - threshold <= moving_high)
        ]

    def final_contacts(
        self,
        bars: list[Rebar],
        current_rank: int,
    ) -> dict[int, dict[tuple[int, int], float]]:
        contacts: dict[int, dict[tuple[int, int], float]] = {}
        for bar in bars:
            bar_contacts: dict[tuple[int, int], float] = {}
            for segment_index, (start, end) in enumerate(zip(bar.axis[:-1], bar.axis[1:])):
                indices = self.candidates(start, end, bar.radius, current_rank)
                if not len(indices):
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
                    if float(distance) < float(threshold) - 1.0e-7:
                        key = (int(segment_index), int(candidate))
                        bar_contacts[key] = min(bar_contacts.get(key, math.inf), float(distance))
            contacts[int(bar.index)] = bar_contacts
        return contacts

    def check(
        self,
        bars: list[Rebar],
        pivot: np.ndarray,
        pose: _Pose,
        current_rank: int,
        final_contacts: dict[int, dict[tuple[int, int], float]],
        allow_final_contacts: bool,
    ) -> tuple[bool, Optional[dict], float]:
        self.pose_checks += 1
        minimum_clearance = math.inf
        for bar in bars:
            axis = _transformed_axis(bar, pivot, pose)
            for segment_index, (start, end) in enumerate(zip(axis[:-1], axis[1:])):
                indices = self.candidates(start, end, bar.radius, current_rank)
                if not len(indices):
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
                for local in np.flatnonzero(clearances < -1.0e-7):
                    candidate = int(indices[int(local)])
                    obstacle = int(self.owner_array[candidate])
                    target = final_contacts.get(int(bar.index), {}).get(
                        (int(segment_index), candidate)
                    )
                    if (
                        allow_final_contacts
                        and target is not None
                        and float(distances[int(local)]) >= target - 0.5
                    ):
                        continue
                    moving_point, obstacle_point = _closest_segment_points(
                        start,
                        end,
                        self.a_array[candidate],
                        self.b_array[candidate],
                    )
                    return False, {
                        "moving_bar_index": int(bar.index),
                        "moving_bar_bim_id": self.display_by_bar[int(bar.index)],
                        "moving_group_id": self.group_by_bar[int(bar.index)],
                        "moving_segment": int(segment_index),
                        "obstacle_bar_index": obstacle,
                        "obstacle_bar_bim_id": self.display_by_bar[obstacle],
                        "obstacle_group_id": self.owner_group[candidate],
                        "obstacle_segment": int(self.owner_segment_array[candidate]),
                        "distance_mm": float(distances[int(local)]),
                        "required_mm": float(thresholds[int(local)]),
                        "penetration_mm": float(-clearances[int(local)]),
                        "moving_contact_point_mm": _round_vector(moving_point, 5),
                        "obstacle_contact_point_mm": _round_vector(obstacle_point, 5),
                        "collision_position_mm": _round_vector(0.5 * (moving_point + obstacle_point), 5),
                    }, minimum_clearance
        return True, None, minimum_clearance


def plan_mesh_group_paths(
    rebars: list[Rebar],
    resolved: dict,
    cfg: dict,
) -> dict:
    """Check the prescribed horizontal/drop/rotate path for every pending group."""
    by_index = {int(bar.index): bar for bar in rebars}
    groups = mesh_group_sequence(resolved)
    group_by_bar = {
        int(index): str(group["group_id"])
        for group in groups
        for index in group.get("bar_indices", [])
    }
    pending = [group for group in groups if not bool(group.get("preinstalled", False))]
    rank_by_group: dict[str, int] = {}
    for group in groups:
        if bool(group.get("preinstalled", False)):
            rank_by_group[str(group["group_id"])] = -1
    for rank, group in enumerate(pending):
        rank_by_group[str(group["group_id"])] = rank
    world = _GroupCollisionWorld(
        rebars,
        group_by_bar,
        rank_by_group,
        float(cfg.get("clearance_mm", 1.0)),
    )
    translation_step = float(cfg.get("assembly_translation_step_mm", 75.0))
    rotation_step = math.radians(float(cfg.get("assembly_rotation_step_deg", 7.5)))
    longitudinal = _unit(resolved.get("longitudinal_axis", [1.0, 0.0, 0.0]))
    paths: list[dict] = []
    blocked_by_failure: Optional[str] = None

    def base_path(group: dict) -> dict:
        return {
            "installation_step": int(group["installation_step"]),
            "group_id": group["group_id"],
            "name": group.get("name", ""),
            "bar_indices": [int(value) for value in group["bar_indices"]],
            "path_type": "vertical_drop_then_fixed_axis_rotation",
            "plane_angle_deg": float(group["plane_angle_deg"]),
            "rotation_axis": group["rotation_axis"],
            "pivot_local_mm": group["pivot_local_mm"],
            "staging_clearance_mm": float(group["staging_clearance_mm"]),
            "control_poses": group["control_poses"],
        }

    for rank, group in enumerate(pending):
        bars = [by_index[int(index)] for index in group["bar_indices"]]
        pivot = np.asarray(group["pivot_local_mm"], dtype=float)
        poses = [_pose_from_json(value) for value in group["control_poses"]]
        if blocked_by_failure is not None:
            paths.append(
                {
                    **base_path(group),
                    "status": "not_evaluated_due_to_prior_failure",
                    "blocked_by_group_id": blocked_by_failure,
                    "phases": [],
                    "target_contact_pair_count": 0,
                    "checked_pose_count": 0,
                    "minimum_clearance_mm": None,
                }
            )
            continue
        contacts = world.final_contacts(bars, rank)
        maximum_sweep_radius = 0.0
        for bar in bars:
            relative = bar.axis - pivot
            radial = relative - np.outer(relative @ longitudinal, longitudinal)
            maximum_sweep_radius = max(
                maximum_sweep_radius,
                float(np.max(np.linalg.norm(radial, axis=1))),
            )
        checked = 0
        minimum_clearance = math.inf
        first_collision: Optional[dict] = None
        phase_results: list[dict] = []
        phase_specs = [
            ("vertical_descent", "竖直下降", poses[0], poses[1]),
            ("fixed_axis_rotation", "绕纵轴旋转", poses[1], poses[2]),
        ]
        for phase_index, (phase_name, phase_label, start, end) in enumerate(phase_specs):
            distance = float(np.linalg.norm(end.position - start.position))
            angle = _pose_angle(start, end)
            sample_count = max(
                1,
                int(math.ceil(distance / max(translation_step, EPS))),
                int(math.ceil(angle / max(rotation_step, EPS))),
                int(math.ceil(maximum_sweep_radius * angle / max(translation_step, EPS))),
            )
            phase_free = True
            for sample_index in range(sample_count + 1):
                fraction = sample_index / sample_count
                pose = _interpolate(start, end, fraction)
                # Existing IFC design contacts are tolerated only when the
                # actual pose equals the final IFC pose.  This also handles a
                # zero-angle rotation phase without treating its duplicate goal
                # sample as an early collision.
                allow_contacts = (
                    float(np.linalg.norm(pose.position - poses[-1].position)) <= 1.0e-7
                    and _pose_angle(pose, poses[-1]) <= 1.0e-8
                )
                free, hit, clearance = world.check(
                    bars, pivot, pose, rank, contacts, allow_contacts
                )
                checked += 1
                minimum_clearance = min(minimum_clearance, clearance)
                if not free:
                    phase_free = False
                    first_collision = {
                        "phase": phase_name,
                        "phase_label": phase_label,
                        "sample_index": sample_index,
                        "sample_count": sample_count,
                        "path_fraction": fraction,
                        "animation_fraction": (phase_index + fraction) / len(phase_specs),
                        "collision_pose": _serialize_pose(
                            pose.position, pose.quaternion, "碰撞姿态"
                        ),
                        **(hit or {}),
                    }
                    break
            phase_results.append(
                {
                    "name": phase_name,
                    "label": phase_label,
                    "status": "collision_free" if phase_free else "collision_detected",
                    "sample_count": sample_count + 1,
                    "translation_mm": distance,
                    "rotation_deg": math.degrees(angle),
                }
            )
            if not phase_free:
                break
        status = "collision_free" if first_collision is None else "collision_detected"
        path = {
            **base_path(group),
            "status": status,
            "phases": phase_results,
            "target_contact_pair_count": int(sum(len(value) for value in contacts.values())),
            "checked_pose_count": checked,
            "minimum_clearance_mm": None if math.isinf(minimum_clearance) else minimum_clearance,
        }
        if first_collision is not None:
            path["first_collision"] = first_collision
            blocked_by_failure = str(group["group_id"])
        paths.append(path)

    collision_free = sum(path["status"] == "collision_free" for path in paths)
    failed = sum(path["status"] == "collision_detected" for path in paths)
    not_evaluated = sum(
        path["status"] == "not_evaluated_due_to_prior_failure" for path in paths
    )
    summary = {
        "planner": "rigid_mesh_group_prescribed_se3",
        "mesh_group_count": len(groups),
        "preinstalled_group_count": len(groups) - len(pending),
        "pending_group_count": len(pending),
        "simulated_group_count": collision_free + failed,
        "not_evaluated_group_count": not_evaluated,
        "collision_free_count": collision_free,
        "collision_detected_count": failed,
        "all_paths_collision_free": failed == 0 and not_evaluated == 0,
        "pose_check_count": world.pose_checks,
        "segment_pair_test_count": world.segment_tests,
        "translation_discretization_mm": translation_step,
        "rotation_discretization_deg": math.degrees(rotation_step),
        "collision_model": "complete centerline capsules; same-group contacts ignored; only preinstalled and earlier groups are obstacles",
        "rigid_body_dof": 6,
        "path_policy": "vertical descent then fixed longitudinal-axis rotation; no RRT fallback",
    }
    return {
        "units": "mm",
        "frame": resolved.get("axes", {}),
        "representation": "shared rigid-group pose at rotation-axis pivot; quaternion order xyzw",
        "summary": summary,
        "paths": paths,
    }
