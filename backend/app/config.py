from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("REBAR_DATA_DIR", BASE_DIR / "data")).resolve()
JOBS_DIR = DATA_DIR / "jobs"
DB_PATH = DATA_DIR / "tasks.sqlite3"
MAX_UPLOAD_MB = int(os.getenv("REBAR_MAX_UPLOAD_MB", "500"))
MAX_CONCURRENT_JOBS = int(os.getenv("REBAR_MAX_CONCURRENT_JOBS", "2"))
JOBS_DIR.mkdir(parents=True, exist_ok=True)
