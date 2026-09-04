import csv
import json
from pathlib import Path

import numpy as np
import pytest

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
    settings.pop("longitudinal_axis", None)
    return {
        "mode": "mesh_groups",
        "schema_version": 4,
        "motion_model": "pending_group_descent_then_cumulative_rotation",
        "model_fingerprint": rebar_model_fingerprint(bars),
        "vertical_axis": [0, 0, 1],
        "assembly_rotation_axis": settings.pop(
            "assembly_rotation_axis",
            {"transverse_mm": None, "elevation_mm": None, "direction": None},
        ),
        "staging_clearance_mm": settings.pop("staging_clearance_mm", 80),
        "groups": groups,
        **settings,
    }


def make_longitudinal_meshes(
    group_sections: list[tuple[str, list[tuple[float, float]]]],
    longitudinal,
) -> tuple[list[Rebar], list[dict]]:
    """Build small longitudinal mesh groups from local (transverse, vertical) points."""
    longitudinal = np.asarray(longitudinal, dtype=float)
    longitudinal /= np.linalg.norm(longitudinal)
    vertical = np.array([0.0, 0.0, 1.0])
    transverse = np.cross(vertical, longitudinal)
    transverse /= np.linalg.norm(transverse)
    bars: list[Rebar] = []
    groups: list[dict] = []
    for step, (group_id, section_points) in enumerate(group_sections, 1):
        indices: list[int] = []
        for transverse_mm, elevation_mm in section_points:
            center = transverse * transverse_mm + vertical * elevation_mm
            index = len(bars)
            bars.append(
                make_bar(
                    index,
                    [center - longitudinal * 2000.0, center + longitudinal * 2000.0],
                )
            )
            indices.append(index)
        groups.append(
            {
                "group_id": group_id,
                "installation_step": step,
                "bar_indices": indices,
            }
        )
    return bars, groups


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
        (
            lambda value: value["assembly_rotation_axis"].update(
                transverse_mm="not-a-number"
            ),
            "有限数值",
        ),
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


def test_groups_share_one_axis_and_side_group_gets_horizontal_assembly_angle():
    bars = [
        make_bar(0, [[0, -50, 100], [1000, -50, 100]]),
        make_bar(1, [[0, 50, 100], [1000, 50, 100]]),
        make_bar(2, [[0, 100, 0], [1000, 100, 0]]),
        make_bar(3, [[0, 100, 100], [1000, 100, 100]]),
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
    assert resolved["schema_version"] == 4
    assert resolved["motion_model"] == "pending_group_descent_then_cumulative_rotation"
    side = next(group for group in resolved["groups"] if group["group_id"] == "side")
    assert abs(abs(side["plane_angle_deg"]) - 90.0) < 1.0e-5
    assert side["assembly_angle_deg"] == pytest.approx(-side["plane_angle_deg"])
    assert side["rotation_axis"] == resolved["assembly_rotation_axis"]
    assert all(
        group["rotation_axis"] == resolved["assembly_rotation_axis"]
        for group in resolved["groups"]
    )


def test_u_bar_hooks_are_excluded_from_plane_fit_but_axis_is_preserved():
    bars = [
        make_bar(0, [[0, -50, 100], [0, -50, 0], [0, 50, 0], [0, 50, 100]]),
        make_bar(1, [[1000, -50, 100], [1000, -50, 0], [1000, 50, 0], [1000, 50, 100]]),
        make_bar(2, [[0, -25, 0], [1000, -25, 0]]),
        make_bar(3, [[0, 25, 0], [1000, 25, 0]]),
        make_bar(4, [[0, -50, 200], [1000, -50, 200]]),
        make_bar(5, [[0, 50, 200], [1000, 50, 200]]),
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


def test_full_hook_geometry_participates_in_group_collision(tmp_path: Path):
    obstacle = make_bar(0, [[0, -50, 150], [1000, -50, 150]])
    hooked = make_bar(1, [[500, -50, 100], [500, -50, 0], [500, 50, 0]])
    bottom = make_bar(2, [[0, 0, 0], [1000, 0, 0]])
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
    path = result["paths"][0]
    hit = path["first_collision"]
    assert hit["phase"] == "pending_group_descent"
    assert hit["moving_bar_index"] == 1
    assert hit["moving_segment"] == 0
    assert hit["obstacle_bar_index"] == 0
    assert hit["moving_bar_bim_id"] == "640521"
    assert hit["obstacle_bar_bim_id"] == "640520"
    assert hit["moving_group_id"] == "moving"
    assert len(hit["collision_pose"]["position_mm"]) == 3
    assert len(hit["collision_pose"]["quaternion_xyzw"]) == 4
    assert len(hit["collision_position_mm"]) == 3
    assert [phase["name"] for phase in path["phases"]] == [
        "pending_group_descent",
        "installed_assembly_rotation_to_next",
    ]
    assert path["checked_pose_count"] == sum(
        phase["sample_count"]
        for phase in path["phases"]
        if phase.get("collision_checked", True)
    )
    assert path["collision_pair_count"] >= 1
    assert path["collision_sample_hit_count"] >= path["collision_pair_count"]
    assert path["maximum_collision_distance_mm"] > 0
    assert path["worst_collision"]["collision_distance_mm"] > 0
    assert path["worst_collision"]["maximum_collision_distance_mm"] > 0
    assert "severity" not in path["worst_collision"]
    assert "severity_label" not in path["worst_collision"]
    assert result["summary"]["continued_after_collision"] is True
    save_mesh_group_outputs(tmp_path, bars, {}, {}, resolved, result)
    with (tmp_path / "mesh_group_collisions.csv").open(
        newline="", encoding="utf-8-sig"
    ) as stream:
        rows = list(csv.DictReader(stream))
    assert rows
    assert {row["moving_bar_bim_id"] for row in rows} >= {"640521"}
    assert max(float(row["collision_distance_mm"]) for row in rows) > 0
    assert max(float(row["maximum_collision_distance_mm"]) for row in rows) > 0
    assert all(float(row["required_distance_mm"]) > float(row["axis_distance_mm"]) for row in rows)


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


def test_first_group_descends_then_rotates_with_current_group_toward_second():
    bars = [
        make_bar(0, [[0, -50, 0], [100, -50, 0]]),
        make_bar(1, [[0, 50, 0], [100, 50, 0]]),
    ]
    resolved = resolve_mesh_groups(
        bars,
        payload(
            bars,
            [
                {
                    "group_id": "first",
                    "installation_step": 1,
                    "bar_indices": [0],
                    "plane_angle_deg": 0,
                },
                {
                    "group_id": "second",
                    "installation_step": 2,
                    "bar_indices": [1],
                    "plane_angle_deg": 90,
                },
            ],
        ),
    )
    result = plan_mesh_group_paths(
        bars,
        resolved,
        {
            "clearance_mm": 0,
            "assembly_translation_step_mm": 20,
            "assembly_rotation_step_deg": 5,
        },
    )
    assert result["initial_preparation"]["omitted"] is True
    first_descent, first_rotation = result["paths"][0]["phases"]
    assert result["paths"][0]["installed_group_ids_before"] == []
    assert result["paths"][0]["installed_group_ids_after_descent"] == ["first"]
    assert first_descent["name"] == "pending_group_descent"
    assert first_descent["moving_group_ids"] == ["first"]
    assert first_descent["obstacle_group_ids"] == []
    assert first_descent["start_pose"]["quaternion_xyzw"] == pytest.approx(
        first_descent["end_pose"]["quaternion_xyzw"]
    )
    assert first_rotation["name"] == "installed_assembly_rotation_to_next"
    assert first_rotation["omitted"] is False
    assert first_rotation["moving_group_ids"] == ["first"]
    assert first_rotation["obstacle_group_ids"] == ["second"]
    assert first_rotation["next_group_id"] == "second"

    second_descent, last_rotation = result["paths"][1]["phases"]
    assert second_descent["moving_group_ids"] == ["second"]
    assert second_descent["obstacle_group_ids"] == ["first"]
    assert first_rotation["stationary_pending_pose"]["position_mm"] == pytest.approx(
        second_descent["start_pose"]["position_mm"]
    )
    assert first_rotation["stationary_pending_pose"]["quaternion_xyzw"] == pytest.approx(
        second_descent["start_pose"]["quaternion_xyzw"]
    )
    assert last_rotation["name"] == "installed_assembly_rotation_to_next"
    assert last_rotation["omitted"] is True
    assert last_rotation["moving_group_ids"] == ["first", "second"]
    assert last_rotation["next_group_id"] is None


def test_safe_staging_clearance_can_exceed_requested_minimum_and_final_restore_is_animation_only():
    bars = [
        make_bar(0, [[0, -300, 0], [100, -300, 0]]),
        make_bar(1, [[0, 100, 0], [100, 100, 0]]),
    ]
    resolved = resolve_mesh_groups(
        bars,
        payload(
            bars,
            [
                {
                    "group_id": "installed",
                    "installation_step": 1,
                    "installation_status": "preinstalled",
                    "bar_indices": [0],
                    "plane_angle_deg": 0,
                },
                {
                    "group_id": "pending",
                    "installation_step": 2,
                    "bar_indices": [1],
                    "plane_angle_deg": 90,
                    "staging_clearance_mm": 10,
                },
            ],
            staging_clearance_mm=10,
        ),
    )
    result = plan_mesh_group_paths(
        bars,
        resolved,
        {
            "clearance_mm": 0,
            "assembly_translation_step_mm": 20,
            "assembly_rotation_step_deg": 5,
        },
    )
    path = result["paths"][0]
    assert path["minimum_staging_clearance_mm"] == pytest.approx(10)
    assert path["effective_staging_clearance_mm"] >= path["minimum_staging_clearance_mm"]
    preparation = result["initial_preparation"]
    assert preparation["name"] == "initial_preparation_rotation"
    assert preparation["omitted"] is False
    assert preparation["moving_group_ids"] == ["installed"]
    assert preparation["obstacle_group_ids"] == ["pending"]
    assert preparation["target_group_id"] == "pending"
    assert preparation["stationary_pending_pose"]["position_mm"] == pytest.approx(
        path["phases"][0]["start_pose"]["position_mm"]
    )
    assert path["phases"][0]["name"] == "pending_group_descent"
    assert path["phases"][1]["omitted"] is True
    restore = result["final_restore"]
    assert restore["name"] == "final_restore_rotation"
    assert restore["collision_checked"] is False
    assert restore["moving_group_ids"] == ["installed", "pending"]
    assert restore["obstacle_group_ids"] == []


def test_v2_definition_is_resolved_as_v4_without_legacy_motion_parameters():
    bars = [make_bar(0, [[0, 0, 0], [100, 0, 0]])]
    legacy = {
        "mode": "mesh_groups",
        "schema_version": 2,
        "model_fingerprint": rebar_model_fingerprint(bars),
        "top_elevation_mm": 999,
        "groups": [{
            "group_id": "G1",
            "installation_step": 1,
            "bar_indices": [0],
            "plane_angle_deg": 90,
            "rotation_axis": {
                "transverse_mm": 123,
                "elevation_mm": 456,
                "direction": [1, 0, 0],
            },
            "staging_clearance_mm": 900,
        }],
    }
    resolved = resolve_mesh_groups(bars, legacy)
    assert resolved["schema_version"] == 4
    assert resolved["source_schema_version"] == 2
    assert resolved["migrated_from_schema_version"] == 2
    assert resolved["top_elevation_controls_motion"] is False
    assert resolved["groups"][0]["plane_angle_deg"] == pytest.approx(90)
    assert resolved["groups"][0]["minimum_staging_clearance_mm"] == pytest.approx(900)
    assert resolved["assembly_rotation_axis"]["transverse_mm"] != 123
    assert resolved["assembly_rotation_axis"]["elevation_mm"] != 456


def test_prior_group_collision_does_not_stop_later_group_evaluation():
    bars = [
        make_bar(0, [[0, 0, 50], [2000, 0, 50]]),
        make_bar(1, [[0, 0, 0], [2000, 0, 0]]),
        make_bar(2, [[0, 500, 0], [2000, 500, 0]]),
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
    assert result["paths"][1]["status"] == "collision_free"
    assert result["paths"][1]["checked_pose_count"] > 0
    assert result["summary"]["simulated_group_count"] == 2
    assert result["summary"]["not_evaluated_group_count"] == 0
    assert result["summary"]["collision_pair_count"] >= 1


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
                {"group_id": "pending", "installation_step": 2, "bar_indices": [1], "plane_angle_deg": 90},
            ],
            top_elevation_mm=0,
        ),
    )
    paths = plan_mesh_group_paths(
        bars,
        resolved,
        {"clearance_mm": 0, "assembly_translation_step_mm": 20, "assembly_rotation_step_deg": 5},
    )
    paths["initial_preparation"]["collisions"] = [{
        "phase": "initial_preparation_rotation",
        "phase_label": "初始已安装网片转向首片装配角",
        "moving_group_id": "installed",
        "moving_bar_index": 0,
        "moving_bar_bim_id": "640520",
        "obstacle_group_id": "pending",
        "obstacle_bar_index": 1,
        "obstacle_bar_bim_id": "640521",
        "collision_distance_mm": 2.0,
        "maximum_collision_distance_mm": 2.0,
        "axis_distance_mm": 2.0,
        "required_distance_mm": 4.0,
        "sample_hit_count": 1,
    }]
    summary = save_mesh_group_outputs(tmp_path, bars, {}, {}, resolved, paths)
    viewer = json.loads((tmp_path / "viewer_model.json").read_text(encoding="utf-8"))
    assert viewer["assembly_unit"] == "mesh_group"
    assert viewer["schema_version"] == 4
    assert viewer["motion_model"] == "pending_group_descent_then_cumulative_rotation"
    assert viewer["assembly_rotation_axis"] == resolved["assembly_rotation_axis"]
    assert viewer["initial_installed"] == [0]
    assert viewer["initial_installed_group_ids"] == ["installed"]
    assert [item["group_id"] for item in viewer["sequence"]] == [
        "__initial_preparation__",
        "pending",
        "__final_restore__",
    ]
    assert viewer["sequence"][0]["assembly_stage"] == "initial_preparation"
    assert viewer["sequence"][1]["bar_indices"] == [1]
    assert viewer["sequence"][2]["assembly_stage"] == "final_restore"
    assert viewer["initial_preparation"]["collision_checked"] is True
    assert (tmp_path / "mesh_groups.json").is_file()
    assert (tmp_path / "mesh_group_sequence.csv").is_file()
    assert (tmp_path / "mesh_group_paths.json").is_file()
    collision_csv = tmp_path / "mesh_group_collisions.csv"
    assert collision_csv.is_file()
    header = collision_csv.read_text(encoding="utf-8-sig").splitlines()[0]
    assert "maximum_collision_distance_mm" in header
    assert "severity" not in header
    with collision_csv.open(newline="", encoding="utf-8-sig") as stream:
        collision_rows = list(csv.DictReader(stream))
    assert collision_rows[0]["group_id"] == "__initial_preparation__"
    assert collision_rows[0]["next_group_id"] == "pending"
    assert collision_rows[0]["group_name"] == "初始已安装网片转向首片装配角"
    assert viewer["final_restore"]["collision_checked"] is False
    assert summary["robot"]["supported"] is False


def test_eight_longitudinal_meshes_resolve_to_v4_shared_axis_and_restore_ifc_pose():
    sections = [
        ("top", [(-40, 100), (40, 100)]),
        ("top_right", [(80, 80), (100, 60)]),
        ("right", [(120, 40), (120, -40)]),
        ("bottom_right", [(100, -60), (80, -80)]),
        ("bottom", [(40, -100), (-40, -100)]),
        ("bottom_left", [(-80, -80), (-100, -60)]),
        ("left", [(-120, -40), (-120, 40)]),
        ("top_left", [(-100, 60), (-80, 80)]),
    ]
    expected_angles = {
        "top": 0.0,
        "top_right": -45.0,
        "right": -90.0,
        "bottom_right": -135.0,
        "bottom": 180.0,
        "bottom_left": 135.0,
        "left": 90.0,
        "top_left": 45.0,
    }
    manually_set = {"top", "right", "bottom", "left"}
    bars, groups = make_longitudinal_meshes(sections, [1.0, 0.0, 0.0])
    for group in groups:
        if group["group_id"] in manually_set:
            group["plane_angle_deg"] = expected_angles[group["group_id"]]

    resolved = resolve_mesh_groups(bars, payload(bars, groups))

    assert resolved["schema_version"] == 4
    assert resolved["motion_model"] == "pending_group_descent_then_cumulative_rotation"
    assert resolved["assembly_rotation_axis"]["direction"] == pytest.approx([1, 0, 0])
    assert resolved["assembly_rotation_axis"]["point_mm"] == pytest.approx([0, 0, 0])
    for group in resolved["groups"]:
        group_id = group["group_id"]
        assert group["plane_angle_deg"] == pytest.approx(expected_angles[group_id])
        assert group["assembly_angle_deg"] == pytest.approx(-expected_angles[group_id])
        assert group["rotation_axis"] == resolved["assembly_rotation_axis"]
        expected_source = "manual" if group_id in manually_set else "automatic"
        assert group["plane_fit"]["source"] == expected_source

    result = plan_mesh_group_paths(
        bars,
        resolved,
        {
            "preview_without_collision": True,
            "assembly_translation_step_mm": 5000,
            "assembly_rotation_step_deg": 90,
        },
    )
    group_ids = [group_id for group_id, _ in sections]
    assert [path["group_id"] for path in result["paths"]] == group_ids
    assert result["initial_preparation"]["omitted"] is True
    for index, path in enumerate(result["paths"]):
        descent, rotation = path["phases"]
        assert descent["moving_group_ids"] == [group_ids[index]]
        assert descent["obstacle_group_ids"] == group_ids[:index]
        assert rotation["moving_group_ids"] == group_ids[: index + 1]
        assert rotation["next_group_id"] == (
            group_ids[index + 1] if index + 1 < len(group_ids) else None
        )
        assert rotation["omitted"] is (index + 1 == len(group_ids))
        if index + 1 < len(group_ids):
            next_descent = result["paths"][index + 1]["phases"][0]
            assert rotation["obstacle_group_ids"] == [group_ids[index + 1]]
            assert rotation["stationary_pending_pose"]["position_mm"] == pytest.approx(
                next_descent["start_pose"]["position_mm"]
            )
            assert rotation["stationary_pending_pose"]["quaternion_xyzw"] == pytest.approx(
                next_descent["start_pose"]["quaternion_xyzw"]
            )
        else:
            assert rotation["obstacle_group_ids"] == []
        assert path["installed_group_ids_after"] == group_ids[: index + 1]
    restore = result["final_restore"]
    assert restore["collision_checked"] is False
    assert restore["omitted"] is False
    assert restore["moving_group_ids"] == group_ids
    assert restore["rotation_deg"] == pytest.approx(45.0)
    assert restore["end_pose"]["quaternion_xyzw"] == pytest.approx([0, 0, 0, 1])


@pytest.mark.parametrize(
    "longitudinal",
    [
        np.array([1.0, 0.0, 0.0]),
        np.array([0.0, 1.0, 0.0]),
        np.array([1.0, 1.0, 0.0]) / np.sqrt(2.0),
    ],
    ids=["ifc-x", "ifc-y", "ifc-diagonal"],
)
def test_cumulative_motion_uses_auto_longitudinal_axis_in_any_xy_direction(longitudinal):
    sections = [
        ("top", [(-40, 100), (40, 100)]),
        ("right", [(120, 40), (120, -40)]),
        ("bottom", [(40, -100), (-40, -100)]),
        ("left", [(-120, -40), (-120, 40)]),
    ]
    angles = {"top": 0.0, "right": -90.0, "bottom": 180.0, "left": 90.0}
    bars, groups = make_longitudinal_meshes(sections, longitudinal)
    for group in groups:
        group["plane_angle_deg"] = angles[group["group_id"]]
    groups[0]["installation_status"] = "preinstalled"

    resolved = resolve_mesh_groups(bars, payload(bars, groups))
    detected = np.asarray(resolved["longitudinal_axis"], dtype=float)
    shared_axis = np.asarray(resolved["assembly_rotation_axis"]["direction"], dtype=float)
    assert abs(float(np.dot(detected, longitudinal))) > 0.999999
    assert abs(float(np.dot(shared_axis, longitudinal))) > 0.999999

    result = plan_mesh_group_paths(
        bars,
        resolved,
        {
            "preview_without_collision": True,
            "assembly_translation_step_mm": 5000,
            "assembly_rotation_step_deg": 90,
        },
    )
    preparation = result["initial_preparation"]
    assert preparation["moving_group_ids"] == ["top"]
    assert preparation["target_group_id"] == "right"
    end_quaternion = np.asarray(preparation["end_pose"]["quaternion_xyzw"], dtype=float)
    assert abs(float(np.dot(end_quaternion[:3] / np.linalg.norm(end_quaternion[:3]), detected))) > 0.999999
    for path in result["paths"]:
        descent = path["phases"][0]
        assert descent["start_pose"]["quaternion_xyzw"] == pytest.approx(
            descent["end_pose"]["quaternion_xyzw"]
        )
    assert result["final_restore"]["end_pose"]["quaternion_xyzw"] == pytest.approx(
        [0, 0, 0, 1]
    )
