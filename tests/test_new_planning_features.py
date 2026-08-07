import csv
from pathlib import Path

import numpy as np
from openpyxl import Workbook

from app.services.assembly_path import (
    PoseState,
    _segment_distances_batch,
    _transform_axis,
    plan_assembly_paths,
)
from app.services.ifc_geometry import Rebar
from app.services.planner import plan_installation
from app.services.sequence_io import load_manual_sequence


def make_bar(index: int, points, radius: float = 5.0) -> Rebar:
    axis = np.asarray(points, dtype=float)
    return Rebar(
        index=index,
        entity_id=100 + index,
        guid=f"guid-{index}",
        name=f"bar-{index}",
        tag=f"B{index}",
        map_id=0,
        axis=axis,
        radius=radius,
        bbox_min=axis.min(0) - radius,
        bbox_max=axis.max(0) + radius,
        length=float(np.linalg.norm(np.diff(axis, axis=0), axis=1).sum()),
    )


def test_segment_distance_3d_crossing():
    distance = _segment_distances_batch(
        np.array([[0.0, 0.0, 0.0]]),
        np.array([[10.0, 0.0, 0.0]]),
        np.array([[5.0, -2.0, 0.0]]),
        np.array([[5.0, 2.0, 0.0]]),
    )
    assert distance[0] < 1e-9


def test_rigid_pose_rotates_rebar_about_pivot():
    bar = make_bar(0, [[-1, 0, 0], [1, 0, 0]])
    pose = PoseState(
        np.array([10.0, 0.0, 0.0]),
        np.array([0.0, 0.0, np.sin(np.pi / 4), np.cos(np.pi / 4)]),
    )
    transformed = _transform_axis(bar, np.zeros(3), pose)
    np.testing.assert_allclose(transformed, [[10, -1, 0], [10, 1, 0]], atol=1e-7)


def test_manual_xlsx_sequence_resolves_and_reorders(tmp_path: Path):
    bars = [
        make_bar(0, [[0, 0, 0], [100, 0, 0]]),
        make_bar(1, [[0, 100, 0], [100, 100, 0]]),
    ]
    path = tmp_path / "sequence.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["installation_step", "guid", "direction_x", "direction_y", "direction_z"])
    sheet.append([2, "guid-0", 1, 0, 0])
    sheet.append([1, "guid-1", None, None, None])
    workbook.save(path)

    sequence, stats = load_manual_sequence(path, bars)
    assert [row["bar_index"] for row in sequence] == [1, 0]
    assert sequence[1]["entry_direction"] == [1.0, 0.0, 0.0]
    assert stats["sequence_source"] == "excel"




def test_manual_xlsx_id_only_uses_row_order(tmp_path: Path):
    bars = [
        make_bar(0, [[0, 0, 0], [100, 0, 0]]),
        make_bar(1, [[0, 100, 0], [100, 100, 0]]),
        make_bar(2, [[0, 200, 0], [100, 200, 0]]),
    ]
    path = tmp_path / "id_sequence.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["\u94a2\u7b4bID"])
    sheet.append(["guid-2"])
    sheet.append(["guid-0"])
    sheet.append(["guid-1"])
    workbook.save(path)

    sequence, stats = load_manual_sequence(path, bars)
    assert [row["bar_index"] for row in sequence] == [2, 0, 1]
    assert [row["installation_step"] for row in sequence] == [1, 2, 3]
    assert stats["sequence_order_field"] == "row_order"



def test_manual_xlsx_name_column_accepts_bim_name_tail(tmp_path: Path):
    bars = [
        make_bar(0, [[0, 0, 0], [100, 0, 0]]),
        make_bar(1, [[0, 100, 0], [100, 100, 0]]),
    ]
    bars[0].name = "main:N4a:640520"
    bars[1].name = "main:N4a:640522"
    path = tmp_path / "name_tail_sequence.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["name"])
    sheet.append(["640522"])
    sheet.append(["640520"])
    workbook.save(path)

    sequence, stats = load_manual_sequence(path, bars)
    assert [row["bar_index"] for row in sequence] == [1, 0]
    assert stats["sequence_order_field"] == "row_order"

def test_automatic_topology_uses_multiple_candidate_axes():
    bars = [
        make_bar(0, [[0, 0, 0], [100, 0, 0]]),
        make_bar(1, [[0, 100, 100], [100, 100, 100]]),
    ]
    sequence, stats = plan_installation(bars, ["z", "y", "x"], 1.0)
    assert len(sequence) == 2
    assert stats["candidate_axes"] == ["Z", "Y", "X"]
    assert stats["tested_direction_count"] == 6
    assert all(abs(np.linalg.norm(row["entry_direction"]) - 1.0) < 1e-8 for row in sequence)


def test_se3_collision_planner_outputs_safe_paths(tmp_path: Path):
    bars = [
        make_bar(0, [[0, 0, 0], [100, 0, 0]]),
        make_bar(1, [[0, 100, 0], [100, 100, 0]]),
    ]
    sequence = [
        {
            "installation_step": index + 1,
            "bar_index": index,
            "entry_direction": [0.0, -1.0, 0.0],
        }
        for index in range(2)
    ]
    config = {
        "clearance_mm": 1.0,
        "grasp_fraction": 0.5,
        "outside_margin_mm": 100.0,
        "assembly_translation_step_mm": 25.0,
        "assembly_rotation_step_deg": 10.0,
        "assembly_rrt_iterations": 0,
        "assembly_random_seed": 3,
    }
    paths, summary = plan_assembly_paths(tmp_path, bars, sequence, config)
    assert summary["all_paths_collision_free"]
    assert summary["rigid_body_dof"] == 6
    assert set(paths) == {0, 1}
    assert (tmp_path / "assembly_paths.json").is_file()
    assert (tmp_path / "collision_report.json").is_file()
    with (tmp_path / "assembly_path_waypoints.csv").open(encoding="utf-8-sig") as stream:
        assert len(list(csv.DictReader(stream))) >= 4
