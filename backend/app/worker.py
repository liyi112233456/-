from __future__ import annotations

import csv
import json
import shutil
import sys
import traceback
import zipfile
from pathlib import Path

import numpy as np

from .config import JOBS_DIR
from .services.assembly_path import plan_assembly_paths
from .services.ifc_geometry import Rebar, parse_ifc_rebars
from .services.mesh_groups import (
    plan_mesh_group_paths,
    rebar_model_fingerprint,
    resolve_mesh_groups,
)
from .services.planner import (
    plan_installation,
    save_mesh_group_outputs,
    save_planning_outputs,
)
from .services.robot_path import generate_robot_outputs
from .services.sequence_io import load_manual_sequence
from .task_store import get_task, init_db, update_task


def _progress(task_id: str):
    def callback(stage: str, progress: float, message: str) -> None:
        update_task(task_id, status="running", stage=stage, progress=float(progress), message=message)
    return callback


def _make_bundle(job_dir: Path) -> Path:
    bundle = job_dir / "result_bundle.zip"
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(job_dir.rglob("*")):
            if not path.is_file() or path == bundle or path.name == "input.ifc":
                continue
            zf.write(path, path.relative_to(job_dir))
    return bundle


def run_planning_job(task_id: str) -> None:
    init_db()
    task = get_task(task_id)
    if not task:
        raise RuntimeError(f"Unknown task {task_id}")
    job_dir = JOBS_DIR / task_id
    input_path = job_dir / "input.ifc"
    output_dir = job_dir / "output"
    robot_dir = output_dir / "robot"
    options = task["options"]
    log_path = job_dir / "worker.log"

    def log(message: str) -> None:
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(message + "\n")

    cb = _progress(task_id)
    try:
        update_task(task_id, status="running", stage="startup", progress=0.01, message="后台计算进程已启动", error=None)
        log("planning job started")
        rebars, type_axes, meta = parse_ifc_rebars(
            input_path,
            float(options.get("axis_simplify_mm", 0.75)),
            progress=cb,
        )
        group_mode = options.get("sequence_source") == "visual_groups"
        if group_mode:
            sequence_path = job_dir / "input_sequence.json"
            if not sequence_path.is_file():
                raise ValueError("Exactly one mesh-group sequence JSON file is required")
            cb("sequence", 0.54, "校验钢筋网片分组、顺序和安装参数")
            payload = json.loads(sequence_path.read_text(encoding="utf-8"))
            fingerprint = rebar_model_fingerprint(rebars)
            resolved_groups = resolve_mesh_groups(
                rebars,
                payload,
                model_fingerprint=fingerprint,
            )
            cb("collision", 0.70, "检查网片组竖直下降和定轴旋转路径")
            mesh_group_paths = plan_mesh_group_paths(rebars, resolved_groups, options)
            summary = save_mesh_group_outputs(
                output_dir,
                rebars,
                type_axes,
                meta,
                resolved_groups,
                mesh_group_paths,
            )
        elif options.get("sequence_source") in {"excel", "visual"}:
            sequence_files = sorted(job_dir.glob("input_sequence.*"))
            if len(sequence_files) != 1:
                raise ValueError("Exactly one manual sequence file is required")
            cb("sequence", 0.54, "校验用户指定的人工安装顺序")
            sequence, planner_stats = load_manual_sequence(sequence_files[0], rebars)
        else:
            sequence, planner_stats = plan_installation(
                rebars,
                list(options.get("candidate_axes", ["z", "y", "x"])),
                float(options.get("clearance_mm", 1.0)),
                progress=cb,
            )

        if not group_mode:
            assembly_paths = None
            collision_summary = None
            if (
                bool(options.get("generate_assembly_paths", True))
                or bool(options.get("generate_robot_path", True))
            ):
                assembly_paths, collision_summary = plan_assembly_paths(
                    output_dir, rebars, sequence, options, progress=cb
                )

            summary = save_planning_outputs(
                output_dir, rebars, type_axes, sequence, meta, planner_stats
            )
            if collision_summary is not None:
                summary["assembly_collision"] = collision_summary
            if bool(options.get("generate_robot_path", True)):
                robot_summary = generate_robot_outputs(
                    robot_dir,
                    rebars,
                    sequence,
                    options,
                    progress=cb,
                    assembly_paths=assembly_paths,
                )
                summary["robot"] = robot_summary
        (output_dir / "planning_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        manifest = {
            "task_id": task_id,
            "source_filename": task["filename"],
            "status": "completed",
            "summary": summary,
            "engineering_notice": "安装顺序与轨迹必须在完整实体、模板、夹具、机器人连杆和现场容差模型中复核。",
        }
        (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        _make_bundle(job_dir)
        update_task(task_id, status="completed", stage="completed", progress=1.0, message="计算完成，可查看三维动画并下载结果", summary=summary)
        log("planning job completed")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        log(error)
        log(traceback.format_exc())
        update_task(task_id, status="failed", stage="failed", progress=1.0, message="计算失败", error=error)
        raise


def load_result_rebars(job_dir: Path) -> tuple[list[Rebar], list[dict]]:
    axes = json.loads((job_dir / "output" / "rebar_axes.json").read_text(encoding="utf-8"))["bars"]
    rebars: list[Rebar] = []
    for b in axes:
        P = np.asarray(b["axis"], dtype=float)
        r = float(b["radius_mm"])
        rebars.append(Rebar(
            int(b["index"]), int(b["entity_id"]), b.get("guid", ""), b.get("name", ""), b.get("tag", ""), int(b.get("map_id", 0)),
            P, r, P.min(0) - r, P.max(0) + r, float(b["length_mm"]),
        ))
    sequence: list[dict] = []
    with (job_dir / "output" / "installation_sequence.csv").open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            status = (row.get("installation_status") or "pending").strip().lower()
            preinstalled = (row.get("preinstalled") or "").strip().lower() in {"1", "true", "yes"} or status == "preinstalled"
            sequence.append({
                "installation_step": int(row["installation_step"]),
                "bar_index": int(row["bar_index"]),
                "entry_direction": [float(x) for x in row["entry_direction"].split(";")],
                "preinstalled": preinstalled,
                "installation_status": "preinstalled" if preinstalled else "pending",
            })
    return rebars, sequence


def load_assembly_paths(job_dir: Path) -> dict[int, dict] | None:
    path = job_dir / "output" / "assembly_paths.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {int(item["bar_index"]): item for item in payload.get("paths", [])}



def run_robot_regeneration(task_id: str, config: dict) -> None:
    init_db()
    job_dir = JOBS_DIR / task_id
    task = get_task(task_id)
    if not task:
        raise RuntimeError("Task not found")
    if task.get("options", {}).get("sequence_source") == "visual_groups":
        raise ValueError(
            "钢筋网片组尚未定义多点夹具和 TCP，不能生成机器人程序"
        )
    try:
        update_task(task_id, status="running", stage="robot", progress=0.90, message="按新参数重新生成机器人轨迹")
        rebars, sequence = load_result_rebars(job_dir)
        merged = dict(task["options"])
        merged.update(config)
        robot_summary = generate_robot_outputs(
            job_dir / "output" / "robot",
            rebars,
            sequence,
            merged,
            progress=_progress(task_id),
            assembly_paths=load_assembly_paths(job_dir),
        )
        summary = task.get("summary") or {}
        summary["robot"] = robot_summary
        (job_dir / "output" / "planning_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        _make_bundle(job_dir)
        update_task(task_id, status="completed", stage="completed", progress=1.0, message="机器人轨迹已重新生成", summary=summary)
    except Exception as exc:
        update_task(task_id, status="failed", stage="failed", progress=1.0, message="机器人轨迹生成失败", error=f"{type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("Usage: python -m app.worker planning|robot TASK_ID [CONFIG_JSON]")
    mode = sys.argv[1]
    task_id = sys.argv[2]
    if mode == "planning":
        run_planning_job(task_id)
    elif mode == "robot":
        config = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        run_robot_regeneration(task_id, config)
    else:
        raise SystemExit(f"Unknown mode: {mode}")
