# REST API

## 创建任务

`POST /api/tasks`，`multipart/form-data`：

- `file`: IFC 文件。除网片组模式外只传一个；当 `sequence_source=visual_groups` 时可重复提交同名 `file` 字段，每个 IFC 自动对应一个网片组；
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

- `mesh_groups.json`：版本 4 网片定义、主体段掩码、拟合残差、平面角和全流程共用旋转中轴；
- `mesh_group_sequence.csv`：网片组顺序、状态、BIM ID 和成员；
- `mesh_group_paths.json`：首片准备、逐片水平竖降、累计钢筋笼转向下一片的姿态、运动/障碍组、碰撞结果，以及不做碰撞检查的 `final_restore`；
- `mesh_group_collisions.csv`：运动/障碍网片与钢筋 BIM ID、阶段、`collision_distance_mm`、最大碰撞距离、中心轴实际距离、要求距离、最深碰撞接触点和六自由度姿态；
- `viewer_model.json`：`assembly_unit=mesh_group`，浏览器按各组当前刚体姿态播放累计安装过程。

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

第一次调用 `POST /api/sequence/preview` 可选择两种输入方式：只传一个完整 IFC，随后点选/框选分组；或重复提交多个 `file` 字段，每个文件中的全部钢筋自动成为一个网片组。多文件必须使用同一工程坐标系，服务会保持原始世界坐标和 BIM `name`/ID，只重新分配跨文件唯一的内部钢筋索引。响应中的 `source_filenames`、`mesh_group_input_mode` 和 `suggested_mesh_groups` 给出文件顺序及自动分组，`model_fingerprint` 针对全部文件的合并模型。

完成分组或调整自动分组后，再向同一接口附加 `visual_sequence_json` 可预览主体段、自动纵轴、平面角、共用旋转中轴、自动安全抬高、累计装配路径和最终整笼回正。多文件预览和正式任务必须以相同顺序重复提交全部 `file` 字段。预览只求解运动姿态（阶段标记 `collision_checked=false`），正式任务才执行完整碰撞计算。创建任务时设置 `sequence_source=visual_groups` 并复用同一 JSON：

```json
{
  "mode": "mesh_groups",
  "schema_version": 4,
  "motion_model": "pending_group_descent_then_cumulative_rotation",
  "input_mode": "multiple_ifc_files",
  "model_fingerprint": "sha256:...",
  "vertical_axis": [0, 0, 1],
  "staging_clearance_mm": 800,
  "assembly_rotation_axis": {
    "transverse_mm": null,
    "elevation_mm": null,
    "direction": null
  },
  "groups": [{
    "group_id": "G001",
    "name": "左侧腹板",
    "source_filename": "左侧腹板.ifc",
    "installation_step": 1,
    "installation_status": "pending",
    "bar_indices": [0, 3, 8],
    "plane_angle_deg": null,
    "staging_clearance_mm": null
  }]
}
```

`model_fingerprint` 必须存在且与本次 IFC 一致。所有钢筋必须恰好属于一个非空组，组 ID 和正整数顺序必须唯一；角度、共用轴坐标和最低抬高距离只接受有限数值，`preinstalled` 只接受 JSON 布尔值。`preinstalled` 组从第一帧起属于累计已安装整体。空的共用中轴参数由后台根据模型主体点求解，人工方向仍必须平行箱梁纵向，平面角限制为 `[-180°, 180°]`。

若存在初始已安装组且首片装配角需要调整，顶层 `initial_preparation` 先记录 `initial_preparation_rotation`；第一片在自动安全高度保持水平并作为碰撞障碍。每个待安装步骤按顺序包含 `pending_group_descent` 和 `installed_assembly_rotation_to_next`，并给出 `installed_group_ids_before`、`installed_group_ids_after_descent`、`next_group_id`、当前/下一装配角、下一片高位姿态及阶段碰撞结果。第二阶段的运动集合包含当前刚安装网片；下一片保持在高位不动。最后一片没有转向下一片，随后执行 `final_restore_rotation`，其 `collision_checked=false`，只用于回到 IFC 姿态。碰撞不会中断模拟，碰撞结果直接输出 `collision_distance_mm = required_distance_mm - axis_distance_mm`；该正值表示两个中心轴胶囊体的重叠距离，数值越大表示碰撞越深。网片组没有单一 TCP，`POST /api/tasks/{id}/robot` 会返回 `409`。

## 复用历史网片任务重新计算

`POST /api/tasks/{id}/rerun`

仅适用于状态为已完成、失败或已取消的 `visual_groups` 任务。接口从历史任务目录复用全部原始 IFC、网片成员、安装顺序、已安装状态、平面角和最低抬高量，创建新的独立任务并返回 `202`；原任务与原结果不会被覆盖。版本 2、3 历史配置在新任务中迁移为版本 4：版本 3 的人工共用中轴继续保留；版本 2 的逐组旋转轴和顶面标高被忽略，共用中轴重新自动识别。单文件历史任务需要 `input.ifc`；多文件任务还会保存并复用 `input_models.json` 与 `input_models/`。两者均需要 `input_sequence.json` 或兼容的 `output/mesh_groups.json`。

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
