from __future__ import annotations

from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    canceled = "canceled"


class PlanningOptions(BaseModel):
    clearance_mm: float = Field(1.0, ge=-5.0, le=100.0)
    candidate_axes: list[str] = Field(default_factory=lambda: ["z", "y", "x"])
    axis_simplify_mm: float = Field(0.75, ge=0.0, le=20.0)
    sequence_source: Literal["automatic", "excel", "visual"] = "automatic"
    generate_assembly_paths: bool = True
    assembly_translation_step_mm: float = Field(75.0, gt=1.0, le=1000.0)
    assembly_rotation_step_deg: float = Field(7.5, gt=0.25, le=45.0)
    assembly_rrt_iterations: int = Field(350, ge=0, le=10000)
    assembly_random_seed: int = 17
    generate_robot_path: bool = True
    robot_linear_speed_mm_s: float = Field(600.0, gt=0.0)
    robot_angular_speed_deg_s: float = Field(45.0, gt=0.0)
    robot_sample_period_s: float = Field(0.10, gt=0.005, le=5.0)
    outside_margin_mm: float = Field(800.0, ge=0.0)
    preinsert_distance_mm: float = Field(250.0, ge=0.0)
    retreat_distance_mm: float = Field(300.0, ge=0.0)
    grasp_fraction: float = Field(0.5, ge=0.0, le=1.0)


class TaskView(BaseModel):
    id: str
    filename: str
    status: TaskStatus
    stage: str
    progress: float
    message: str
    created_at: str
    updated_at: str
    error: str | None = None
    summary: dict[str, Any] | None = None


class RobotPathRequest(BaseModel):
    linear_speed_mm_s: float = Field(600.0, gt=0)
    angular_speed_deg_s: float = Field(45.0, gt=0)
    sample_period_s: float = Field(0.10, gt=0.005, le=5)
    outside_margin_mm: float = Field(800.0, ge=0)
    preinsert_distance_mm: float = Field(250.0, ge=0)
    retreat_distance_mm: float = Field(300.0, ge=0)
    grasp_fraction: float = Field(0.5, ge=0, le=1)
