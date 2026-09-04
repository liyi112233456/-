from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from .config import JOBS_DIR, MAX_UPLOAD_MB
from .models import PlanningOptions, RobotPathRequest
from .services.ifc_geometry import (
    parse_ifc_rebar_models,
    parse_ifc_rebars,
    rebar_display_id,
)
from .services.manual_sequence_workbook import build_manual_sequence_workbook
from .services.mesh_groups import (
    plan_mesh_group_paths,
    rebar_model_fingerprint,
    resolve_mesh_groups,
)
from .task_store import create_task, get_task, init_db, list_tasks

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    lifespan=lifespan,
    title="钢筋空间拓扑规划与碰撞检测系统",
    version="1.9.0",
    description="IFC 钢筋轴线恢复、多方向空间拓扑、Excel/可视化人工顺序、钢筋网片组刚体安装和六自由度碰撞检查。",
)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "service": "rebar-planning-system", "version": app.version}


def public_task(task: dict) -> dict:
    result = {
        k: task.get(k)
        for k in [
            "id", "filename", "status", "stage", "progress", "message",
            "created_at", "updated_at", "error", "summary",
        ]
    }
    result["sequence_source"] = (task.get("options") or {}).get("sequence_source")
    return result


MESH_GROUP_SCHEMA_VERSION = 4
MESH_GROUP_MOTION_MODEL = "pending_group_descent_then_cumulative_rotation"
LEGACY_MESH_GROUP_MOTION_MODEL = "cumulative_installed_rotation_then_pending_descent"
MULTI_IFC_MANIFEST = "input_models.json"


async def _save_task_ifc_uploads(
    uploads: list[UploadFile], job_dir: Path
) -> tuple[int, list[dict]]:
    """Save uploaded IFC/IFCZIP models and return a stable parse manifest."""
    if not uploads:
        raise HTTPException(400, "请选择 IFC 文件")
    total_uploaded = 0
    models: list[dict] = []
    extra_dir = job_dir / "input_models"
    for position, upload in enumerate(uploads, 1):
        source_filename = upload.filename or f"model_{position}.ifc"
        suffix = Path(source_filename).suffix.lower()
        if suffix not in {".ifc", ".ifczip"}:
            raise HTTPException(400, f"{source_filename}：仅支持 .ifc 或 .ifczip 文件")
        target = job_dir / "input.ifc" if position == 1 else extra_dir / f"{position:04d}.ifc"
        target.parent.mkdir(parents=True, exist_ok=True)
        uploaded_path = target if suffix == ".ifc" else target.with_suffix(".ifczip")
        with uploaded_path.open("wb") as stream:
            while chunk := await upload.read(1024 * 1024):
                total_uploaded += len(chunk)
                if total_uploaded > MAX_UPLOAD_MB * 1024 * 1024:
                    raise HTTPException(413, f"全部 IFC 文件总大小超过 {MAX_UPLOAD_MB} MB")
                stream.write(chunk)
        if suffix == ".ifczip":
            try:
                with zipfile.ZipFile(uploaded_path) as archive:
                    members = [
                        info for info in archive.infolist()
                        if not info.is_dir() and info.filename.lower().endswith(".ifc")
                    ]
                    if not members:
                        raise HTTPException(400, f"{source_filename}：IFCZIP 中未找到 .ifc 模型")
                    extracted_size = 0
                    with archive.open(members[0]) as source, target.open("wb") as output:
                        while chunk := source.read(1024 * 1024):
                            extracted_size += len(chunk)
                            if extracted_size > MAX_UPLOAD_MB * 1024 * 1024:
                                raise HTTPException(413, f"{source_filename}：解压后的 IFC 超过 {MAX_UPLOAD_MB} MB")
                            output.write(chunk)
            except zipfile.BadZipFile as exc:
                raise HTTPException(400, f"{source_filename}：IFCZIP 损坏或格式无效") from exc
            finally:
                uploaded_path.unlink(missing_ok=True)
        models.append({
            "path": target.relative_to(job_dir).as_posix(),
            "source_filename": source_filename,
        })
    if len(models) > 1:
        (job_dir / MULTI_IFC_MANIFEST).write_text(
            json.dumps(
                {"input_mode": "multiple_ifc_mesh_groups", "models": models},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return total_uploaded, models


def _validate_mesh_group_payload(payload: object, *, current_only: bool = True) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("JSON 顶层必须是对象")
    if payload.get("mode") != "mesh_groups":
        raise ValueError("mode 必须是 mesh_groups")
    raw_schema_version = payload.get("schema_version", 0)
    if isinstance(raw_schema_version, bool):
        raise ValueError("schema_version 必须是整数")
    try:
        numeric_schema_version = float(raw_schema_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("schema_version 必须是整数") from exc
    if not math.isfinite(numeric_schema_version) or not numeric_schema_version.is_integer():
        raise ValueError("schema_version 必须是整数")
    schema_version = int(numeric_schema_version)
    accepted = {MESH_GROUP_SCHEMA_VERSION} if current_only else {2, 3, MESH_GROUP_SCHEMA_VERSION}
    if schema_version not in accepted:
        if current_only:
            raise ValueError(f"schema_version 必须是 {MESH_GROUP_SCHEMA_VERSION}")
        raise ValueError("schema_version 必须是 2、3 或 4")
    if schema_version == MESH_GROUP_SCHEMA_VERSION:
        motion_model = payload.get("motion_model")
        if motion_model != MESH_GROUP_MOTION_MODEL:
            raise ValueError(f"motion_model 必须是 {MESH_GROUP_MOTION_MODEL}")
        axis = payload.get("assembly_rotation_axis")
        if axis is not None and not isinstance(axis, dict):
            raise ValueError("assembly_rotation_axis 必须是对象或 null")
    elif schema_version == 3:
        motion_model = payload.get("motion_model")
        if motion_model != LEGACY_MESH_GROUP_MOTION_MODEL:
            raise ValueError(f"版本 3 的 motion_model 必须是 {LEGACY_MESH_GROUP_MOTION_MODEL}")
        axis = payload.get("assembly_rotation_axis")
        if axis is not None and not isinstance(axis, dict):
            raise ValueError("assembly_rotation_axis 必须是对象或 null")
    groups = payload.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("groups 必须是非空数组")
    return payload


def _migrate_mesh_group_payload(payload: object) -> tuple[dict, int | None]:
    """Convert saved v2/v3 group definitions to the v4 assembly motion model."""
    source = _validate_mesh_group_payload(payload, current_only=False)
    source_version = int(source["schema_version"])
    if source_version == MESH_GROUP_SCHEMA_VERSION:
        return source, None

    migrated_groups: list[dict] = []
    for source_group in source["groups"]:
        if not isinstance(source_group, dict):
            raise ValueError("groups 中的每个网片组必须是对象")
        migrated_groups.append({
            "group_id": source_group.get("group_id"),
            "name": source_group.get("name", ""),
            "installation_step": source_group.get(
                "installation_step", source_group.get("step")
            ),
            "installation_status": (
                "preinstalled"
                if bool(source_group.get("preinstalled", False))
                else source_group.get(
                    "installation_status", source_group.get("status", "pending")
                )
            ),
            "bar_indices": source_group.get("bar_indices", []),
            "plane_angle_deg": source_group.get("plane_angle_deg"),
            "staging_clearance_mm": source_group.get(
                "minimum_staging_clearance_mm",
                source_group.get("staging_clearance_mm"),
            ),
        })

    assembly_axis = (
        source.get("assembly_rotation_axis")
        if source_version == 3
        else {
            "transverse_mm": None,
            "elevation_mm": None,
            "direction": None,
        }
    )
    migrated = {
        "mode": "mesh_groups",
        "schema_version": MESH_GROUP_SCHEMA_VERSION,
        "motion_model": MESH_GROUP_MOTION_MODEL,
        "model_fingerprint": source.get("model_fingerprint"),
        "longitudinal_axis": source.get("longitudinal_axis", "auto"),
        "vertical_axis": source.get("vertical_axis", [0, 0, 1]),
        "staging_clearance_mm": source.get("staging_clearance_mm", 800),
        "assembly_rotation_axis": assembly_axis,
        "groups": migrated_groups,
        "migrated_from_schema_version": source_version,
    }
    return _validate_mesh_group_payload(migrated), source_version


@app.get("/api/tasks")
def tasks(limit: int = 50) -> list[dict]:
    return [public_task(t) for t in list_tasks(min(max(limit, 1), 200))]


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: str) -> dict:
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return public_task(task)


@app.post("/api/tasks", status_code=202)
async def create_planning_task(
    file: Annotated[list[UploadFile], File(...)],
    options_json: Annotated[str, Form()] = "{}",
    sequence_file: Annotated[UploadFile | None, File()] = None,
    visual_sequence_json: Annotated[str | None, Form()] = None,
) -> dict:
    try:
        raw_options = json.loads(options_json or "{}")
        options = PlanningOptions.model_validate(raw_options)
    except Exception as exc:
        raise HTTPException(422, f"规划参数无效: {exc}") from exc
    uploads = list(file or [])
    if len(uploads) > 1 and options.sequence_source != "visual_groups":
        raise HTTPException(400, "多个 IFC 文件仅用于可视化网片组顺序；其他模式请选择一个完整 IFC")
    task_id = uuid.uuid4().hex
    job_dir = JOBS_DIR / task_id
    job_dir.mkdir(parents=True, exist_ok=False)
    size = 0
    try:
        size, saved_models = await _save_task_ifc_uploads(uploads, job_dir)
        if options.sequence_source == "excel":
            if sequence_file is None or not sequence_file.filename:
                raise HTTPException(400, "An Excel sequence file is required in excel mode")
            sequence_suffix = Path(sequence_file.filename).suffix.lower()
            if sequence_suffix not in {".xlsx", ".csv", ".tsv"}:
                raise HTTPException(400, "Sequence file must be .xlsx, .csv or .tsv")
            sequence_path = job_dir / f"input_sequence{sequence_suffix}"
            sequence_size = 0
            with sequence_path.open("wb") as stream:
                while chunk := await sequence_file.read(1024 * 1024):
                    sequence_size += len(chunk)
                    if sequence_size > 20 * 1024 * 1024:
                        raise HTTPException(413, "Sequence file exceeds 20 MB")
                    stream.write(chunk)
            size += sequence_size
        elif options.sequence_source in {"visual", "visual_groups"}:
            if not visual_sequence_json:
                message = (
                    "可视化钢筋网片组尚未设置"
                    if options.sequence_source == "visual_groups"
                    else "可视化人工顺序尚未设置"
                )
                raise HTTPException(400, message)
            try:
                visual_payload = json.loads(visual_sequence_json)
                if not isinstance(visual_payload, dict):
                    raise ValueError("JSON 顶层必须是对象")
                if options.sequence_source == "visual_groups":
                    visual_payload = _validate_mesh_group_payload(visual_payload)
                    # Group installation does not have a single-bar TCP/gripper
                    # definition, so controller exports are intentionally disabled.
                    options.generate_assembly_paths = True
                    options.generate_robot_path = False
                else:
                    items = visual_payload.get("items")
                    if not isinstance(items, list) or not items:
                        raise ValueError("items 必须是非空数组")
            except Exception as exc:
                label = "可视化钢筋网片组" if options.sequence_source == "visual_groups" else "可视化人工顺序"
                raise HTTPException(422, f"{label}格式无效: {exc}") from exc
            encoded = json.dumps(visual_payload, ensure_ascii=False, separators=(",", ":"))
            encoded_size = len(encoded.encode("utf-8"))
            if encoded_size > 20 * 1024 * 1024:
                raise HTTPException(413, "可视化人工顺序超过 20 MB")
            (job_dir / "input_sequence.json").write_text(encoded, encoding="utf-8")
            size += encoded_size
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    if len(saved_models) == 1:
        task_filename = saved_models[0]["source_filename"]
    else:
        task_filename = f"{saved_models[0]['source_filename']} 等 {len(saved_models)} 个网片 IFC"
    create_task(task_id, task_filename, options.model_dump())
    launch_worker("planning", task_id)
    return {"task_id": task_id, "status": "queued", "size_bytes": size}


@app.post("/api/tasks/{task_id}/rerun", status_code=202)
def rerun_mesh_group_task(task_id: str) -> dict:
    """Create a new calculation from a saved mesh-group task without editing it again."""
    source_task = get_task(task_id)
    if not source_task:
        raise HTTPException(404, "任务不存在")
    if (source_task.get("options") or {}).get("sequence_source") != "visual_groups":
        raise HTTPException(409, "只有可视化网片组任务可以复用原分组与顺序重新计算")
    if source_task.get("status") not in {"completed", "failed", "canceled"}:
        raise HTTPException(409, "当前任务仍在计算，完成或结束后才能重新计算")

    source_dir = JOBS_DIR / task_id
    source_ifc = source_dir / "input.ifc"
    if not source_ifc.is_file():
        raise HTTPException(409, "历史任务的原始 IFC 已不存在，无法重新计算")
    sequence_path = source_dir / "input_sequence.json"
    if not sequence_path.is_file():
        # Older mesh-group jobs may only retain the resolved output. It has the
        # same versioned group structure and can be validated by the new worker.
        sequence_path = source_dir / "output" / "mesh_groups.json"
    if not sequence_path.is_file():
        raise HTTPException(409, "历史任务的网片分组与安装顺序已不存在，无法重新计算")
    try:
        payload, migrated_from = _migrate_mesh_group_payload(
            json.loads(sequence_path.read_text(encoding="utf-8"))
        )
    except Exception as exc:
        raise HTTPException(409, f"历史任务的网片配置无效: {exc}") from exc

    try:
        options = PlanningOptions.model_validate(source_task.get("options") or {})
    except Exception as exc:
        raise HTTPException(409, f"历史任务的计算参数已不兼容: {exc}") from exc
    options.sequence_source = "visual_groups"
    options.generate_assembly_paths = True
    options.generate_robot_path = False

    new_task_id = uuid.uuid4().hex
    new_dir = JOBS_DIR / new_task_id
    new_dir.mkdir(parents=True, exist_ok=False)
    try:
        shutil.copy2(source_ifc, new_dir / "input.ifc")
        source_manifest = source_dir / MULTI_IFC_MANIFEST
        if source_manifest.is_file():
            shutil.copy2(source_manifest, new_dir / MULTI_IFC_MANIFEST)
            source_models_dir = source_dir / "input_models"
            if source_models_dir.is_dir():
                shutil.copytree(source_models_dir, new_dir / "input_models")
        (new_dir / "input_sequence.json").write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    except Exception:
        shutil.rmtree(new_dir, ignore_errors=True)
        raise

    create_task(new_task_id, source_task.get("filename") or "model.ifc", options.model_dump())
    launch_worker("planning", new_task_id)
    return {
        "task_id": new_task_id,
        "source_task_id": task_id,
        "status": "queued",
        "reused_mesh_groups": True,
        "schema_version": MESH_GROUP_SCHEMA_VERSION,
        "migrated_from_schema_version": migrated_from,
        "message": "已复用原 IFC、网片分组和安装顺序创建新计算任务",
    }


@app.get("/api/templates/installation-sequence")
def installation_sequence_template() -> FileResponse:
    path = STATIC_DIR / "installation_sequence_template.xlsx"
    if not path.is_file():
        raise HTTPException(404, "Sequence template is not installed")
    return FileResponse(path, filename="rebar_installation_sequence_template.xlsx")


def _save_uploaded_ifc_for_sequence(file: UploadFile, temp_dir: Path) -> tuple[Path, str]:
    """Persist an uploaded IFC (or IFCZIP member) for the synchronous generator."""
    original_name = file.filename or "model.ifc"
    suffix = Path(original_name).suffix.lower()
    if suffix not in {".ifc", ".ifczip"}:
        raise HTTPException(400, "\u4ec5\u652f\u6301 .ifc \u6216 .ifczip \u6587\u4ef6")
    uploaded_path = temp_dir / f"upload{suffix}"
    size = 0
    with uploaded_path.open("wb") as stream:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_MB * 1024 * 1024:
                raise HTTPException(413, f"\u6587\u4ef6\u8d85\u8fc7 {MAX_UPLOAD_MB} MB")
            stream.write(chunk)
    if suffix == ".ifc":
        return uploaded_path, original_name
    try:
        with zipfile.ZipFile(uploaded_path) as archive:
            members = [
                info
                for info in archive.infolist()
                if not info.is_dir() and info.filename.lower().endswith(".ifc")
            ]
            if not members:
                raise HTTPException(400, "IFCZIP \u4e2d\u672a\u627e\u5230 .ifc \u6a21\u578b")
            member = members[0]
            extracted = temp_dir / "model.ifc"
            with archive.open(member) as source, extracted.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            return extracted, original_name
    except zipfile.BadZipFile as exc:
        raise HTTPException(400, "IFCZIP \u6587\u4ef6\u635f\u574f\u6216\u683c\u5f0f\u65e0\u6548") from exc


@app.post("/api/sequence/preview")
def preview_visual_sequence(
    file: Annotated[list[UploadFile], File(...)],
    visual_sequence_json: Annotated[str | None, Form()] = None,
) -> dict:
    """Parse one or more IFCs into the visual mesh/order editor model."""
    temp_dir = Path(tempfile.mkdtemp(prefix="visual_sequence_", dir=JOBS_DIR))
    try:
        uploads = list(file or [])
        if not uploads:
            raise HTTPException(400, "请选择 IFC 文件")
        parsed_models: list[tuple[Path, str]] = []
        for position, upload in enumerate(uploads, 1):
            model_dir = temp_dir / f"model_{position:04d}"
            model_dir.mkdir(parents=True, exist_ok=True)
            input_path, source_name = _save_uploaded_ifc_for_sequence(upload, model_dir)
            parsed_models.append((input_path, source_name))
        rebars, _, ifc_meta = parse_ifc_rebar_models(parsed_models)
        fingerprint = rebar_model_fingerprint(rebars)
        source_names = [source_name for _, source_name in parsed_models]
        suggested_groups = [
            {
                "group_id": f"G{position:03d}",
                "name": Path(source["source_filename"]).stem or f"网片组 {position}",
                "installation_step": position,
                "installation_status": "pending",
                "bar_indices": source["bar_indices"],
                "source_filename": source["source_filename"],
            }
            for position, source in enumerate(ifc_meta.get("source_models", []), 1)
        ] if len(parsed_models) > 1 else []
        response = {
            "units": "mm",
            "source_filename": source_names[0] if len(source_names) == 1 else f"{source_names[0]} 等 {len(source_names)} 个 IFC",
            "source_filenames": source_names,
            "mesh_group_input_mode": "multiple_ifc_files" if len(source_names) > 1 else "single_complete_ifc",
            "suggested_mesh_groups": suggested_groups,
            "model_fingerprint": fingerprint,
            "bars": [
                {
                    "i": bar.index,
                    "n": rebar_display_id(bar),
                    "name": bar.name,
                    "tag": bar.tag,
                    "r": round(bar.radius, 4),
                    "p": bar.axis.round(3).tolist(),
                }
                for bar in rebars
            ],
            "initial_installed": [],
            "sequence": [],
            "meta": {
                **ifc_meta,
                "rebar_count": len(rebars),
                "axis_total_length_m": sum(bar.length for bar in rebars) / 1000.0,
            },
        }
        if visual_sequence_json:
            try:
                payload = json.loads(visual_sequence_json)
            except json.JSONDecodeError as exc:
                raise HTTPException(422, f"可视化钢筋网片组 JSON 格式无效: {exc}") from exc
            if not isinstance(payload, dict) or payload.get("mode") != "mesh_groups":
                raise HTTPException(422, "网片组预览的 mode 必须是 mesh_groups")
            mesh_groups = resolve_mesh_groups(
                rebars,
                payload,
                model_fingerprint=fingerprint,
            )
            preview_paths = plan_mesh_group_paths(
                rebars,
                mesh_groups,
                {
                    "clearance_mm": float(payload.get("clearance_mm", 1.0)),
                    "assembly_translation_step_mm": float(
                        payload.get("assembly_translation_step_mm", 75.0)
                    ),
                    "assembly_rotation_step_deg": float(
                        payload.get("assembly_rotation_step_deg", 7.5)
                    ),
                    "preview_without_collision": True,
                },
            )
            mesh_groups["paths"] = preview_paths.get("paths", [])
            mesh_groups["initial_preparation"] = preview_paths.get("initial_preparation")
            mesh_groups["final_restore"] = preview_paths.get("final_restore")
            mesh_groups["path_summary"] = preview_paths.get("summary", {})
            response["mesh_groups"] = mesh_groups
        return response
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            422,
            f"IFC 解析或可视化模型生成失败：{type(exc).__name__}: {exc}",
        ) from exc
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.post("/api/sequence/generate")
def generate_manual_sequence(file: Annotated[UploadFile, File(...)]) -> FileResponse:
    """Parse an IFC and return an editable row-order installation workbook."""
    temp_dir = Path(tempfile.mkdtemp(prefix="manual_sequence_", dir=JOBS_DIR))
    try:
        input_path, source_name = _save_uploaded_ifc_for_sequence(file, temp_dir)
        rebars, _, ifc_meta = parse_ifc_rebars(input_path)
        stem = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", Path(source_name).stem).strip("_") or "model"
        output_path = temp_dir / f"rebar_installation_sequence_{stem}.xlsx"
        build_manual_sequence_workbook(
            rebars,
            output_path,
            source_filename=source_name,
            ifc_meta=ifc_meta,
        )
        return FileResponse(
            output_path,
            filename=output_path.name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
        )
    except HTTPException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(422, f"IFC \u89e3\u6790\u6216\u4eba\u5de5\u987a\u5e8f\u8868\u751f\u6210\u5931\u8d25\uff1a{type(exc).__name__}: {exc}") from exc


def launch_worker(mode: str, task_id: str, config: dict | None = None) -> None:


    cmd = [sys.executable, "-m", "app.worker", mode, task_id]
    if config is not None:
        cmd.append(json.dumps(config, ensure_ascii=False))
    env = os.environ.copy()
    backend_dir = str(APP_DIR.parent)
    env["PYTHONPATH"] = backend_dir + os.pathsep + env.get("PYTHONPATH", "")
    log_path = JOBS_DIR / task_id / f"{mode}_launcher.log"
    log_stream = log_path.open("ab")
    subprocess.Popen(cmd, cwd=backend_dir, env=env, stdout=log_stream, stderr=subprocess.STDOUT, start_new_session=True)
    log_stream.close()


@app.get("/api/tasks/{task_id}/events")
async def task_events(task_id: str, request: Request) -> StreamingResponse:
    if not get_task(task_id):
        raise HTTPException(404, "任务不存在")

    async def event_stream():
        last = None
        while True:
            if await request.is_disconnected():
                break
            task = get_task(task_id)
            if not task:
                break
            payload = json.dumps(public_task(task), ensure_ascii=False)
            if payload != last:
                yield f"event: task\ndata: {payload}\n\n"
                last = payload
            if task["status"] in {"completed", "failed", "canceled"}:
                break
            await asyncio.sleep(0.5)
        yield "event: close\ndata: {}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/tasks/{task_id}/files/{file_path:path}")
def task_file(task_id: str, file_path: str) -> FileResponse:
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    root = (JOBS_DIR / task_id / "output").resolve()
    path = (root / file_path).resolve()
    if root not in path.parents and path != root:
        raise HTTPException(400, "非法文件路径")
    if not path.is_file():
        raise HTTPException(404, "结果文件不存在")
    return FileResponse(path, filename=path.name)


@app.get("/api/tasks/{task_id}/bundle")
def task_bundle(task_id: str) -> FileResponse:
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    path = JOBS_DIR / task_id / "result_bundle.zip"
    if not path.is_file():
        raise HTTPException(409, "结果包尚未生成")
    return FileResponse(path, filename=f"rebar_planning_{task_id[:8]}.zip")


@app.post("/api/tasks/{task_id}/robot", status_code=202)
def regenerate_robot(task_id: str, request: RobotPathRequest) -> dict:
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["status"] != "completed":
        raise HTTPException(409, "规划任务尚未完成")
    if task.get("options", {}).get("sequence_source") == "visual_groups":
        raise HTTPException(
            409,
            "钢筋网片组尚未定义多点夹具和 TCP，当前仅支持组级安装动画与碰撞检测，不能生成机器人程序",
        )
    config = {
        "robot_linear_speed_mm_s": request.linear_speed_mm_s,
        "robot_angular_speed_deg_s": request.angular_speed_deg_s,
        "robot_sample_period_s": request.sample_period_s,
        "outside_margin_mm": request.outside_margin_mm,
        "preinsert_distance_mm": request.preinsert_distance_mm,
        "retreat_distance_mm": request.retreat_distance_mm,
        "grasp_fraction": request.grasp_fraction,
    }
    launch_worker("robot", task_id, config)
    return {"task_id": task_id, "status": "accepted", "message": "机器人轨迹重新生成任务已启动"}


@app.get("/api/tasks/{task_id}/log")
def task_log(task_id: str) -> FileResponse:
    path = JOBS_DIR / task_id / "worker.log"
    if not path.is_file():
        raise HTTPException(404, "日志尚未生成")
    return FileResponse(path, media_type="text/plain; charset=utf-8")
