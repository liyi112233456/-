from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from scipy.spatial.transform import Rotation, Slerp

from .ifc_geometry import Rebar

Progress = Callable[[str, float, str], None]


def _norm(v: np.ndarray, default=(0.0, 0.0, 1.0)) -> np.ndarray:
    v = np.asarray(v, dtype=float)
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else np.asarray(default, dtype=float)


def _arc_point_tangent(P: np.ndarray, fraction: float = 0.5) -> tuple[np.ndarray, np.ndarray]:
    seg = np.linalg.norm(np.diff(P, axis=0), axis=1)
    total = float(seg.sum())
    target = np.clip(fraction, 0.0, 1.0) * total
    acc = 0.0
    for i, length in enumerate(seg):
        if acc + length >= target or i == len(seg) - 1:
            u = 0.0 if length < 1e-12 else (target - acc) / length
            return P[i] + u * (P[i + 1] - P[i]), _norm(P[i + 1] - P[i], (1, 0, 0))
        acc += length
    return P[-1], _norm(P[-1] - P[-2], (1, 0, 0))


def _pose_rotation(tangent: np.ndarray, motion: np.ndarray) -> np.ndarray:
    z = _norm(motion)
    x = tangent - z * np.dot(tangent, z)
    if np.linalg.norm(x) < 1e-8:
        seed = np.array([1.0, 0.0, 0.0]) if abs(z[0]) < 0.8 else np.array([0.0, 1.0, 0.0])
        x = seed - z * np.dot(seed, z)
    x = _norm(x)
    y = _norm(np.cross(z, x))
    x = _norm(np.cross(y, z))
    return np.column_stack([x, y, z])


def _quintic(u: float) -> float:
    u = float(np.clip(u, 0.0, 1.0))
    return 10 * u ** 3 - 15 * u ** 4 + 6 * u ** 5


def _segment_samples(p0, R0, p1, R1, speed_mm_s, angular_deg_s, dt):
    dist = float(np.linalg.norm(p1 - p0))
    angle = float(np.linalg.norm((Rotation.from_matrix(R0).inv() * Rotation.from_matrix(R1)).as_rotvec()))
    duration = max(dist / max(speed_mm_s, 1e-6), angle / max(math.radians(angular_deg_s), 1e-6), dt)
    n = max(2, int(math.ceil(duration / dt)) + 1)
    ts = np.linspace(0.0, duration, n)
    key = Rotation.from_matrix(np.stack([R0, R1]))
    slerp = Slerp([0.0, duration], key)
    for t in ts:
        s = _quintic(t / duration)
        p = p0 + s * (p1 - p0)
        R = slerp([s * duration]).as_matrix()[0]
        yield float(t), p, R


def _outside_distance(bar: Rebar, direction: np.ndarray, model_min: np.ndarray, model_max: np.ndarray, margin: float) -> float:
    corners = np.array([[x, y, z] for x in (model_min[0], model_max[0]) for y in (model_min[1], model_max[1]) for z in (model_min[2], model_max[2])])
    proj = corners @ direction
    bp = bar.axis @ direction
    return max(margin, float(bp.max() - proj.min() + margin), float(proj.max() - bp.min() + margin))


def _waypoints(bar: Rebar, direction: np.ndarray, model_min: np.ndarray, model_max: np.ndarray, cfg: dict):
    grasp, tangent = _arc_point_tangent(bar.axis, float(cfg["grasp_fraction"]))
    R = _pose_rotation(tangent, direction)
    travel = _outside_distance(bar, direction, model_min, model_max, float(cfg["outside_margin_mm"]))
    final = grasp
    outside = final - direction * travel
    pre = final - direction * float(cfg["preinsert_distance_mm"])
    retreat = final - direction * float(cfg["retreat_distance_mm"])
    return [
        ("outside", outside, R, 0),
        ("preinsert", pre, R, 1),
        ("insert", final, R, 1),
        ("release", final, R, 0),
        ("retreat", retreat, R, 0),
    ]


def _assembly_waypoints(
    bar: Rebar, direction: np.ndarray, cfg: dict, assembly_path: dict
):
    _, tangent = _arc_point_tangent(bar.axis, float(cfg["grasp_fraction"]))
    final_gripper_rotation = _pose_rotation(tangent, direction)
    controls = assembly_path.get("control_poses") or []
    if not controls:
        return []
    poses = []
    for control in controls:
        position = np.asarray(control["position_mm"], dtype=float)
        object_rotation = Rotation.from_quat(control["quaternion_xyzw"]).as_matrix()
        poses.append((position, object_rotation @ final_gripper_rotation))
    waypoints = [("outside", poses[0][0], poses[0][1], 0)]
    waypoints.append(("grasp", poses[0][0], poses[0][1], 1))
    for index, (position, rotation) in enumerate(poses[1:], 1):
        phase = "insert" if index == len(poses) - 1 else "transfer"
        waypoints.append((phase, position, rotation, 1))
    final_position, final_rotation = poses[-1]
    waypoints.append(("release", final_position, final_rotation, 0))
    retreat = final_position - direction * float(cfg["retreat_distance_mm"])
    waypoints.append(("retreat", retreat, final_rotation, 0))
    return waypoints


def generate_robot_outputs(
    out_dir: Path,
    rebars: list[Rebar],
    sequence: list[dict],
    cfg: dict,
    progress: Optional[Progress] = None,
    assembly_paths: Optional[dict[int, dict]] = None,
) -> dict:
    cb = progress or (lambda *_: None)
    out_dir.mkdir(parents=True, exist_ok=True)
    by_index = {b.index: b for b in rebars}
    model_min = np.min([b.bbox_min for b in rebars], axis=0)
    model_max = np.max([b.bbox_max for b in rebars], axis=0)
    dt = float(cfg["robot_sample_period_s"])
    linear = float(cfg["robot_linear_speed_mm_s"])
    angular = float(cfg["robot_angular_speed_deg_s"])
    preinstalled_bar_indices = [
        int(row["bar_index"]) for row in sequence if bool(row.get("preinstalled", False))
    ]
    pending_sequence = [row for row in sequence if not bool(row.get("preinstalled", False))]

    tcp_csv = out_dir / "tcp_trajectory.csv"
    fields = [
        "time_s", "installation_step", "bar_index", "phase",
        "x_mm", "y_mm", "z_mm", "qx", "qy", "qz", "qw", "gripper",
    ]
    waypoints_all = []
    skipped_bar_indices: list[int] = []
    sample_count = 0
    clock = 0.0
    with tcp_csv.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for index, row in enumerate(pending_sequence):
            bar = by_index[int(row["bar_index"])]
            direction = _norm(
                np.asarray(row["entry_direction"], dtype=float), (0, 0, -1)
            )
            assembly_path = (
                assembly_paths.get(bar.index) if assembly_paths is not None else None
            )
            if assembly_path is not None:
                if assembly_path.get("status") != "collision_free":
                    skipped_bar_indices.append(bar.index)
                    continue
                wp = _assembly_waypoints(bar, direction, cfg, assembly_path)
            else:
                wp = _waypoints(bar, direction, model_min, model_max, cfg)
            if len(wp) < 2:
                skipped_bar_indices.append(bar.index)
                continue
            waypoints_all.append((row, wp))
            for phase_idx in range(len(wp) - 1):
                _, p0, R0, _ = wp[phase_idx]
                phase1, p1, R1, grip1 = wp[phase_idx + 1]
                samples = list(
                    _segment_samples(p0, R0, p1, R1, linear, angular, dt)
                )
                selected = samples[:-1] if phase_idx < len(wp) - 2 else samples
                for local_t, p, R in selected:
                    q = Rotation.from_matrix(R).as_quat()
                    writer.writerow({
                        "time_s": f"{clock + local_t:.6f}",
                        "installation_step": row["installation_step"],
                        "bar_index": bar.index,
                        "phase": phase1,
                        "x_mm": f"{p[0]:.6f}",
                        "y_mm": f"{p[1]:.6f}",
                        "z_mm": f"{p[2]:.6f}",
                        "qx": f"{q[0]:.9f}",
                        "qy": f"{q[1]:.9f}",
                        "qz": f"{q[2]:.9f}",
                        "qw": f"{q[3]:.9f}",
                        "gripper": grip1,
                    })
                    sample_count += 1
                clock += samples[-1][0]
            if index % max(1, len(pending_sequence) // 20) == 0:
                cb(
                    "robot",
                    0.972 + 0.018 * index / max(1, len(pending_sequence)),
                    f"Generate collision-approved robot TCP path {index + 1}/{len(pending_sequence)}",
                )

    preview = []
    for row, wp in waypoints_all:
        preview.append({
            "installation_step": row["installation_step"],
            "bar_index": row["bar_index"],
            "waypoints": [
                {
                    "phase": phase,
                    "position_mm": np.round(p, 4).tolist(),
                    "quaternion_xyzw": np.round(
                        Rotation.from_matrix(R).as_quat(), 8
                    ).tolist(),
                    "gripper": grip,
                }
                for phase, p, R, grip in wp
            ],
        })
    (out_dir / "robot_waypoints.json").write_text(
        json.dumps(preview, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    _export_abb(waypoints_all, out_dir / "rebar_install.mod")
    _export_kuka(waypoints_all, out_dir / "rebar_install.src")
    _export_urscript(waypoints_all, out_dir / "rebar_install.script")
    summary = {
        "total_bar_count": len(sequence),
        "preinstalled_bar_count": len(preinstalled_bar_indices),
        "preinstalled_bar_indices": preinstalled_bar_indices,
        "requested_bar_count": len(pending_sequence),
        "exported_bar_count": len(waypoints_all),
        "skipped_unsafe_bar_count": len(skipped_bar_indices),
        "skipped_bar_indices": skipped_bar_indices,
        "tcp_sample_count": sample_count,
        "nominal_duration_s": clock,
        "uses_se3_assembly_paths": assembly_paths is not None,
        "task_space_only": True,
        "controller_exports_exclude_failed_paths": True,
        "joint_trajectory_requires": [
            "robot URDF", "base frame", "workpiece frame", "TCP",
            "joint seed and limits", "gripper and robot-link collision models",
        ],
        "files": [
            "tcp_trajectory.csv", "robot_waypoints.json", "rebar_install.mod",
            "rebar_install.src", "rebar_install.script",
        ],
    }
    (out_dir / "robot_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    cb("robot", 0.99, f"Generated {sample_count} collision-approved TCP poses")
    return summary

def _export_abb(waypoints_all, path: Path) -> None:
    lines = [
        "MODULE RebarInstall",
        "  PERS tooldata tRebar:=[TRUE,[[0,0,0],[1,0,0,0]],[1,[0,0,0],[1,0,0,0],0,0,0]];",
        "  PROC main()",
    ]
    for row, waypoints in waypoints_all:
        lines.append(f"    ! Step {row['installation_step']}, bar {row['bar_index']}")
        for phase, p, R, _ in waypoints:
            q = Rotation.from_matrix(R).as_quat()
            qwxyz = [q[3], q[0], q[1], q[2]]
            lines.append(
                f"    MoveL [[{p[0]:.3f},{p[1]:.3f},{p[2]:.3f}],"
                f"[{qwxyz[0]:.8f},{qwxyz[1]:.8f},{qwxyz[2]:.8f},{qwxyz[3]:.8f}],"
                "[0,0,0,0],[9E9,9E9,9E9,9E9,9E9,9E9]],v200,z10,tRebar;"
                f" ! {phase}"
            )
    lines += ["  ENDPROC", "ENDMODULE"]
    path.write_text("\n".join(lines), encoding="utf-8")


def _export_kuka(waypoints_all, path: Path) -> None:
    lines = ["&ACCESS RVP", "&REL 1", "DEF REBAR_INSTALL()", "  BAS(#INITMOV,0)"]
    for row, waypoints in waypoints_all:
        lines.append(f"  ; Step {row['installation_step']}, bar {row['bar_index']}")
        for phase, p, R, _ in waypoints:
            A, B, C = Rotation.from_matrix(R).as_euler("ZYX", degrees=True)
            lines.append(f"  LIN {{X {p[0]:.3f},Y {p[1]:.3f},Z {p[2]:.3f},A {A:.5f},B {B:.5f},C {C:.5f}}} ; {phase}")
    lines.append("END")
    path.write_text("\n".join(lines), encoding="utf-8")


def _export_urscript(waypoints_all, path: Path) -> None:
    lines = ["def rebar_install():"]
    for row, waypoints in waypoints_all:
        lines.append(f"  # Step {row['installation_step']}, bar {row['bar_index']}")
        for phase, p, R, _ in waypoints:
            rv = Rotation.from_matrix(R).as_rotvec()
            lines.append(
                f"  movel(p[{p[0]/1000:.6f},{p[1]/1000:.6f},{p[2]/1000:.6f},"
                f"{rv[0]:.8f},{rv[1]:.8f},{rv[2]:.8f}], a=0.5, v=0.2) # {phase}"
            )
    lines.append("end")
    path.write_text("\n".join(lines), encoding="utf-8")
