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


def _resolve_assembly_rotation_axis(
    payload: dict,
    model_center: np.ndarray,
    longitudinal: np.ndarray,
    transverse: np.ndarray,
    vertical: np.ndarray,
) -> dict:
    """Resolve the single longitudinal axis shared by the cumulative cage."""
    value = payload.get("assembly_rotation_axis", MISSING)
    if value is MISSING or value is None:
        raw_axis: dict = {}
    elif not isinstance(value, dict):
        raise ValueError("累计钢筋笼旋转中轴必须是对象")
    else:
        raw_axis = value
    direction_value = raw_axis.get("direction")
    if direction_value not in (None, [], ""):
        supplied_direction = _unit(direction_value)
        if abs(float(np.dot(supplied_direction, longitudinal))) < 0.999:
            raise ValueError("累计钢筋笼旋转中轴必须平行箱梁纵向")
    point_value = raw_axis.get("point_mm", MISSING)
    manually_positioned = False
    if point_value is not MISSING and point_value is not None and point_value != "":
        if not isinstance(point_value, (list, tuple)) or len(point_value) != 3:
            raise ValueError("累计钢筋笼旋转中轴点必须包含 3 个有限数值")
        try:
            point = np.asarray(point_value, dtype=float)
        except (TypeError, ValueError) as exc:
            raise ValueError("累计钢筋笼旋转中轴点无效") from exc
        if not np.all(np.isfinite(point)):
            raise ValueError("累计钢筋笼旋转中轴点无效")
        manually_positioned = True
    else:
        transverse_value = _optional_finite(
            raw_axis, "transverse_mm", "累计钢筋笼旋转中轴横向坐标"
        )
        elevation_value = _optional_finite(
            raw_axis, "elevation_mm", "累计钢筋笼旋转中轴标高"
        )
        point = np.asarray(model_center, dtype=float).copy()
        if transverse_value is not None:
            point += transverse * (transverse_value - float(np.dot(point, transverse)))
        if elevation_value is not None:
            point += vertical * (elevation_value - float(np.dot(point, vertical)))
        manually_positioned = transverse_value is not None or elevation_value is not None
    return {
        "point_mm": _round_vector(point, 5),
        "direction": _round_vector(longitudinal),
        "transverse_mm": float(np.dot(point, transverse)),
        "elevation_mm": float(np.dot(point, vertical)),
        "source": "manual" if manually_positioned else "automatic_main_body_center",
    }


def _assembly_pose(
    axis_point: np.ndarray,
    longitudinal: np.ndarray,
    angle_rad: float,
    vertical: np.ndarray | None = None,
    lift_mm: float = 0.0,
    label: str = "",
) -> dict:
    position = np.asarray(axis_point, dtype=float).copy()
    if vertical is not None and abs(float(lift_mm)) > EPS:
        position += np.asarray(vertical, dtype=float) * float(lift_mm)
    quaternion = Rotation.from_rotvec(longitudinal * float(angle_rad)).as_quat()
    return _serialize_pose(position, quaternion, label)


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
    source_schema_version = _strict_integer(
        payload.get("schema_version", 0), "网片组 JSON 的 schema_version"
    )
    if source_schema_version not in {2, 3, 4}:
        raise ValueError("网片组 JSON 的 schema_version 必须是 2、3 或 4")
    if (
        source_schema_version == 3
        and payload.get("motion_model")
        != "cumulative_installed_rotation_then_pending_descent"
    ):
        raise ValueError(
            "版本 3 网片组的 motion_model 必须是 "
            "cumulative_installed_rotation_then_pending_descent"
        )
    if (
        source_schema_version == 4
        and payload.get("motion_model")
        != "pending_group_descent_then_cumulative_rotation"
    ):
        raise ValueError(
            "版本 4 网片组的 motion_model 必须是 "
            "pending_group_descent_then_cumulative_rotation"
        )
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
                "source_filename": str(raw.get("source_filename") or "").strip(),
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
            payload, "minimum_staging_clearance_mm", "默认最低抬高距离"
        )
    if default_clearance is None:
        default_clearance = _optional_finite(
            payload, "default_staging_clearance_mm", "默认初始抬高距离"
        )
    default_clearance = 800.0 if default_clearance is None else default_clearance
    if default_clearance < 0.0:
        raise ValueError("网片初始抬高距离不能为负数")

    main_body_points: list[np.ndarray] = []
    for group, fit_info in zip(validated, fits):
        masks = fit_info["fit"].get("main_body_segments", {})
        for index in group["bar_indices"]:
            bar = by_index[index]
            for start, end in masks.get(str(index), []):
                main_body_points.append(bar.axis[int(start):int(end) + 1])
    assembly_center = (
        np.median(np.concatenate(main_body_points, axis=0), axis=0)
        if main_body_points
        else model_center
    )
    assembly_axis = _resolve_assembly_rotation_axis(
        payload, assembly_center, longitudinal, transverse, vertical
    )
    assembly_axis_point = np.asarray(assembly_axis["point_mm"], dtype=float)

    resolved_groups: list[dict] = []
    for group, fit_info in zip(validated, fits):
        raw = group.pop("raw")
        fit = fit_info["fit"]
        angle = float(fit_info["angle"])
        clearance = _optional_finite(
            raw, "staging_clearance_mm", f"网片组 {group['group_id']} 的初始抬高距离"
        )
        if clearance is None:
            clearance = _optional_finite(
                raw, "minimum_staging_clearance_mm",
                f"网片组 {group['group_id']} 的最低抬高距离",
            )
        clearance = default_clearance if clearance is None else clearance
        if clearance < 0.0:
            raise ValueError(f"网片组 {group['group_id']} 的抬高距离不能为负数")
        assembly_angle = -angle
        control_poses = [
            _assembly_pose(
                assembly_axis_point, longitudinal, assembly_angle,
                vertical, clearance, "最低抬高水平初始态",
            ),
            _assembly_pose(
                assembly_axis_point, longitudinal, assembly_angle,
                label="当前装配角下的安装态",
            ),
        ]
        bars = [by_index[index] for index in group["bar_indices"]]
        resolved_groups.append(
            {
                **group,
                "bim_ids": [rebar_display_id(bar) for bar in bars],
                "plane_angle_deg": float(math.degrees(angle)),
                "assembly_angle_deg": float(math.degrees(assembly_angle)),
                "plane_fit": fit,
                "rotation_axis": dict(assembly_axis),
                "pivot_local_mm": list(assembly_axis["point_mm"]),
                "staging_clearance_mm": float(clearance),
                "minimum_staging_clearance_mm": float(clearance),
                "phases": [
                    {"name": "pending_group_descent", "label": "待安装网片竖直下降"},
                    {
                        "name": "installed_assembly_rotation_to_next",
                        "label": "累计已安装网片整体转向下一网片",
                    },
                ],
                "control_poses": control_poses,
            }
        )

    return {
        "mode": "mesh_groups",
        "schema_version": 4,
        "source_schema_version": source_schema_version,
        "migrated_from_schema_version": (
            source_schema_version if source_schema_version < 4 else None
        ),
        "motion_model": "pending_group_descent_then_cumulative_rotation",
        "input_mode": str(payload.get("input_mode") or "single_complete_ifc"),
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
        "assembly_rotation_axis": assembly_axis,
        "top_elevation_mm": float(top_elevation),
        "top_elevation_source": top_source,
        "top_elevation_controls_motion": False,
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
    ) -> tuple[bool, list[dict], float]:
        self.pose_checks += 1
        minimum_clearance = math.inf
        hits_by_bar_pair: dict[tuple[int, int], dict] = {}
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
                    hit = {
                        "moving_bar_index": int(bar.index),
                        "moving_bar_bim_id": self.display_by_bar[int(bar.index)],
                        "moving_group_id": self.group_by_bar[int(bar.index)],
                        "moving_segment": int(segment_index),
                        "obstacle_bar_index": obstacle,
                        "obstacle_bar_bim_id": self.display_by_bar[obstacle],
                        "obstacle_group_id": self.owner_group[candidate],
                        "obstacle_segment": int(self.owner_segment_array[candidate]),
                        "axis_distance_mm": float(distances[int(local)]),
                        "required_distance_mm": float(thresholds[int(local)]),
                        # Direct overlap distance of the two rebar capsules.
                        # A larger positive value means a deeper collision.
                        "collision_distance_mm": float(-clearances[int(local)]),
                        "moving_contact_point_mm": _round_vector(moving_point, 5),
                        "obstacle_contact_point_mm": _round_vector(obstacle_point, 5),
                        "collision_position_mm": _round_vector(0.5 * (moving_point + obstacle_point), 5),
                    }
                    key = (int(bar.index), obstacle)
                    previous = hits_by_bar_pair.get(key)
                    if previous is None or hit["collision_distance_mm"] > previous["collision_distance_mm"]:
                        hits_by_bar_pair[key] = hit
        hits = list(hits_by_bar_pair.values())
        return not hits, hits, minimum_clearance


class _FixedObstacleCollisionWorld:
    """Collision index for a v3 phase with fixed obstacle geometry."""

    def __init__(self, obstacles: list[dict], clearance_mm: float) -> None:
        self.clearance_mm = float(clearance_mm)
        self.owner: list[int] = []
        self.owner_segment: list[int] = []
        self.owner_group: list[str] = []
        self.a: list[np.ndarray] = []
        self.b: list[np.ndarray] = []
        self.radius: list[float] = []
        self.display_by_bar: dict[int, str] = {}
        geoms = []
        for entry in obstacles:
            bar: Rebar = entry["bar"]
            axis = np.asarray(entry["axis"], dtype=float)
            group_id = str(entry["group_id"])
            self.display_by_bar[int(bar.index)] = rebar_display_id(bar)
            for segment_index, (start, end) in enumerate(zip(axis[:-1], axis[1:])):
                self.owner.append(int(bar.index))
                self.owner_segment.append(int(segment_index))
                self.owner_group.append(group_id)
                self.a.append(np.asarray(start, dtype=float))
                self.b.append(np.asarray(end, dtype=float))
                self.radius.append(float(bar.radius))
                geoms.append(
                    Point(float(start[0]), float(start[1]))
                    if float(np.linalg.norm(end[:2] - start[:2])) <= EPS
                    else LineString([start[:2], end[:2]])
                )
        self.owner_array = np.asarray(self.owner, dtype=np.int32)
        self.owner_segment_array = np.asarray(self.owner_segment, dtype=np.int32)
        self.a_array = np.asarray(self.a, dtype=float).reshape((-1, 3))
        self.b_array = np.asarray(self.b, dtype=float).reshape((-1, 3))
        self.radius_array = np.asarray(self.radius, dtype=float)
        self.max_radius = float(max(self.radius, default=0.0))
        self.tree = STRtree(geoms) if geoms else None
        self.pose_checks = 0
        self.segment_tests = 0

    def candidates(self, start: np.ndarray, end: np.ndarray, radius: float) -> np.ndarray:
        if self.tree is None:
            return np.empty(0, dtype=np.int64)
        pad = radius + self.max_radius + max(0.0, self.clearance_mm)
        low = np.minimum(start[:2], end[:2]) - pad
        high = np.maximum(start[:2], end[:2]) + pad
        indices = np.asarray(
            self.tree.query(box(float(low[0]), float(low[1]), float(high[0]), float(high[1]))),
            dtype=np.int64,
        )
        if not len(indices):
            return indices
        threshold = radius + self.radius_array[indices] + self.clearance_mm
        obstacle_low = np.minimum(self.a_array[indices, 2], self.b_array[indices, 2])
        obstacle_high = np.maximum(self.a_array[indices, 2], self.b_array[indices, 2])
        moving_low = min(float(start[2]), float(end[2]))
        moving_high = max(float(start[2]), float(end[2]))
        return indices[
            (obstacle_high + threshold >= moving_low)
            & (obstacle_low - threshold <= moving_high)
        ]

    def final_contacts(self, moving: list[dict]) -> dict[tuple[int, int, int], float]:
        contacts: dict[tuple[int, int, int], float] = {}
        for entry in moving:
            bar: Rebar = entry["bar"]
            for segment_index, (start, end) in enumerate(
                zip(entry["axis"][:-1], entry["axis"][1:])
            ):
                indices = self.candidates(start, end, float(bar.radius))
                if not len(indices):
                    continue
                count = len(indices)
                distances = _segment_distances_batch(
                    np.repeat(start[None, :], count, axis=0),
                    np.repeat(end[None, :], count, axis=0),
                    self.a_array[indices],
                    self.b_array[indices],
                )
                thresholds = float(bar.radius) + self.radius_array[indices] + self.clearance_mm
                for candidate, distance, threshold in zip(indices, distances, thresholds):
                    if float(distance) < float(threshold) - 1.0e-7:
                        contacts[(int(bar.index), int(segment_index), int(candidate))] = float(distance)
        return contacts

    def check(
        self,
        moving: list[dict],
        final_contacts: dict[tuple[int, int, int], float] | None = None,
        allow_final_contacts: bool = False,
    ) -> tuple[bool, list[dict], float]:
        self.pose_checks += 1
        final_contacts = final_contacts or {}
        minimum_clearance = math.inf
        hits_by_pair: dict[tuple[int, int], dict] = {}
        for entry in moving:
            bar: Rebar = entry["bar"]
            moving_id = int(bar.index)
            moving_group_id = str(entry["group_id"])
            self.display_by_bar.setdefault(moving_id, rebar_display_id(bar))
            for segment_index, (start, end) in enumerate(
                zip(entry["axis"][:-1], entry["axis"][1:])
            ):
                indices = self.candidates(start, end, float(bar.radius))
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
                thresholds = float(bar.radius) + self.radius_array[indices] + self.clearance_mm
                clearances = distances - thresholds
                minimum_clearance = min(minimum_clearance, float(np.min(clearances)))
                for local in np.flatnonzero(clearances < -1.0e-7):
                    candidate = int(indices[int(local)])
                    target = final_contacts.get((moving_id, int(segment_index), candidate))
                    if (
                        allow_final_contacts
                        and target is not None
                        and float(distances[int(local)]) >= target - 0.5
                    ):
                        continue
                    obstacle = int(self.owner_array[candidate])
                    moving_point, obstacle_point = _closest_segment_points(
                        start, end, self.a_array[candidate], self.b_array[candidate]
                    )
                    hit = {
                        "moving_bar_index": moving_id,
                        "moving_bar_bim_id": self.display_by_bar[moving_id],
                        "moving_group_id": moving_group_id,
                        "moving_segment": int(segment_index),
                        "obstacle_bar_index": obstacle,
                        "obstacle_bar_bim_id": self.display_by_bar[obstacle],
                        "obstacle_group_id": self.owner_group[candidate],
                        "obstacle_segment": int(self.owner_segment_array[candidate]),
                        "axis_distance_mm": float(distances[int(local)]),
                        "required_distance_mm": float(thresholds[int(local)]),
                        "collision_distance_mm": float(-clearances[int(local)]),
                        "moving_contact_point_mm": _round_vector(moving_point, 5),
                        "obstacle_contact_point_mm": _round_vector(obstacle_point, 5),
                        "collision_position_mm": _round_vector(
                            0.5 * (moving_point + obstacle_point), 5
                        ),
                    }
                    key = (moving_id, obstacle)
                    old = hits_by_pair.get(key)
                    if old is None or hit["collision_distance_mm"] > old["collision_distance_mm"]:
                        hits_by_pair[key] = hit
        hits = list(hits_by_pair.values())
        return not hits, hits, minimum_clearance


def _pose_at_angle(
    pivot: np.ndarray,
    longitudinal: np.ndarray,
    angle_rad: float,
    vertical: np.ndarray | None = None,
    lift_mm: float = 0.0,
) -> _Pose:
    position = np.asarray(pivot, dtype=float).copy()
    if vertical is not None:
        position += np.asarray(vertical, dtype=float) * float(lift_mm)
    return _Pose(position, Rotation.from_rotvec(longitudinal * float(angle_rad)).as_quat())


def _entries_at_pose(
    group_ids: list[str],
    groups: dict[str, dict],
    bars: dict[int, Rebar],
    pivot: np.ndarray,
    pose: _Pose,
) -> list[dict]:
    return [
        {
            "bar": bars[int(index)],
            "group_id": group_id,
            "axis": _transformed_axis(bars[int(index)], pivot, pose),
        }
        for group_id in group_ids
        for index in groups[group_id].get("bar_indices", [])
    ]


def _rotation_samples(
    group_ids: list[str],
    groups: dict[str, dict],
    bars: dict[int, Rebar],
    pivot: np.ndarray,
    longitudinal: np.ndarray,
    angle: float,
    translation_step: float,
    rotation_step: float,
) -> int:
    radius = 0.0
    for group_id in group_ids:
        for index in groups[group_id].get("bar_indices", []):
            relative = bars[int(index)].axis - pivot
            radial = relative - np.outer(relative @ longitudinal, longitudinal)
            radius = max(radius, float(np.max(np.linalg.norm(radial, axis=1))))
    sweep = abs(float(angle))
    return max(
        1,
        int(math.ceil(sweep / max(rotation_step, EPS))),
        int(math.ceil(radius * sweep / max(translation_step, EPS))),
    )


def _v3_phase(
    *,
    name: str,
    label: str,
    moving_ids: list[str],
    obstacle_ids: list[str],
    groups: dict[str, dict],
    bars: dict[int, Rebar],
    pivot: np.ndarray,
    start: _Pose,
    end: _Pose,
    obstacle_pose: _Pose,
    sample_count: int,
    clearance_mm: float,
    allow_endpoint_contacts: bool,
    animation_offset: float,
    animation_span: float,
    collision_checked: bool = True,
) -> tuple[dict, list[dict], int, int]:
    control = [
        _serialize_pose(start.position, start.quaternion, "阶段起点"),
        _serialize_pose(end.position, end.quaternion, "阶段终点"),
    ]
    if not collision_checked:
        return {
            "name": name,
            "label": label,
            "status": "not_checked",
            "collision_checked": False,
            "omitted": False,
            "moving_group_ids": list(moving_ids),
            "obstacle_group_ids": list(obstacle_ids),
            "pivot_local_mm": _round_vector(pivot, 5),
            "start_pose": control[0],
            "end_pose": control[1],
            "control_poses": control,
            "sample_count": sample_count + 1,
            "translation_mm": float(np.linalg.norm(end.position - start.position)),
            "rotation_deg": math.degrees(_pose_angle(start, end)),
            "minimum_clearance_mm": None,
            "collision_pair_count": 0,
            "collision_sample_hit_count": 0,
            "maximum_collision_distance_mm": 0.0,
            "collisions": [],
        }, [], 0, 0
    obstacles = _entries_at_pose(obstacle_ids, groups, bars, pivot, obstacle_pose)
    world = _FixedObstacleCollisionWorld(obstacles, clearance_mm)
    endpoint = _entries_at_pose(moving_ids, groups, bars, pivot, end)
    contacts = world.final_contacts(endpoint) if allow_endpoint_contacts else {}
    records: dict[tuple[int, int], dict] = {}
    minimum_clearance = math.inf
    sample_hits = 0
    for sample_index in range(sample_count + 1):
        fraction = sample_index / max(sample_count, 1)
        pose = _interpolate(start, end, fraction)
        moving = _entries_at_pose(moving_ids, groups, bars, pivot, pose)
        _, hits, clearance = world.check(
            moving, contacts, allow_endpoint_contacts and sample_index == sample_count
        )
        minimum_clearance = min(minimum_clearance, clearance)
        sample_hits += len(hits)
        for hit in hits:
            observation = {
                "phase": name,
                "phase_label": label,
                "sample_index": sample_index,
                "sample_count": sample_count,
                "path_fraction": fraction,
                "animation_fraction": animation_offset + animation_span * fraction,
                "collision_pose": _serialize_pose(
                    pose.position, pose.quaternion, "碰撞姿态"
                ),
                **hit,
            }
            key = (int(hit["moving_bar_index"]), int(hit["obstacle_bar_index"]))
            old = records.get(key)
            if old is None:
                records[key] = {
                    **observation,
                    "first_sample_index": sample_index,
                    "first_path_fraction": fraction,
                    "first_animation_fraction": observation["animation_fraction"],
                    "last_sample_index": sample_index,
                    "last_path_fraction": fraction,
                    "last_animation_fraction": observation["animation_fraction"],
                    "sample_hit_count": 1,
                    "maximum_collision_distance_mm": float(hit["collision_distance_mm"]),
                }
            else:
                old["sample_hit_count"] += 1
                old["last_sample_index"] = sample_index
                old["last_path_fraction"] = fraction
                old["last_animation_fraction"] = observation["animation_fraction"]
                if hit["collision_distance_mm"] > old["maximum_collision_distance_mm"]:
                    saved = {
                        key: old[key] for key in (
                            "first_sample_index", "first_path_fraction",
                            "first_animation_fraction", "last_sample_index",
                            "last_path_fraction", "last_animation_fraction",
                            "sample_hit_count",
                        )
                    }
                    old.update(observation)
                    old.update(saved)
                    old["maximum_collision_distance_mm"] = float(hit["collision_distance_mm"])
    collisions = sorted(
        records.values(),
        key=lambda item: -float(item["maximum_collision_distance_mm"]),
    )
    return {
        "name": name,
        "label": label,
        "status": "collision_detected" if collisions else "collision_free",
        "collision_checked": True,
        "omitted": False,
        "moving_group_ids": list(moving_ids),
        "obstacle_group_ids": list(obstacle_ids),
        "pivot_local_mm": _round_vector(pivot, 5),
        "start_pose": control[0],
        "end_pose": control[1],
        "control_poses": control,
        "sample_count": sample_count + 1,
        "translation_mm": float(np.linalg.norm(end.position - start.position)),
        "rotation_deg": math.degrees(_pose_angle(start, end)),
        "minimum_clearance_mm": None if math.isinf(minimum_clearance) else minimum_clearance,
        "collision_pair_count": len(collisions),
        "collision_sample_hit_count": sample_hits,
        "maximum_collision_distance_mm": (
            float(collisions[0]["maximum_collision_distance_mm"]) if collisions else 0.0
        ),
        "collisions": collisions,
    }, collisions, world.pose_checks, world.segment_tests


def _plan_mesh_group_paths_v3(
    rebars: list[Rebar], resolved: dict, cfg: dict
) -> dict:
    bars = {int(bar.index): bar for bar in rebars}
    sequence = mesh_group_sequence(resolved)
    groups = {str(group["group_id"]): group for group in sequence}
    pending = [group for group in sequence if not bool(group.get("preinstalled", False))]
    installed = [
        str(group["group_id"]) for group in sequence if bool(group.get("preinstalled", False))
    ]
    longitudinal = _unit(resolved.get("longitudinal_axis", [1.0, 0.0, 0.0]))
    vertical = _unit(resolved.get("vertical_axis", [0.0, 0.0, 1.0]))
    axis = resolved.get("assembly_rotation_axis") or {}
    pivot = np.asarray(axis.get("point_mm", [0.0, 0.0, 0.0]), dtype=float)
    translation_step = max(EPS, float(cfg.get("assembly_translation_step_mm", 75.0)))
    rotation_step = max(
        EPS, math.radians(float(cfg.get("assembly_rotation_step_deg", 7.5)))
    )
    clearance_mm = float(cfg.get("clearance_mm", 1.0))
    collision_checked = not bool(cfg.get("preview_without_collision", False))
    current_angle = 0.0
    paths: list[dict] = []
    pose_checks = 0
    segment_tests = 0

    for group in pending:
        group_id = str(group["group_id"])
        installed_before = list(installed)
        target_angle = math.radians(
            float(group.get("assembly_angle_deg", -float(group["plane_angle_deg"])))
        )
        minimum_lift = float(
            group.get(
                "minimum_staging_clearance_mm",
                group.get("staging_clearance_mm", resolved.get("staging_clearance_mm", 800.0)),
            )
        )
        target_pose = _pose_at_angle(pivot, longitudinal, target_angle)
        pending_target = _entries_at_pose([group_id], groups, bars, pivot, target_pose)
        pending_bottom = min(
            float(np.min(entry["axis"] @ vertical)) - float(entry["bar"].radius)
            for entry in pending_target
        )
        swept_top: Optional[float] = None
        sweep_count = 0
        effective_lift = minimum_lift
        if installed_before:
            sweep_count = _rotation_samples(
                installed_before, groups, bars, pivot, longitudinal,
                target_angle - current_angle, translation_step, rotation_step,
            )
            swept_top_value = -math.inf
            sweep_start = _pose_at_angle(pivot, longitudinal, current_angle)
            for sample_index in range(sweep_count + 1):
                pose = _interpolate(sweep_start, target_pose, sample_index / sweep_count)
                for entry in _entries_at_pose(installed_before, groups, bars, pivot, pose):
                    swept_top_value = max(
                        swept_top_value,
                        float(np.max(entry["axis"] @ vertical)) + float(entry["bar"].radius),
                    )
            swept_top = float(swept_top_value)
            effective_lift = max(
                minimum_lift,
                swept_top + max(0.0, clearance_mm) - pending_bottom,
            )
        pending_high = _pose_at_angle(
            pivot, longitudinal, target_angle, vertical, effective_lift
        )
        assembly_start = _pose_at_angle(pivot, longitudinal, current_angle)
        phases: list[dict] = []
        collisions: list[dict] = []

        if installed_before:
            rotation_count = _rotation_samples(
                installed_before, groups, bars, pivot, longitudinal,
                target_angle - current_angle, translation_step, rotation_step,
            )
            phase, phase_collisions, checks, tests = _v3_phase(
                name="installed_assembly_rotation",
                label="累计已安装网片整体旋转",
                moving_ids=installed_before,
                obstacle_ids=[group_id],
                groups=groups,
                bars=bars,
                pivot=pivot,
                start=assembly_start,
                end=target_pose,
                obstacle_pose=pending_high,
                sample_count=rotation_count,
                clearance_mm=clearance_mm,
                allow_endpoint_contacts=False,
                animation_offset=0.0,
                animation_span=0.5,
                collision_checked=collision_checked,
            )
            phase["stationary_pending_pose"] = _serialize_pose(
                pending_high.position, pending_high.quaternion, "待安装网片高位悬停态"
            )
            phases.append(phase)
            collisions.extend(phase_collisions)
            pose_checks += checks
            segment_tests += tests
        else:
            omitted_pose = _serialize_pose(
                target_pose.position, target_pose.quaternion, "省略"
            )
            phases.append({
                "name": "installed_assembly_rotation",
                "label": "累计已安装网片整体旋转",
                "status": "not_applicable",
                "collision_checked": False,
                "omitted": True,
                "reason": "当前没有累计已安装网片",
                "moving_group_ids": [],
                "obstacle_group_ids": [group_id],
                "pivot_local_mm": _round_vector(pivot, 5),
                "start_pose": omitted_pose,
                "end_pose": omitted_pose,
                "control_poses": [],
                "stationary_pending_pose": _serialize_pose(
                    pending_high.position, pending_high.quaternion, "待安装网片高位悬停态"
                ),
                "sample_count": 0,
                "translation_mm": 0.0,
                "rotation_deg": 0.0,
                "collision_pair_count": 0,
                "collision_sample_hit_count": 0,
                "maximum_collision_distance_mm": 0.0,
                "collisions": [],
            })

        descent_count = max(1, int(math.ceil(effective_lift / translation_step)))
        descent, phase_collisions, checks, tests = _v3_phase(
            name="pending_group_descent",
            label="待安装网片竖直下降",
            moving_ids=[group_id],
            obstacle_ids=installed_before,
            groups=groups,
            bars=bars,
            pivot=pivot,
            start=pending_high,
            end=target_pose,
            obstacle_pose=target_pose,
            sample_count=descent_count,
            clearance_mm=clearance_mm,
            allow_endpoint_contacts=True,
            animation_offset=0.5 if installed_before else 0.0,
            animation_span=0.5 if installed_before else 1.0,
            collision_checked=collision_checked,
        )
        phases.append(descent)
        collisions.extend(phase_collisions)
        pose_checks += checks
        segment_tests += tests
        collisions.sort(key=lambda item: -float(item["maximum_collision_distance_mm"]))
        installed.append(group_id)
        path = {
            "installation_step": int(group["installation_step"]),
            "group_id": group_id,
            "name": group.get("name", ""),
            "bar_indices": [int(value) for value in group["bar_indices"]],
            "path_type": "cumulative_installed_rotation_then_pending_descent",
            "motion_model": "cumulative_installed_rotation_then_pending_descent",
            "assembly_rotation_axis": dict(axis),
            "pivot_local_mm": _round_vector(pivot, 5),
            "plane_angle_deg": float(group["plane_angle_deg"]),
            "assembly_angle_start_deg": math.degrees(current_angle),
            "assembly_angle_target_deg": math.degrees(target_angle),
            "installed_group_ids_before": installed_before,
            "installed_group_ids_after": list(installed),
            "minimum_staging_clearance_mm": minimum_lift,
            "effective_staging_clearance_mm": effective_lift,
            "automatic_lift_added_mm": max(0.0, effective_lift - minimum_lift),
            "installed_sweep_max_elevation_mm": swept_top,
            "pending_target_min_elevation_mm": pending_bottom,
            "sweep_envelope_sample_count": sweep_count + 1 if installed_before else 0,
            "control_poses": [
                _serialize_pose(
                    pending_high.position, pending_high.quaternion, "待安装网片高位初始态"
                ),
                _serialize_pose(target_pose.position, target_pose.quaternion, "下降完成安装态"),
            ],
            "phases": phases,
            "status": (
                "collision_detected"
                if collisions
                else ("collision_free" if collision_checked else "not_checked")
            ),
            "checked_pose_count": sum(
                int(phase.get("sample_count", 0))
                for phase in phases
                if bool(phase.get("collision_checked", False))
            ),
            "minimum_clearance_mm": min(
                (
                    float(phase["minimum_clearance_mm"])
                    for phase in phases
                    if phase.get("minimum_clearance_mm") is not None
                ),
                default=None,
            ),
            "collision_pair_count": len({
                (
                    str(item["moving_group_id"]), int(item["moving_bar_index"]),
                    str(item["obstacle_group_id"]), int(item["obstacle_bar_index"]),
                )
                for item in collisions
            }),
            "collision_record_count": len(collisions),
            "collision_sample_hit_count": sum(
                int(item["sample_hit_count"]) for item in collisions
            ),
            "maximum_collision_distance_mm": (
                float(collisions[0]["maximum_collision_distance_mm"]) if collisions else 0.0
            ),
            "collisions": collisions,
        }
        if collisions:
            path["first_collision"] = min(
                collisions, key=lambda item: float(item["first_animation_fraction"])
            )
            path["worst_collision"] = collisions[0]
        paths.append(path)
        current_angle = target_angle

    all_group_ids = [str(group["group_id"]) for group in sequence]
    restore_start = _pose_at_angle(pivot, longitudinal, current_angle)
    restore_end = _pose_at_angle(pivot, longitudinal, 0.0)
    restore_omitted = abs(current_angle) <= 1.0e-8
    restore_count = (
        0 if restore_omitted else _rotation_samples(
            all_group_ids, groups, bars, pivot, longitudinal, -current_angle,
            translation_step, rotation_step,
        )
    )
    final_restore = {
        "name": "final_restore_rotation",
        "label": "完整钢筋笼回正至 IFC 姿态",
        "status": "not_applicable" if restore_omitted else "animation_only",
        "collision_checked": False,
        "omitted": restore_omitted,
        "reason": (
            "末组安装后已处于 IFC 姿态"
            if restore_omitted else "最终整体回正按计划不做碰撞检测"
        ),
        "moving_group_ids": all_group_ids,
        "obstacle_group_ids": [],
        "pivot_local_mm": _round_vector(pivot, 5),
        "start_pose": _serialize_pose(
            restore_start.position, restore_start.quaternion, "整笼回正起点"
        ),
        "end_pose": _serialize_pose(
            restore_end.position, restore_end.quaternion, "IFC 最终姿态"
        ),
        "control_poses": [] if restore_omitted else [
            _serialize_pose(
                restore_start.position, restore_start.quaternion, "整笼回正起点"
            ),
            _serialize_pose(
                restore_end.position, restore_end.quaternion, "IFC 最终姿态"
            ),
        ],
        "sample_count": 0 if restore_omitted else restore_count + 1,
        "translation_mm": 0.0,
        "rotation_deg": 0.0 if restore_omitted else abs(math.degrees(current_angle)),
        "collision_pair_count": 0,
        "collision_sample_hit_count": 0,
        "maximum_collision_distance_mm": 0.0,
        "collisions": [],
    }
    all_collisions = [item for path in paths for item in path["collisions"]]
    collided = sum(path["status"] == "collision_detected" for path in paths)
    unique_pairs = {
        (
            item["moving_group_id"], item["moving_bar_index"],
            item["obstacle_group_id"], item["obstacle_bar_index"],
        )
        for item in all_collisions
    }
    summary = {
        "planner": "cumulative_rigid_mesh_group_prescribed_se3",
        "schema_version": 3,
        "mesh_group_count": len(sequence),
        "preinstalled_group_count": len(sequence) - len(pending),
        "pending_group_count": len(pending),
        "simulated_group_count": len(pending),
        "not_evaluated_group_count": 0,
        "collision_checked": collision_checked,
        "collision_free_count": len(paths) - collided if collision_checked else 0,
        "collision_detected_count": collided,
        "collision_pair_count": len(unique_pairs),
        "collision_record_count": len(all_collisions),
        "collision_sample_hit_count": sum(
            int(item["sample_hit_count"]) for item in all_collisions
        ),
        "maximum_collision_distance_mm": max(
            (float(item["maximum_collision_distance_mm"]) for item in all_collisions),
            default=0.0,
        ),
        "all_paths_collision_free": collided == 0 if collision_checked else None,
        "continued_after_collision": True,
        "pose_check_count": pose_checks,
        "segment_pair_test_count": segment_tests,
        "translation_discretization_mm": translation_step,
        "rotation_discretization_deg": math.degrees(rotation_step),
        "collision_model": "complete centerline capsules; installed groups move as one rigid assembly; internal contacts ignored",
        "rigid_body_dof": 6,
        "path_policy": "installed assembly rotates first; pending horizontal group only descends; final restore is animation-only",
    }
    return {
        "schema_version": 3,
        "units": "mm",
        "frame": resolved.get("axes", {}),
        "motion_model": "cumulative_installed_rotation_then_pending_descent",
        "assembly_rotation_axis": dict(axis),
        "representation": "one shared rigid transform per cumulative installed assembly; quaternion order xyzw",
        "summary": summary,
        "paths": paths,
        "final_restore": final_restore,
    }


def _plan_mesh_group_paths_v4(
    rebars: list[Rebar], resolved: dict, cfg: dict
) -> dict:
    """Plan horizontal descent followed by cumulative rotation to the next group."""
    bars = {int(bar.index): bar for bar in rebars}
    sequence = mesh_group_sequence(resolved)
    groups = {str(group["group_id"]): group for group in sequence}
    pending = [group for group in sequence if not bool(group.get("preinstalled", False))]
    initially_installed = [
        str(group["group_id"]) for group in sequence if bool(group.get("preinstalled", False))
    ]
    longitudinal = _unit(resolved.get("longitudinal_axis", [1.0, 0.0, 0.0]))
    vertical = _unit(resolved.get("vertical_axis", [0.0, 0.0, 1.0]))
    axis = resolved.get("assembly_rotation_axis") or {}
    pivot = np.asarray(axis.get("point_mm", [0.0, 0.0, 0.0]), dtype=float)
    translation_step = max(EPS, float(cfg.get("assembly_translation_step_mm", 75.0)))
    rotation_step = max(
        EPS, math.radians(float(cfg.get("assembly_rotation_step_deg", 7.5)))
    )
    clearance_mm = float(cfg.get("clearance_mm", 1.0))
    collision_checked = not bool(cfg.get("preview_without_collision", False))

    def assembly_angle(group: dict) -> float:
        return math.radians(
            float(group.get("assembly_angle_deg", -float(group["plane_angle_deg"])))
        )

    def wrapped_delta(start_angle: float, end_angle: float) -> float:
        return (float(end_angle) - float(start_angle) + math.pi) % (2.0 * math.pi) - math.pi

    def omitted_phase(
        *,
        name: str,
        label: str,
        reason: str,
        moving_ids: list[str],
        obstacle_ids: list[str],
        start: _Pose,
        end: _Pose,
    ) -> dict:
        return {
            "name": name,
            "label": label,
            "status": "not_applicable",
            "collision_checked": False,
            "omitted": True,
            "reason": reason,
            "moving_group_ids": list(moving_ids),
            "obstacle_group_ids": list(obstacle_ids),
            "pivot_local_mm": _round_vector(pivot, 5),
            "start_pose": _serialize_pose(start.position, start.quaternion, "省略阶段起点"),
            "end_pose": _serialize_pose(end.position, end.quaternion, "省略阶段终点"),
            "control_poses": [],
            "sample_count": 0,
            "translation_mm": 0.0,
            "rotation_deg": 0.0,
            "minimum_clearance_mm": None,
            "collision_pair_count": 0,
            "collision_sample_hit_count": 0,
            "maximum_collision_distance_mm": 0.0,
            "collisions": [],
        }

    def solve_staging(
        group: dict,
        installed_ids: list[str],
        rotation_start_angle: float,
    ) -> dict:
        group_id = str(group["group_id"])
        target_angle = assembly_angle(group)
        target_pose = _pose_at_angle(pivot, longitudinal, target_angle)
        target_entries = _entries_at_pose([group_id], groups, bars, pivot, target_pose)
        pending_bottom = min(
            float(np.min(entry["axis"] @ vertical)) - float(entry["bar"].radius)
            for entry in target_entries
        )
        minimum_lift = float(
            group.get(
                "minimum_staging_clearance_mm",
                group.get(
                    "staging_clearance_mm",
                    resolved.get("staging_clearance_mm", 800.0),
                ),
            )
        )
        effective_lift = minimum_lift
        swept_top: Optional[float] = None
        sweep_count = 0
        if installed_ids:
            delta = wrapped_delta(rotation_start_angle, target_angle)
            sweep_count = _rotation_samples(
                installed_ids,
                groups,
                bars,
                pivot,
                longitudinal,
                delta,
                translation_step,
                rotation_step,
            )
            sweep_start = _pose_at_angle(pivot, longitudinal, rotation_start_angle)
            swept_top_value = -math.inf
            for sample_index in range(sweep_count + 1):
                pose = _interpolate(
                    sweep_start, target_pose, sample_index / max(sweep_count, 1)
                )
                for entry in _entries_at_pose(
                    installed_ids, groups, bars, pivot, pose
                ):
                    swept_top_value = max(
                        swept_top_value,
                        float(np.max(entry["axis"] @ vertical))
                        + float(entry["bar"].radius),
                    )
            swept_top = float(swept_top_value)
            effective_lift = max(
                minimum_lift,
                swept_top + max(0.0, clearance_mm) - pending_bottom,
            )
        high_pose = _pose_at_angle(
            pivot, longitudinal, target_angle, vertical, effective_lift
        )
        return {
            "target_angle": target_angle,
            "target_pose": target_pose,
            "high_pose": high_pose,
            "minimum_lift": minimum_lift,
            "effective_lift": effective_lift,
            "automatic_lift": max(0.0, effective_lift - minimum_lift),
            "pending_bottom": pending_bottom,
            "swept_top": swept_top,
            "sweep_count": sweep_count,
            "rotation_start_angle": rotation_start_angle,
        }

    # A group's high pose is governed by the cumulative cage rotation immediately
    # before its descent.  Solving all stages first lets the preceding path expose
    # the exact stationary pose of its next pending group.
    staging: dict[str, dict] = {}
    installed_for_stage = list(initially_installed)
    rotation_start_angle = 0.0
    for group in pending:
        group_id = str(group["group_id"])
        staging[group_id] = solve_staging(
            group, installed_for_stage, rotation_start_angle
        )
        installed_for_stage.append(group_id)
        rotation_start_angle = float(staging[group_id]["target_angle"])

    pose_checks = 0
    segment_tests = 0
    initial_collisions: list[dict] = []
    first_group = pending[0] if pending else None
    if first_group is None:
        neutral = _pose_at_angle(pivot, longitudinal, 0.0)
        initial_preparation = omitted_phase(
            name="initial_preparation_rotation",
            label="初始已安装网片转向首片装配角",
            reason="没有待安装网片",
            moving_ids=initially_installed,
            obstacle_ids=[],
            start=neutral,
            end=neutral,
        )
        initial_preparation.update({
            "target_group_id": None,
            "assembly_angle_start_deg": 0.0,
            "assembly_angle_target_deg": 0.0,
            "stationary_pending_pose": None,
        })
    else:
        first_id = str(first_group["group_id"])
        first_stage = staging[first_id]
        preparation_start = _pose_at_angle(pivot, longitudinal, 0.0)
        preparation_end = first_stage["target_pose"]
        preparation_delta = wrapped_delta(0.0, first_stage["target_angle"])
        if not initially_installed:
            initial_preparation = omitted_phase(
                name="initial_preparation_rotation",
                label="初始已安装网片转向首片装配角",
                reason="没有初始已安装网片，首片直接水平下降",
                moving_ids=[],
                obstacle_ids=[first_id],
                start=preparation_end,
                end=preparation_end,
            )
        elif abs(preparation_delta) <= 1.0e-8:
            initial_preparation = omitted_phase(
                name="initial_preparation_rotation",
                label="初始已安装网片转向首片装配角",
                reason="初始已安装网片已处于首片装配角",
                moving_ids=initially_installed,
                obstacle_ids=[first_id],
                start=preparation_start,
                end=preparation_end,
            )
        else:
            preparation_samples = _rotation_samples(
                initially_installed,
                groups,
                bars,
                pivot,
                longitudinal,
                preparation_delta,
                translation_step,
                rotation_step,
            )
            initial_preparation, initial_collisions, checks, tests = _v3_phase(
                name="initial_preparation_rotation",
                label="初始已安装网片转向首片装配角",
                moving_ids=initially_installed,
                obstacle_ids=[first_id],
                groups=groups,
                bars=bars,
                pivot=pivot,
                start=preparation_start,
                end=preparation_end,
                obstacle_pose=first_stage["high_pose"],
                sample_count=preparation_samples,
                clearance_mm=clearance_mm,
                allow_endpoint_contacts=False,
                animation_offset=0.0,
                animation_span=1.0,
                collision_checked=collision_checked,
            )
            pose_checks += checks
            segment_tests += tests
        initial_preparation.update({
            "target_group_id": first_id,
            "assembly_angle_start_deg": 0.0,
            "assembly_angle_target_deg": math.degrees(first_stage["target_angle"]),
            "stationary_pending_pose": _serialize_pose(
                first_stage["high_pose"].position,
                first_stage["high_pose"].quaternion,
                "首个待安装网片高位悬停态",
            ),
            "minimum_staging_clearance_mm": first_stage["minimum_lift"],
            "effective_staging_clearance_mm": first_stage["effective_lift"],
            "automatic_lift_added_mm": first_stage["automatic_lift"],
            "installed_sweep_max_elevation_mm": first_stage["swept_top"],
            "pending_target_min_elevation_mm": first_stage["pending_bottom"],
            "sweep_envelope_sample_count": (
                first_stage["sweep_count"] + 1 if initially_installed else 0
            ),
        })

    installed = list(initially_installed)
    paths: list[dict] = []
    for pending_index, group in enumerate(pending):
        group_id = str(group["group_id"])
        stage = staging[group_id]
        target_angle = float(stage["target_angle"])
        target_pose = stage["target_pose"]
        pending_high = stage["high_pose"]
        installed_before = list(installed)
        next_group = (
            pending[pending_index + 1] if pending_index + 1 < len(pending) else None
        )
        next_id = str(next_group["group_id"]) if next_group is not None else None
        phases: list[dict] = []
        collisions: list[dict] = []

        descent_count = max(
            1, int(math.ceil(float(stage["effective_lift"]) / translation_step))
        )
        descent, phase_collisions, checks, tests = _v3_phase(
            name="pending_group_descent",
            label="待安装网片竖直下降",
            moving_ids=[group_id],
            obstacle_ids=installed_before,
            groups=groups,
            bars=bars,
            pivot=pivot,
            start=pending_high,
            end=target_pose,
            obstacle_pose=target_pose,
            sample_count=descent_count,
            clearance_mm=clearance_mm,
            allow_endpoint_contacts=True,
            animation_offset=0.0,
            animation_span=0.5 if next_group is not None else 1.0,
            collision_checked=collision_checked,
        )
        phases.append(descent)
        collisions.extend(phase_collisions)
        pose_checks += checks
        segment_tests += tests

        installed.append(group_id)
        installed_after_descent = list(installed)
        next_stage = staging[next_id] if next_id is not None else None
        if next_stage is not None:
            next_angle = float(next_stage["target_angle"])
            rotation_delta = wrapped_delta(target_angle, next_angle)
            if abs(rotation_delta) <= 1.0e-8:
                rotation = omitted_phase(
                    name="installed_assembly_rotation_to_next",
                    label="累计已安装网片整体转向下一网片",
                    reason="累计钢筋笼已处于下一网片装配角",
                    moving_ids=installed_after_descent,
                    obstacle_ids=[next_id],
                    start=target_pose,
                    end=next_stage["target_pose"],
                )
            else:
                rotation_count = _rotation_samples(
                    installed_after_descent,
                    groups,
                    bars,
                    pivot,
                    longitudinal,
                    rotation_delta,
                    translation_step,
                    rotation_step,
                )
                rotation, phase_collisions, checks, tests = _v3_phase(
                    name="installed_assembly_rotation_to_next",
                    label="累计已安装网片整体转向下一网片",
                    moving_ids=installed_after_descent,
                    obstacle_ids=[next_id],
                    groups=groups,
                    bars=bars,
                    pivot=pivot,
                    start=target_pose,
                    end=next_stage["target_pose"],
                    obstacle_pose=next_stage["high_pose"],
                    sample_count=rotation_count,
                    clearance_mm=clearance_mm,
                    allow_endpoint_contacts=False,
                    animation_offset=0.5,
                    animation_span=0.5,
                    collision_checked=collision_checked,
                )
                collisions.extend(phase_collisions)
                pose_checks += checks
                segment_tests += tests
            rotation["stationary_pending_pose"] = _serialize_pose(
                next_stage["high_pose"].position,
                next_stage["high_pose"].quaternion,
                "下一待安装网片高位悬停态",
            )
            rotation["next_group_id"] = next_id
            phases.append(rotation)
        else:
            next_angle = None
            rotation = omitted_phase(
                name="installed_assembly_rotation_to_next",
                label="累计已安装网片整体转向下一网片",
                reason="当前网片为最后一个待安装网片",
                moving_ids=installed_after_descent,
                obstacle_ids=[],
                start=target_pose,
                end=target_pose,
            )
            rotation["stationary_pending_pose"] = None
            rotation["next_group_id"] = None
            phases.append(rotation)

        collisions.sort(
            key=lambda item: -float(item["maximum_collision_distance_mm"])
        )
        control_poses = [
            _serialize_pose(
                pending_high.position, pending_high.quaternion, "待安装网片高位初始态"
            ),
            _serialize_pose(
                target_pose.position, target_pose.quaternion, "竖直下降完成安装态"
            ),
        ]
        if next_stage is not None:
            control_poses.append(
                _serialize_pose(
                    next_stage["target_pose"].position,
                    next_stage["target_pose"].quaternion,
                    "累计整体转至下一网片装配角",
                )
            )
        path = {
            "installation_step": int(group["installation_step"]),
            "group_id": group_id,
            "name": group.get("name", ""),
            "bar_indices": [int(value) for value in group["bar_indices"]],
            "path_type": "pending_group_descent_then_cumulative_rotation",
            "motion_model": "pending_group_descent_then_cumulative_rotation",
            "assembly_rotation_axis": dict(axis),
            "pivot_local_mm": _round_vector(pivot, 5),
            "plane_angle_deg": float(group["plane_angle_deg"]),
            "installed_group_ids_before": installed_before,
            "installed_group_ids_after_descent": installed_after_descent,
            "installed_group_ids_after": installed_after_descent,
            "next_group_id": next_id,
            "current_assembly_angle_deg": math.degrees(target_angle),
            "next_assembly_angle_deg": (
                math.degrees(next_angle) if next_angle is not None else None
            ),
            "assembly_angle_start_deg": math.degrees(target_angle),
            "assembly_angle_target_deg": math.degrees(target_angle),
            "assembly_angle_after_step_deg": (
                math.degrees(next_angle)
                if next_angle is not None
                else math.degrees(target_angle)
            ),
            "minimum_staging_clearance_mm": stage["minimum_lift"],
            "effective_staging_clearance_mm": stage["effective_lift"],
            "automatic_lift_added_mm": stage["automatic_lift"],
            "installed_sweep_max_elevation_mm": stage["swept_top"],
            "pending_target_min_elevation_mm": stage["pending_bottom"],
            "sweep_envelope_sample_count": (
                stage["sweep_count"] + 1 if installed_before else 0
            ),
            "pending_high_pose": control_poses[0],
            "pending_target_pose": control_poses[1],
            "next_pending_high_pose": (
                _serialize_pose(
                    next_stage["high_pose"].position,
                    next_stage["high_pose"].quaternion,
                    "下一待安装网片高位悬停态",
                )
                if next_stage is not None
                else None
            ),
            "next_minimum_staging_clearance_mm": (
                next_stage["minimum_lift"] if next_stage is not None else None
            ),
            "next_effective_staging_clearance_mm": (
                next_stage["effective_lift"] if next_stage is not None else None
            ),
            "next_automatic_lift_added_mm": (
                next_stage["automatic_lift"] if next_stage is not None else None
            ),
            "next_installed_sweep_max_elevation_mm": (
                next_stage["swept_top"] if next_stage is not None else None
            ),
            "next_sweep_envelope_sample_count": (
                next_stage["sweep_count"] + 1 if next_stage is not None else 0
            ),
            "control_poses": control_poses,
            "phases": phases,
            "status": (
                "collision_detected"
                if collisions
                else ("collision_free" if collision_checked else "not_checked")
            ),
            "checked_pose_count": sum(
                int(phase.get("sample_count", 0))
                for phase in phases
                if bool(phase.get("collision_checked", False))
            ),
            "minimum_clearance_mm": min(
                (
                    float(phase["minimum_clearance_mm"])
                    for phase in phases
                    if phase.get("minimum_clearance_mm") is not None
                ),
                default=None,
            ),
            "collision_pair_count": len({
                (
                    str(item["moving_group_id"]),
                    int(item["moving_bar_index"]),
                    str(item["obstacle_group_id"]),
                    int(item["obstacle_bar_index"]),
                )
                for item in collisions
            }),
            "collision_record_count": len(collisions),
            "collision_sample_hit_count": sum(
                int(item["sample_hit_count"]) for item in collisions
            ),
            "maximum_collision_distance_mm": (
                float(collisions[0]["maximum_collision_distance_mm"])
                if collisions
                else 0.0
            ),
            "collisions": collisions,
        }
        if collisions:
            path["first_collision"] = min(
                collisions, key=lambda item: float(item["first_animation_fraction"])
            )
            path["worst_collision"] = collisions[0]
        paths.append(path)

    final_angle = (
        float(staging[str(pending[-1]["group_id"])]["target_angle"])
        if pending
        else 0.0
    )
    all_group_ids = [str(group["group_id"]) for group in sequence]
    restore_start = _pose_at_angle(pivot, longitudinal, final_angle)
    restore_end = _pose_at_angle(pivot, longitudinal, 0.0)
    restore_delta = wrapped_delta(final_angle, 0.0)
    restore_omitted = abs(restore_delta) <= 1.0e-8
    restore_count = (
        0
        if restore_omitted
        else _rotation_samples(
            all_group_ids,
            groups,
            bars,
            pivot,
            longitudinal,
            restore_delta,
            translation_step,
            rotation_step,
        )
    )
    final_restore = {
        "name": "final_restore_rotation",
        "label": "完整钢筋笼回正至 IFC 姿态",
        "status": "not_applicable" if restore_omitted else "animation_only",
        "collision_checked": False,
        "omitted": restore_omitted,
        "reason": (
            "末组安装后已处于 IFC 姿态"
            if restore_omitted
            else "最终整体回正按计划不做碰撞检测"
        ),
        "moving_group_ids": all_group_ids,
        "obstacle_group_ids": [],
        "pivot_local_mm": _round_vector(pivot, 5),
        "start_pose": _serialize_pose(
            restore_start.position, restore_start.quaternion, "整笼回正起点"
        ),
        "end_pose": _serialize_pose(
            restore_end.position, restore_end.quaternion, "IFC 最终姿态"
        ),
        "control_poses": (
            []
            if restore_omitted
            else [
                _serialize_pose(
                    restore_start.position, restore_start.quaternion, "整笼回正起点"
                ),
                _serialize_pose(
                    restore_end.position, restore_end.quaternion, "IFC 最终姿态"
                ),
            ]
        ),
        "sample_count": 0 if restore_omitted else restore_count + 1,
        "translation_mm": 0.0,
        "rotation_deg": 0.0 if restore_omitted else math.degrees(abs(restore_delta)),
        "minimum_clearance_mm": None,
        "collision_pair_count": 0,
        "collision_sample_hit_count": 0,
        "maximum_collision_distance_mm": 0.0,
        "collisions": [],
    }

    path_collisions = [item for path in paths for item in path["collisions"]]
    all_collisions = list(initial_collisions) + path_collisions
    collided_paths = sum(path["status"] == "collision_detected" for path in paths)
    initial_collided = bool(initial_collisions)
    unique_pairs = {
        (
            item["moving_group_id"],
            item["moving_bar_index"],
            item["obstacle_group_id"],
            item["obstacle_bar_index"],
        )
        for item in all_collisions
    }
    checked_phases = [
        phase
        for phase in [initial_preparation]
        + [phase for path in paths for phase in path["phases"]]
        if bool(phase.get("collision_checked", False))
    ]
    collision_phase_count = sum(
        phase.get("status") == "collision_detected" for phase in checked_phases
    )
    summary = {
        "planner": "cumulative_rigid_mesh_group_prescribed_se3",
        "schema_version": 4,
        "mesh_group_count": len(sequence),
        "preinstalled_group_count": len(initially_installed),
        "pending_group_count": len(pending),
        "simulated_group_count": len(pending),
        "not_evaluated_group_count": 0,
        "collision_checked": collision_checked,
        "collision_free_count": len(paths) - collided_paths if collision_checked else 0,
        "collision_detected_count": collided_paths + int(initial_collided),
        "collision_detected_path_count": collided_paths,
        "collision_detected_phase_count": collision_phase_count,
        "initial_preparation_collision_detected": initial_collided,
        "collision_pair_count": len(unique_pairs),
        "collision_record_count": len(all_collisions),
        "collision_sample_hit_count": sum(
            int(item["sample_hit_count"]) for item in all_collisions
        ),
        "maximum_collision_distance_mm": max(
            (
                float(item["maximum_collision_distance_mm"])
                for item in all_collisions
            ),
            default=0.0,
        ),
        "all_paths_collision_free": (
            collision_phase_count == 0 if collision_checked else None
        ),
        "all_collision_checked_phases_free": (
            collision_phase_count == 0 if collision_checked else None
        ),
        "continued_after_collision": True,
        "pose_check_count": pose_checks,
        "segment_pair_test_count": segment_tests,
        "translation_discretization_mm": translation_step,
        "rotation_discretization_deg": math.degrees(rotation_step),
        "collision_model": (
            "complete centerline capsules; installed groups move as one rigid "
            "assembly; internal contacts ignored"
        ),
        "rigid_body_dof": 6,
        "path_policy": (
            "initial preparation when needed; pending horizontal group descends "
            "first; cumulative installed assembly then rotates to the next group; "
            "final restore is animation-only"
        ),
    }
    return {
        "schema_version": 4,
        "units": "mm",
        "frame": resolved.get("axes", {}),
        "motion_model": "pending_group_descent_then_cumulative_rotation",
        "assembly_rotation_axis": dict(axis),
        "representation": (
            "one shared rigid transform per cumulative installed assembly; "
            "quaternion order xyzw"
        ),
        "summary": summary,
        "initial_preparation": initial_preparation,
        "paths": paths,
        "final_restore": final_restore,
    }


def plan_mesh_group_paths(
    rebars: list[Rebar],
    resolved: dict,
    cfg: dict,
) -> dict:
    """Check the prescribed horizontal/drop/rotate path for every pending group."""
    if int(resolved.get("schema_version", 2)) >= 4:
        return _plan_mesh_group_paths_v4(rebars, resolved, cfg)
    if int(resolved.get("schema_version", 2)) == 3:
        return _plan_mesh_group_paths_v3(rebars, resolved, cfg)
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
        collision_records: dict[tuple[str, int, int], dict] = {}
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
            phase_pair_keys: set[tuple[str, int, int]] = set()
            phase_sample_hit_count = 0
            phase_max_collision_distance = 0.0
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
                free, hits, clearance = world.check(
                    bars, pivot, pose, rank, contacts, allow_contacts
                )
                checked += 1
                minimum_clearance = min(minimum_clearance, clearance)
                if not free:
                    phase_free = False
                    animation_fraction = (phase_index + fraction) / len(phase_specs)
                    collision_pose = _serialize_pose(
                        pose.position, pose.quaternion, "碰撞姿态"
                    )
                    phase_sample_hit_count += len(hits)
                    for hit in hits:
                        observation = {
                            "phase": phase_name,
                            "phase_label": phase_label,
                            "sample_index": sample_index,
                            "sample_count": sample_count,
                            "path_fraction": fraction,
                            "animation_fraction": animation_fraction,
                            "collision_pose": collision_pose,
                            **hit,
                        }
                        if first_collision is None:
                            first_collision = dict(observation)
                        key = (
                            phase_name,
                            int(hit["moving_bar_index"]),
                            int(hit["obstacle_bar_index"]),
                        )
                        phase_pair_keys.add(key)
                        phase_max_collision_distance = max(
                            phase_max_collision_distance,
                            float(hit["collision_distance_mm"]),
                        )
                        existing = collision_records.get(key)
                        if existing is None:
                            collision_records[key] = {
                                **observation,
                                "first_sample_index": sample_index,
                                "first_path_fraction": fraction,
                                "first_animation_fraction": animation_fraction,
                                "last_sample_index": sample_index,
                                "last_path_fraction": fraction,
                                "last_animation_fraction": animation_fraction,
                                "sample_hit_count": 1,
                                "maximum_collision_distance_mm": float(
                                    hit["collision_distance_mm"]
                                ),
                            }
                        else:
                            existing["sample_hit_count"] += 1
                            existing["last_sample_index"] = sample_index
                            existing["last_path_fraction"] = fraction
                            existing["last_animation_fraction"] = animation_fraction
                            if float(hit["collision_distance_mm"]) > float(
                                existing["maximum_collision_distance_mm"]
                            ):
                                first_fields = {
                                    name: existing[name]
                                    for name in (
                                        "first_sample_index",
                                        "first_path_fraction",
                                        "first_animation_fraction",
                                        "sample_hit_count",
                                        "last_sample_index",
                                        "last_path_fraction",
                                        "last_animation_fraction",
                                    )
                                }
                                existing.update(observation)
                                existing.update(first_fields)
                                existing["maximum_collision_distance_mm"] = float(
                                    hit["collision_distance_mm"]
                                )
            phase_results.append(
                {
                    "name": phase_name,
                    "label": phase_label,
                    "status": "collision_free" if phase_free else "collision_detected",
                    "sample_count": sample_count + 1,
                    "translation_mm": distance,
                    "rotation_deg": math.degrees(angle),
                    "collision_pair_count": len(phase_pair_keys),
                    "collision_sample_hit_count": phase_sample_hit_count,
                    "maximum_collision_distance_mm": phase_max_collision_distance,
                }
            )
        collisions = sorted(
            collision_records.values(),
            key=lambda item: (
                -float(item["maximum_collision_distance_mm"]),
                float(item["first_animation_fraction"]),
            ),
        )
        unique_path_pairs = {
            (int(item["moving_bar_index"]), int(item["obstacle_bar_index"]))
            for item in collisions
        }
        status = "collision_free" if first_collision is None else "collision_detected"
        path = {
            **base_path(group),
            "status": status,
            "phases": phase_results,
            "target_contact_pair_count": int(sum(len(value) for value in contacts.values())),
            "checked_pose_count": checked,
            "minimum_clearance_mm": None if math.isinf(minimum_clearance) else minimum_clearance,
            "collision_pair_count": len(unique_path_pairs),
            "collision_record_count": len(collisions),
            "collision_sample_hit_count": int(
                sum(int(item["sample_hit_count"]) for item in collisions)
            ),
            "maximum_collision_distance_mm": (
                float(collisions[0]["maximum_collision_distance_mm"])
                if collisions else 0.0
            ),
            "collisions": collisions,
        }
        if first_collision is not None:
            path["first_collision"] = first_collision
            path["worst_collision"] = collisions[0]
        paths.append(path)

    collision_free = sum(path["status"] == "collision_free" for path in paths)
    failed = sum(path["status"] == "collision_detected" for path in paths)
    all_collisions = [collision for path in paths for collision in path.get("collisions", [])]
    unique_collision_pairs = {
        (
            str(collision.get("moving_group_id", "")),
            int(collision["moving_bar_index"]),
            str(collision.get("obstacle_group_id", "")),
            int(collision["obstacle_bar_index"]),
        )
        for collision in all_collisions
    }
    maximum_collision_distance = max(
        (
            float(collision["maximum_collision_distance_mm"])
            for collision in all_collisions
        ),
        default=0.0,
    )
    summary = {
        "planner": "rigid_mesh_group_prescribed_se3",
        "mesh_group_count": len(groups),
        "preinstalled_group_count": len(groups) - len(pending),
        "pending_group_count": len(pending),
        "simulated_group_count": len(pending),
        "not_evaluated_group_count": 0,
        "collision_free_count": collision_free,
        "collision_detected_count": failed,
        "collision_pair_count": len(unique_collision_pairs),
        "collision_record_count": len(all_collisions),
        "collision_sample_hit_count": int(
            sum(int(collision["sample_hit_count"]) for collision in all_collisions)
        ),
        "maximum_collision_distance_mm": maximum_collision_distance,
        "all_paths_collision_free": failed == 0,
        "continued_after_collision": True,
        "pose_check_count": world.pose_checks,
        "segment_pair_test_count": world.segment_tests,
        "translation_discretization_mm": translation_step,
        "rotation_discretization_deg": math.degrees(rotation_step),
        "collision_model": "complete centerline capsules; same-group contacts ignored; only preinstalled and earlier groups are obstacles",
        "rigid_body_dof": 6,
        "path_policy": "vertical descent then fixed longitudinal-axis rotation; collision samples are recorded and the full path plus all later groups continue; no RRT fallback",
    }
    return {
        "units": "mm",
        "frame": resolved.get("axes", {}),
        "representation": "shared rigid-group pose at rotation-axis pivot; quaternion order xyzw",
        "summary": summary,
        "paths": paths,
    }
