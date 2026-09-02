import json
from pathlib import Path

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from app.services.ifc_geometry import Rebar
from app.services.mesh_groups import (
    plan_mesh_group_paths,
    rebar_model_fingerprint,
    resolve_mesh_groups,
)
from app.services.planner import save_mesh_group_outputs


def make_bar(index: int, points, radius: float = 2.0, name: str | None = None) -> Rebar:
    axis = np.asarray(points, dtype=float)
    return Rebar(
        index=index,
        entity_id=1000 + index,
        guid=f"guid-{index}",
        name=name or f"bar:{640520 + index}",
        tag=f"B{index}",
        map_id=0,
        axis=axis,
        radius=radius,
        bbox_min=axis.min(0) - radius,
        bbox_max=axis.max(0) + radius,
        length=float(np.linalg.norm(np.diff(axis, axis=0), axis=1).sum()),
    )


def payload(bars, groups, **settings):
    return {
        "mode": "mesh_groups",
        "schema_version": 2,
        "model_fingerprint": rebar_model_fingerprint(bars),
        "longitudinal_axis": settings.pop("longitudinal_axis", [1, 0, 0]),
        "vertical_axis": [0, 0, 1],
        "staging_clearance_mm": settings.pop("staging_clearance_mm", 80),
        "groups": groups,
        **settings,
    }


def test_mesh_group_validation_requires_complete_unique_coverage():
    bars = [
        make_bar(0, [[0, 0, 0], [100, 0, 0]]),
        make_bar(1, [[0, 20, 0], [100, 20, 0]]),
    ]
    with pytest.raises(ValueError, match="未分组"):
        resolve_mesh_groups(
            bars,
            payload(bars, [{"group_id": "G1", "installation_step": 1, "bar_indices": [0]}]),
        )
    with pytest.raises(ValueError, match="同时属于"):
        resolve_mesh_groups(
            bars,
            payload(
                bars,
                [
                    {"group_id": "G1", "installation_step": 1, "bar_indices": [0, 1]},
                    {"group_id": "G2", "installation_step": 2, "bar_indices": [1]},
                ],
            ),
        )


def test_model_fingerprint_ignores_axis_resampling():
    straight = make_bar(0, [[0, 0, 0], [100, 0, 0]])
    sampled = make_bar(0, [[0, 0, 0], [25, 0, 0], [75, 0, 0], [100, 0, 0]])
    assert rebar_model_fingerprint([straight]) == rebar_model_fingerprint([sampled])
    translated = make_bar(0, [[10, 0, 0], [110, 0, 0]])
    assert rebar_model_fingerprint([straight]) != rebar_model_fingerprint([translated])


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.pop("model_fingerprint"), "模型指纹"),
        (lambda value: value["groups"][0].update(installation_step=1.5), "必须是整数"),
        (lambda value: value["groups"][0].update(preinstalled="false"), "必须是布尔值"),
        (lambda value: value["groups"][0].update(plane_angle_deg=360), "-180° 到 180°"),
        (lambda value: value.update(top_elevation_mm="not-a-number"), "有限数值"),
    ],
)
def test_mesh_group_validation_rejects_invalid_manual_values(mutate, message):
    bars = [make_bar(0, [[0, 0, 0], [100, 0, 0]])]
    value = payload(
        bars,
        [{"group_id": "G1", "installation_step": 1, "bar_indices": [0]}],
    )
    mutate(value)
    with pytest.raises(ValueError, match=message):
        resolve_mesh_groups(bars, value)


def test_side_group_becomes_horizontal_above_top_then_returns_to_ifc_pose():
    bars = [
        make_bar(0, [[0, -50, 100], [100, -50, 100]]),
        make_bar(1, [[0, 50, 100], [100, 50, 100]]),
        make_bar(2, [[0, 100, 0], [100, 100, 0]]),
        make_bar(3, [[0, 100, 100], [100, 100, 100]]),
    ]
    resolved = resolve_mesh_groups(
        bars,
        payload(
            bars,
            [
                {"group_id": "top", "name": "顶板", "installation_step": 1, "bar_indices": [0, 1]},
                {"group_id": "side", "name": "腹板", "installation_step": 2, "bar_indices": [2, 3]},
            ],
        ),
    )
    assert resolved["top_elevation_mm"] == pytest.approx(100.0)
    side = next(group for group in resolved["groups"] if group["group_id"] == "side")
    assert abs(abs(side["plane_angle_deg"]) - 90.0) < 1.0e-5
    pivot = np.asarray(side["pivot_local_mm"])
    transition = side["control_poses"][1]
    rotation = Rotation.from_quat(transition["quaternion_xyzw"])
    transformed = np.concatenate(
        [rotation.apply(bar.axis - pivot) + np.asarray(transition["position_mm"]) for bar in bars[2:]],
        axis=0,
    )
    assert np.ptp(transformed[:, 2]) < 1.0e-6
    assert np.mean(transformed[:, 2]) == pytest.approx(100.0)
    assert side["control_poses"][2]["quaternion_xyzw"] == [0.0, 0.0, 0.0, 1.0]


def test_u_bar_hooks_are_excluded_from_plane_fit_but_axis_is_preserved():
    bars = [
        make_bar(0, [[0, -50, 100], [0, -50, 0], [0, 50, 0], [0, 50, 100]]),
        make_bar(1, [[100, -50, 100], [100, -50, 0], [100, 50, 0], [100, 50, 100]]),
        make_bar(2, [[0, -25, 0], [100, -25, 0]]),
        make_bar(3, [[0, 25, 0], [100, 25, 0]]),
        make_bar(4, [[0, -50, 200], [100, -50, 200]]),
        make_bar(5, [[0, 50, 200], [100, 50, 200]]),
    ]
    resolved = resolve_mesh_groups(
        bars,
        payload(
            bars,
            [
                {"group_id": "bottom", "installation_step": 1, "bar_indices": [0, 1, 2, 3]},
                {"group_id": "top", "installation_step": 2, "bar_indices": [4, 5]},
            ],
        ),
    )
    bottom = resolved["groups"][0]
    assert bottom["plane_fit"]["main_body_segments"]["0"] == [[1, 2]]
    assert bottom["plane_fit"]["main_body_segments"]["1"] == [[1, 2]]
    assert bottom["plane_fit"]["excluded_segment_count"] >= 4
    assert len(bars[0].axis) == 4
    assert abs(abs(bottom["plane_angle_deg"]) - 180.0) < 1.0e-5


def test_full_hook_geometry_participates_in_group_collision():
    obstacle = make_bar(0, [[0, -50, 150], [100, -50, 150]])
    hooked = make_bar(1, [[50, -50, 100], [50, -50, 0], [50, 50, 0]])
    bottom = make_bar(2, [[0, 0, 0], [100, 0, 0]])
    bars = [obstacle, hooked, bottom]
    resolved = resolve_mesh_groups(
        bars,
        payload(
            bars,
            [
                {
                    "group_id": "fixed",
                    "installation_step": 1,
                    "installation_status": "preinstalled",
                    "bar_indices": [0],
                    "plane_angle_deg": 0,
                },
                {
                    "group_id": "moving",
                    "installation_step": 2,
                    "bar_indices": [1, 2],
                    "plane_angle_deg": 0,
                },
            ],
            top_elevation_mm=0,
            staging_clearance_mm=300,
        ),
    )
    result = plan_mesh_group_paths(
        bars,
        resolved,
        {"clearance_mm": 0, "assembly_translation_step_mm": 10, "assembly_rotation_step_deg": 5},
    )
    assert result["summary"]["preinstalled_group_count"] == 1
    assert result["summary"]["collision_detected_count"] == 1
    hit = result["paths"][0]["first_collision"]
    assert hit["phase"] == "vertical_descent"
    assert hit["moving_bar_index"] == 1
    assert hit["moving_segment"] == 0
    assert hit["obstacle_bar_index"] == 0
    assert hit["moving_bar_bim_id"] == "640521"
    assert hit["obstacle_bar_bim_id"] == "640520"
    assert hit["moving_group_id"] == "moving"
    assert len(hit["collision_pose"]["position_mm"]) == 3
    assert len(hit["collision_pose"]["quaternion_xyzw"]) == 4
    assert len(hit["collision_position_mm"]) == 3


def test_future_groups_are_not_obstacles_and_preinstalled_groups_are_not_simulated():
    bars = [
        make_bar(0, [[0, 0, 0], [100, 0, 0]]),
        make_bar(1, [[0, 0, 0], [100, 0, 0]]),
    ]
    resolved = resolve_mesh_groups(
        bars,
        payload(
            bars,
            [
                {"group_id": "first", "installation_step": 1, "bar_indices": [0], "plane_angle_deg": 0},
                {"group_id": "future", "installation_step": 2, "bar_indices": [1], "plane_angle_deg": 0},
            ],
            top_elevation_mm=0,
        ),
    )
    result = plan_mesh_group_paths(
        bars,
        resolved,
        {"clearance_mm": 0, "assembly_translation_step_mm": 20, "assembly_rotation_step_deg": 5},
    )
    assert result["paths"][0]["status"] == "collision_free"
    assert result["paths"][1]["status"] == "collision_free"  # final IFC contact is retained


def test_prior_group_collision_stops_later_group_evaluation():
    bars = [
        make_bar(0, [[0, 0, 50], [100, 0, 50]]),
        make_bar(1, [[0, 0, 0], [100, 0, 0]]),
        make_bar(2, [[0, 500, 0], [100, 500, 0]]),
    ]
    resolved = resolve_mesh_groups(
        bars,
        payload(
            bars,
            [
                {"group_id": "fixed", "installation_step": 1, "installation_status": "preinstalled", "bar_indices": [0], "plane_angle_deg": 0},
                {"group_id": "blocked", "installation_step": 2, "bar_indices": [1], "plane_angle_deg": 0},
                {"group_id": "later", "installation_step": 3, "bar_indices": [2], "plane_angle_deg": 0},
            ],
            top_elevation_mm=0,
            staging_clearance_mm=100,
        ),
    )
    result = plan_mesh_group_paths(
        bars,
        resolved,
        {"clearance_mm": 0, "assembly_translation_step_mm": 10, "assembly_rotation_step_deg": 5},
    )
    assert result["paths"][0]["status"] == "collision_detected"
    assert result["paths"][1]["status"] == "not_evaluated_due_to_prior_failure"
    assert result["paths"][1]["blocked_by_group_id"] == "blocked"
    assert result["summary"]["simulated_group_count"] == 1
    assert result["summary"]["not_evaluated_group_count"] == 1


def test_mesh_group_outputs_drive_group_viewer(tmp_path: Path):
    bars = [
        make_bar(0, [[0, 0, 0], [100, 0, 0]]),
        make_bar(1, [[0, 50, 0], [100, 50, 0]]),
    ]
    resolved = resolve_mesh_groups(
        bars,
        payload(
            bars,
            [
                {"group_id": "installed", "installation_step": 1, "installation_status": "preinstalled", "bar_indices": [0], "plane_angle_deg": 0},
                {"group_id": "pending", "installation_step": 2, "bar_indices": [1], "plane_angle_deg": 0},
            ],
            top_elevation_mm=0,
        ),
    )
    paths = plan_mesh_group_paths(
        bars,
        resolved,
        {"clearance_mm": 0, "assembly_translation_step_mm": 20, "assembly_rotation_step_deg": 5},
    )
    summary = save_mesh_group_outputs(tmp_path, bars, {}, {}, resolved, paths)
    viewer = json.loads((tmp_path / "viewer_model.json").read_text(encoding="utf-8"))
    assert viewer["assembly_unit"] == "mesh_group"
    assert viewer["initial_installed"] == [0]
    assert viewer["sequence"][0]["group_id"] == "pending"
    assert viewer["sequence"][0]["bar_indices"] == [1]
    assert (tmp_path / "mesh_groups.json").is_file()
    assert (tmp_path / "mesh_group_sequence.csv").is_file()
    assert (tmp_path / "mesh_group_paths.json").is_file()
    assert summary["robot"]["supported"] is False
