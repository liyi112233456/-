# REST API

## 创建任务

`POST /api/tasks`，`multipart/form-data`：

- `file`: IFC 文件；
- `options_json`: `PlanningOptions` JSON；
- `sequence_file`: 当 `sequence_source=excel` 时必填，支持 `.xlsx`、`.csv`、`.tsv`。

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
