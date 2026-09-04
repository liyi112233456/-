# 钢筋空间拓扑规划与碰撞检测系统

一个可直接部署的前后端一体化系统，将以下能力统一到同一个任务流程中：

- 上传 IFC2X3 / IFC4 模型；
- 识别 `IfcReinforcingBar`，以及 Revit 导出为 `IfcBuildingElementProxy + IfcMappedItem` 的钢筋；
- 从 `IfcFacetedBrep` 弯曲管状实体和 `IfcExtrudedAreaSolid` 直筋实体恢复中心轴；
- 在 X/Y/Z 正反方向和循环核心任意方向上构造空间拓扑，生成安装顺序；
- 可上传 Excel/CSV/TSV 人工安装顺序，最简只需一列 `name`，填写 BIM 可对照编号（如 `640520`），按行顺序替代自动拓扑顺序；
- 可在网页三维模型中点选钢筋或框选单根/多根钢筋、搜索 BIM ID、拖动调整顺序，并标记已安装钢筋，保存后直接进入碰撞计算；
- 可把全部钢筋点选/框选为任意数量的网片组，拖动指定组安装顺序，并整组标记为已安装；
- 网片模式既可上传一个完整 IFC 后人工分组，也可一次上传多个 IFC，并将每个 IFC 中的全部钢筋自动作为一个网片组；多文件保持原工程坐标和 BIM `name`/ID，可直接修改组名、顺序、已安装状态与路径参数；
- 自动识别箱梁纵向和网片主体平面，拟合时排除离面弯钩/弯腿，完整钢筋仍参与动画与碰撞；
- 按“首片准备 → 当前待安装网片水平竖降 → 当前片加入后累计钢筋笼整体转向下一片”的连续装配过程校核碰撞，最后整笼回正到 IFC 姿态；
- 可对已完成的网片组任务直接复用原 IFC、分组、参数和安装顺序创建新计算任务，无需重新框选；
- Directly parse IfcReinforcingBar products with IfcSweptDiskSolid / IfcCompositeCurve / IfcPolyline / IfcTrimmedCurve geometry, including circular bends, in addition to mapped BREP and extrusion geometry.
- Generate an editable manual installation-order Excel workbook from an IFC via `POST /api/sequence/generate` or the web page button.
- In manual-sequence files, `installation_status` can mark bars as `pending` or `preinstalled`; preinstalled bars skip animation and robot-path generation but remain fixed collision obstacles for every pending bar.
- 对每根钢筋执行刚体六自由度路径搜索、任意转动和离散胶囊体碰撞检查；
- 在网页中直接旋转、缩放和播放全部钢筋的安装动画；
- 生成机械臂 TCP 位姿轨迹；
- 导出 CSV、JSON、ABB RAPID、KUKA KRL 和 URScript；
- 通过 SSE 实时显示后台计算进度；
- 任务、日志和结果包持久化。

## 一键启动

### Docker

```bash
cd rebar_planning_system
docker compose up --build
```

浏览器打开：`http://localhost:8000`

API 文档：`http://localhost:8000/docs`

### 本地 Python

```bash
cd backend
python -m pip install -r requirements.txt
../scripts/start_local.sh
```

## 页面流程

1. 拖入一个完整 IFC；或在网片模式选择多个独立网片 IFC（每个文件自动成为一组）；
2. 选择自动多方向拓扑、Excel 人工顺序、单筋可视化顺序或可视化网片组顺序，并配置碰撞离散步长；
3. 后台独立进程执行解析与规划；
4. SSE 实时显示阶段和进度；
5. 计算完成后网页自动加载三维模型；
6. 播放、暂停、单步、旋转和缩放安装动画；
7. 播放钢筋的曲线平移与任意旋转，叠加经碰撞检查通过的机器人路径；
8. 下载完整结果包或单个控制器程序。

## 目录

```text
backend/app/main.py                 FastAPI API 与静态网页服务
backend/app/worker.py               独立计算进程入口
backend/app/services/ifc_geometry.py IFC STEP 解析和轴线恢复
backend/app/services/planner.py     多方向空间拓扑排序
backend/app/services/sequence_io.py  Excel/CSV/TSV 人工顺序导入
backend/app/services/assembly_path.py 六自由度刚体路径搜索与碰撞检查
backend/app/services/mesh_groups.py 网片主体拟合、姿态解算与组级碰撞检查
backend/app/services/robot_path.py  TCP 轨迹与控制器导出
backend/app/static/                 前端页面、样式和三维播放器
backend/data/jobs/                  任务数据目录
tools/open3d_desktop.py             可选 Open3D 桌面播放器
docs/API.md                         API 说明
docs/ARCHITECTURE.md                系统架构与证明边界
```

## 结果文件

```text
output/rebar_axes.json
output/installation_sequence.csv
output/viewer_model.json
output/assembly_paths.json
output/collision_report.json
output/assembly_path_waypoints.csv
output/planning_summary.json
output/robot/tcp_trajectory.csv
output/robot/robot_waypoints.json
output/robot/rebar_install.mod
output/robot/rebar_install.src
output/robot/rebar_install.script
result_bundle.zip
```

网片组模式改为输出：

```text
output/rebar_axes.json
output/mesh_groups.json
output/mesh_group_sequence.csv
output/mesh_group_paths.json
output/mesh_group_collisions.csv
output/viewer_model.json
output/planning_summary.json
result_bundle.zip
```

## 规划模式说明

第一步 `multi_direction_spatial_topology` 把钢筋表示为带半径中心轴胶囊体，同时检查配置坐标轴的正反扫掠方向；循环核心会补充钢筋端部切线、主方向、径向和斜向候选。自动顺序也可由 Excel 或网页三维可视化人工顺序完全替代。

第二步 `rigid_rebar_discrete_se3` 按安装顺序把既有钢筋作为动态障碍物，对当前钢筋的三维平移和四元数旋转进行离散碰撞检查。直线进入失败时依次尝试旋转折线路径和 SE(3) RRT 曲线路径。输出会明确区分通过与失败；失败步骤不会写入机器人控制器程序。

网片模式 `pending_group_descent_then_cumulative_rotation` 使用一条穿过钢筋笼横截面中心的共用纵向中轴。若存在初始已安装组，系统先执行首片准备旋转，使第一待装面进入水平装配角；没有初始已安装组时首片直接水平竖降。每片下降到位后立即加入累计钢筋笼，随后包含当前片的整个已安装整体转向下一待装面的水平装配角；下一片在高位保持水平且不随之旋转。最后一片不再执行组间转向，而是生成不参与碰撞统计的整笼回正动画，使模型回到 IFC 最终姿态。

首片准备阶段检查初始已安装整体与高位第一片；组间转向阶段检查累计已安装整体与高位下一片；下降阶段只检查当前网片与累计已安装整体。更后面的未来组不出现，组内和已经连接的组间接触忽略。发现碰撞后仍继续采样当前完整路径及所有后续网片，不调用 RRT。结果按运动钢筋与障碍钢筋直接汇总碰撞距离（胶囊体要求距离减去中心轴实际距离，单位 mm），并在 `mesh_group_collisions.csv` 中输出最大碰撞距离、最深碰撞姿态与接触点。网片平面只用自动识别的主体段拟合，弯钩不会影响平面角，但始终保留在完整扫掠碰撞中。当前未定义多点夹具和组级 TCP，因此网片模式不输出 ABB/KUKA/UR 程序。

在任务列表中选中已完成、失败或已取消的网片组任务，可点击“沿用原网片分组与顺序重新计算”。系统创建独立的新任务，复用历史任务保存的 IFC、全部网片成员、安装顺序、已安装状态、平面角和最低抬高量，原任务及其旧版动画结果保持不变。版本 2、3 配置会自动迁移到版本 4；版本 3 的人工共用中轴继续保留，版本 2 的逐组旋转轴和顶面标高不带入新运动模型，共用中轴会重新自动识别，因此无需重新框选或排序。历史任务目录中的输入文件若已被人工清理，则不能复用。

碰撞证书范围是“钢筋中心轴胶囊体 + 给定平移/旋转离散步长”。仅在精确 IFC 终点姿态、且对应最终接触线段对一致时保留设计接触；提前接触仍判为碰撞。混凝土、模板、支架、吊具、夹具、机器人完整连杆、线缆、人员空间、制造误差和弹性变形仍需在 RobotStudio、KUKA.Sim、RoboDK、URSim 或等效平台复核。

## Open3D 桌面模式

网页播放器不需要 Open3D。需要桌面 Open3D 窗口时：

```bash
pip install open3d
python tools/open3d_desktop.py \
  --axes backend/data/jobs/<id>/output/rebar_axes.json \
  --sequence backend/data/jobs/<id>/output/installation_sequence.csv
```

## 针对上传箱梁模型的验证

系统解析器已针对所提供 IFC 的两种钢筋表达完成验证：

- 3424 根钢筋实例；
- 169 类可复用几何；
- 9831.489 m 中心轴；
- 直径 8–20 mm；
- 网页模型包含全部钢筋，不做数量截断。
