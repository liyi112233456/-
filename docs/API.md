# REST API

## 创建任务

`POST /api/tasks`，`multipart/form-data`：

- `file`: IFC 文件；
- `options_json`: `PlanningOptions` JSON；
- `sequence_file`: 当 `sequence_source=excel` 时必填，支持 `.xlsx`、`.csv`、`.tsv`。
- `visual_sequence_json`: 当 `sequence_source=visual` 或 `visual_groups` 时必填；前者为逐根顺序，后者为完整网片组定义。

返回 `202` 和 `task_id`。后台以独立 Python 进程执行计算。

## 状态与进度

- `GET /api/tasks`
- `GET /api/tasks/{task_id}`
- `GET /api/tasks/{task_id}/events`：Server-Sent Events，事件名 `task`。

阶段包括 `parse`、`axis`、`instantiate`、`topology`、`sequence`、`collision`、`robot`、`completed`。

## 结果

- `GET /api/tasks/{id}/files/viewer_model.json`
- `GET /api/tasks/{id}/files/installation_sequence.csv`
- `GET /api/tasks/{id}/files/rebar_axes.json`
- `GET /api/tasks/{id}/files/assembly_paths.json`：逐根钢筋的六自由度控制姿态；
- `GET /api/tasks/{id}/files/collision_report.json`：失败步骤和首个碰撞对象；
- `GET /api/tasks/{id}/files/assembly_path_waypoints.csv`；
- `GET /api/tasks/{id}/files/robot/tcp_trajectory.csv`
- `GET /api/tasks/{id}/bundle`

当 `sequence_source=visual_groups` 时，组模式结果为：

- `mesh_groups.json`：主体段掩码、拟合残差、平面角、顶面标高和旋转轴；
- `mesh_group_sequence.csv`：网片组顺序、状态、BIM ID 和成员；
- `mesh_group_paths.json`：整组共享 `control_poses`、竖降/旋转阶段、碰撞姿态、钢筋 BIM ID 与接触点；
- `viewer_model.json`：`assembly_unit=mesh_group`，浏览器按组播放。

## 重新生成机器人轨迹

`POST /api/tasks/{id}/robot`

```json
{
  "linear_speed_mm_s": 600,
  "angular_speed_deg_s": 45,
  "sample_period_s": 0.1,
  "outside_margin_mm": 800,
  "preinsert_distance_mm": 250,
  "retreat_distance_mm": 300,
  "grasp_fraction": 0.5
}
```

## Generate Manual Sequence Excel from IFC

`POST /api/sequence/generate` (`multipart/form-data`)

- `file`: an `.ifc` or `.ifczip` model.
- The service parses all reinforcing bars and returns an editable `.xlsx` workbook.
- The generated workbook includes `installation_status`. Use `待安装`/`pending` for simulated bars and `已安装`/`preinstalled` for bars already in place. Preinstalled bars are fixed step-0 collision obstacles and are excluded from assembly animation and robot-path output.
- The first sheet `????` uses the `name` column as the required BIM identifier; row order is the installation order.
- The workbook also includes `??` and `????` sheets. The default suggestion is bottom slab -> web -> top slab; users can move rows or edit the `name` column before uploading the workbook for planning.

## 可视化人工顺序

`POST /api/sequence/preview`（`multipart/form-data`）接收 `file`，返回轻量三维钢筋轴线、BIM ID、半径和索引。网页编辑器可通过点选模型或搜索 BIM ID 加入钢筋，支持拖动、上移/下移、移除以及“已安装”标记；保存时必须完整覆盖 IFC 中的全部钢筋，随后以 `sequence_source=visual` 创建任务。

## 可视化网片组顺序

第一次调用 `POST /api/sequence/preview` 只传 `file`，响应包含稳定的 `model_fingerprint`。点选/框选完成分组后，再向同一接口附加 `visual_sequence_json` 可预览主体段、自动纵轴、平面角、顶面、旋转轴和三姿态路径。创建任务时设置 `sequence_source=visual_groups` 并复用同一 JSON：

```json
{
  "mode": "mesh_groups",
  "schema_version": 2,
  "model_fingerprint": "sha256:...",
  "longitudinal_axis": null,
  "vertical_axis": [0, 0, 1],
  "top_elevation_mm": null,
  "staging_clearance_mm": 800,
  "groups": [{
    "group_id": "G001",
    "name": "左侧腹板",
    "installation_step": 1,
    "installation_status": "pending",
    "bar_indices": [0, 3, 8],
    "plane_angle_deg": null,
    "rotation_axis": {"transverse_mm": null, "elevation_mm": null, "direction": null},
    "staging_clearance_mm": null
  }]
}
```

`model_fingerprint` 必须存在且与本次 IFC 一致。所有钢筋必须恰好属于一个非空组，组 ID 和正整数顺序必须唯一；角度、标高、轴坐标和抬高距离只接受有限数值，`preinstalled` 只接受 JSON 布尔值。`preinstalled` 组从第一帧起作为障碍物且不模拟。空参数由后台求解；人工旋转轴仍必须平行箱梁纵向，平面角限制为 `[-180°, 180°]`。某组发生碰撞后，后续组标记为 `not_evaluated_due_to_prior_failure`，不再假定失败组已经安装。网片组没有单一 TCP，`POST /api/tasks/{id}/robot` 会返回 `409`。

## Excel 顺序模板

`GET /api/templates/installation-sequence`

模板首个工作表第一行必须保留列名。推荐最简格式只填一列 `name`，内容填 BIM 中可对照的编号，例如 `640520`、`640522`。从第 2 行开始按行顺序填写，系统自动把行顺序作为安装顺序。`name` 列既支持完整名称，也支持完整名称最后一段编号（如 `主梁钢筋_N4a:主梁钢筋_N4a:640520` 可只填 `640520`）。仍兼容 `bar_id`、`bar_index`、`entity_id`、`guid`、`tag` 等列；如存在歧义，改用 `guid` 或 `bar_index` 消除歧义。方向列 `direction_x/y/z` 可选，但填写时必须三列齐全且为非零向量。顺序必须覆盖 IFC 中每根钢筋且不得重复。

## 六自由度参数

`PlanningOptions` 新增：

- `generate_assembly_paths`：是否执行第二步；
- `assembly_translation_step_mm`：碰撞插值最大平移步长；
- `assembly_rotation_step_deg`：碰撞插值最大旋转步长；
- `assembly_rrt_iterations`：直线和旋转折线失败后的 SE(3) RRT 迭代上限；
- `assembly_random_seed`：可复现随机种子。

`assembly_paths.json` 的 `control_poses` 使用 IFC 模型坐标，位置单位为 mm，四元数顺序为 `xyzw`。`status=collision_detected` 的钢筋不会进入 ABB/KUKA/UR 控制程序。
