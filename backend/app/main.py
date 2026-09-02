from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
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
from .services.ifc_geometry import parse_ifc_rebars, rebar_display_id
from .services.manual_sequence_workbook import build_manual_sequence_workbook
from .services.mesh_groups import rebar_model_fingerprint, resolve_mesh_groups
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
    version="1.6.2",
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
    file: Annotated[UploadFile, File(...)],
    options_json: Annotated[str, Form()] = "{}",
    sequence_file: Annotated[UploadFile | None, File()] = None,
    visual_sequence_json: Annotated[str | None, Form()] = None,
) -> dict:
    suffix = Path(file.filename or "model.ifc").suffix.lower()
    if suffix not in {".ifc", ".ifczip"}:
        raise HTTPException(400, "仅支持 .ifc 或 .ifczip 文件")
    try:
        raw_options = json.loads(options_json or "{}")
        options = PlanningOptions.model_validate(raw_options)
    except Exception as exc:
        raise HTTPException(422, f"规划参数无效: {exc}") from exc
    task_id = uuid.uuid4().hex
    job_dir = JOBS_DIR / task_id
    job_dir.mkdir(parents=True, exist_ok=False)
    input_path = job_dir / "input.ifc"
    size = 0
    try:
        with input_path.open("wb") as stream:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_MB * 1024 * 1024:
                    raise HTTPException(413, f"文件超过 {MAX_UPLOAD_MB} MB")
                stream.write(chunk)
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
                    if visual_payload.get("mode") != "mesh_groups":
                        raise ValueError("mode 必须是 mesh_groups")
                    if int(visual_payload.get("schema_version", 0)) != 2:
                        raise ValueError("schema_version 必须是 2")
                    groups = visual_payload.get("groups")
                    if not isinstance(groups, list) or not groups:
                        raise ValueError("groups 必须是非空数组")
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
    create_task(task_id, file.filename or "model.ifc", options.model_dump())
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
        payload = json.loads(sequence_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("mode") != "mesh_groups":
            raise ValueError("mode 必须是 mesh_groups")
        if int(payload.get("schema_version", 0)) != 2:
            raise ValueError("schema_version 必须是 2")
        if not isinstance(payload.get("groups"), list) or not payload["groups"]:
            raise ValueError("groups 必须是非空数组")
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
    file: Annotated[UploadFile, File(...)],
    visual_sequence_json: Annotated[str | None, Form()] = None,
) -> dict:
    """Parse an IFC into the lightweight model used by the visual order editor."""
    temp_dir = Path(tempfile.mkdtemp(prefix="visual_sequence_", dir=JOBS_DIR))
    try:
        input_path, source_name = _save_uploaded_ifc_for_sequence(file, temp_dir)
        rebars, _, ifc_meta = parse_ifc_rebars(input_path)
        fingerprint = rebar_model_fingerprint(rebars)
        response = {
            "units": "mm",
            "source_filename": source_name,
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
            response["mesh_groups"] = resolve_mesh_groups(
                rebars,
                payload,
                model_fingerprint=fingerprint,
            )
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
