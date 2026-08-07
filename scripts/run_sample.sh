#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IFC="${1:?Usage: run_sample.sh /path/to/model.ifc}"
cd "$ROOT/backend"
export PYTHONPATH="$PWD"
python - <<PY
import json, shutil, uuid
from pathlib import Path
from app.task_store import init_db, create_task
from app.config import JOBS_DIR
from app.worker import run_planning_job
src=Path(r'''$IFC'''); task_id=uuid.uuid4().hex; d=JOBS_DIR/task_id; d.mkdir(parents=True); shutil.copy2(src,d/'input.ifc')
opts={'clearance_mm':1.0,'candidate_axes':['z','y'],'axis_simplify_mm':0.75,'generate_robot_path':True,'robot_linear_speed_mm_s':600.0,'robot_angular_speed_deg_s':45.0,'robot_sample_period_s':0.1,'outside_margin_mm':800.0,'preinsert_distance_mm':250.0,'retreat_distance_mm':300.0,'grasp_fraction':0.5}
init_db();create_task(task_id,src.name,opts);run_planning_job(task_id);print(task_id);print(d/'result_bundle.zip')
PY
