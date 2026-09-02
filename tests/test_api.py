from fastapi.testclient import TestClient
from io import BytesIO
import json
from openpyxl import load_workbook
import app.main as main_module
from app.main import app


def test_health():
    client=TestClient(app)
    r=client.get('/api/health')
    assert r.status_code==200
    assert r.json()['status']=='ok'


def test_index():
    client=TestClient(app)
    r=client.get('/')

    assert r.status_code==200
    assert '钢筋空间拓扑规划与碰撞检测系统' in r.text
    assert 'id="motionGuideToggle"' in r.text
    assert 'id="poseAxesToggle"' in r.text
    assert 'id="motionHud"' in r.text
    assert 'value="visual"' in r.text
    assert 'value="visual_groups"' in r.text
    assert 'id="visualSequenceEditor"' in r.text
    assert 'id="visualBoxSelectBtn"' in r.text
    assert 'id="meshGroupEditor"' in r.text
    assert 'id="meshConfirmGroupBtn"' in r.text
    assert 'id="meshSolveBtn"' in r.text
    assert 'id="meshPreviewSlider"' in r.text
    assert "框选多根时按模型索引依次追加" in r.text
    for speed in ("0.01", "0.03", "0.05", "0.1"):
        assert f'<option value="{speed}">{speed}×</option>' in r.text

def test_sequence_template():
    client=TestClient(app)
    r=client.get('/api/templates/installation-sequence')
    assert r.status_code==200
    assert r.content[:2]==b'PK'
    workbook = load_workbook(BytesIO(r.content), read_only=False)
    sheet = workbook["安装顺序"]
    assert [sheet.cell(1, col).value for col in range(1, 3)] == ["name", "installation_status"]
    assert len(sheet.data_validations.dataValidation) == 1
    workbook.close()


def test_visual_sequence_preview_parses_ifc():
    content = b"""ISO-10303-21;
HEADER;
FILE_SCHEMA(('IFC2X3'));
ENDSEC;
DATA;
#1=IFCCARTESIANPOINT((0.,0.,0.));
#2=IFCCARTESIANPOINT((100.,0.,0.));
#3=IFCPOLYLINE((#1,#2));
#4=IFCCOMPOSITECURVESEGMENT(.CONTINUOUS.,.T.,#3);
#5=IFCCOMPOSITECURVE((#4),.F.);
#6=IFCSWEPTDISKSOLID(#5,6.,$,0.,1.);
#7=IFCSHAPEREPRESENTATION($,'Body','AdvancedSweptSolid',(#6));
#8=IFCPRODUCTDEFINITIONSHAPE($,$,(#7));
#9=IFCREINFORCINGBAR('guid',$,'rebar:640520',$,$,$,#8,'640520',12.,113.,100.,.NOTDEFINED.,$);
ENDSEC;
END-ISO-10303-21;
"""
    client = TestClient(app)
    response = client.post(
        "/api/sequence/preview",
        files={"file": ("preview.ifc", content, "application/octet-stream")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"]["rebar_count"] == 1
    assert payload["bars"][0]["n"] == "640520"
    assert payload["bars"][0]["p"] == [[0.0, 0.0, 0.0], [100.0, 0.0, 0.0]]


def test_visual_mesh_group_preview_resolves_shared_rigid_path():
    content = b"""ISO-10303-21;
HEADER;
FILE_SCHEMA(('IFC2X3'));
ENDSEC;
DATA;
#1=IFCCARTESIANPOINT((0.,0.,0.));
#2=IFCCARTESIANPOINT((100.,0.,0.));
#3=IFCPOLYLINE((#1,#2));
#4=IFCCOMPOSITECURVESEGMENT(.CONTINUOUS.,.T.,#3);
#5=IFCCOMPOSITECURVE((#4),.F.);
#6=IFCSWEPTDISKSOLID(#5,6.,$,0.,1.);
#7=IFCSHAPEREPRESENTATION($,'Body','AdvancedSweptSolid',(#6));
#8=IFCPRODUCTDEFINITIONSHAPE($,$,(#7));
#9=IFCREINFORCINGBAR('guid',$,'rebar:640520',$,$,$,#8,'640520',12.,113.,100.,.NOTDEFINED.,$);
ENDSEC;
END-ISO-10303-21;
"""
    client = TestClient(app)
    initial = client.post(
        "/api/sequence/preview",
        files={"file": ("preview.ifc", content, "application/octet-stream")},
    )
    assert initial.status_code == 200
    fingerprint = initial.json()["model_fingerprint"]
    groups = {
        "mode": "mesh_groups",
        "schema_version": 2,
        "model_fingerprint": fingerprint,
        "longitudinal_axis": [1, 0, 0],
        "top_elevation_mm": 0,
        "staging_clearance_mm": 800,
        "groups": [{
            "group_id": "G001",
            "name": "顶板",
            "installation_step": 1,
            "installation_status": "pending",
            "bar_indices": [0],
            "plane_angle_deg": 0,
        }],
    }
    response = client.post(
        "/api/sequence/preview",
        files={"file": ("preview.ifc", content, "application/octet-stream")},
        data={"visual_sequence_json": json.dumps(groups, ensure_ascii=False)},
    )
    assert response.status_code == 200
    resolved = response.json()["mesh_groups"]
    assert resolved["mode"] == "mesh_groups"
    assert resolved["group_count"] == 1
    group = resolved["groups"][0]
    assert group["bar_indices"] == [0]
    assert [pose["label"] for pose in group["control_poses"]] == [
        "水平初始态", "顶面过渡态", "IFC 最终安装态"
    ]
    assert group["control_poses"][0]["position_mm"][2] == 800.0


def test_visual_mesh_group_preview_rejects_wrong_model_fingerprint():
    content = b"""ISO-10303-21;
HEADER;
FILE_SCHEMA(('IFC2X3'));
ENDSEC;
DATA;
#1=IFCCARTESIANPOINT((0.,0.,0.));
#2=IFCCARTESIANPOINT((100.,0.,0.));
#3=IFCPOLYLINE((#1,#2));
#4=IFCCOMPOSITECURVESEGMENT(.CONTINUOUS.,.T.,#3);
#5=IFCCOMPOSITECURVE((#4),.F.);
#6=IFCSWEPTDISKSOLID(#5,6.,$,0.,1.);
#7=IFCSHAPEREPRESENTATION($,'Body','AdvancedSweptSolid',(#6));
#8=IFCPRODUCTDEFINITIONSHAPE($,$,(#7));
#9=IFCREINFORCINGBAR('guid',$,'rebar:640520',$,$,$,#8,'640520',12.,113.,100.,.NOTDEFINED.,$);
ENDSEC;
END-ISO-10303-21;
"""
    group_payload = {
        "mode": "mesh_groups", "schema_version": 2,
        "model_fingerprint": "sha256:not-this-model",
        "longitudinal_axis": [1, 0, 0],
        "groups": [{"group_id": "G1", "installation_step": 1, "bar_indices": [0]}],
    }
    response = TestClient(app).post(
        "/api/sequence/preview",
        files={"file": ("preview.ifc", content, "application/octet-stream")},
        data={"visual_sequence_json": json.dumps(group_payload)},
    )
    assert response.status_code == 422
    assert "不同的 IFC" in response.json()["detail"]


def test_visual_mesh_group_task_forces_collision_and_disables_robot(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(main_module, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(
        main_module,
        "create_task",
        lambda task_id, filename, options: captured.update(
            task_id=task_id, filename=filename, options=options
        ),
    )
    monkeypatch.setattr(main_module, "launch_worker", lambda mode, task_id, config=None: None)
    group_payload = {
        "mode": "mesh_groups",
        "schema_version": 2,
        "model_fingerprint": "sha256:preview-fingerprint",
        "groups": [{"group_id": "G1", "installation_step": 1, "bar_indices": [0]}],
    }
    response = TestClient(app).post(
        "/api/tasks",
        files={"file": ("model.ifc", b"IFC", "application/octet-stream")},
        data={
            "options_json": json.dumps(
                {
                    "sequence_source": "visual_groups",
                    "generate_assembly_paths": False,
                    "generate_robot_path": True,
                }
            ),
            "visual_sequence_json": json.dumps(group_payload),
        },
    )
    assert response.status_code == 202
    assert captured["options"]["sequence_source"] == "visual_groups"
    assert captured["options"]["generate_assembly_paths"] is True
    assert captured["options"]["generate_robot_path"] is False
    saved = json.loads(
        (tmp_path / captured["task_id"] / "input_sequence.json").read_text(encoding="utf-8")
    )
    assert saved["mode"] == "mesh_groups"


def test_mesh_group_task_rejects_robot_regeneration(monkeypatch):
    monkeypatch.setattr(
        main_module,
        "get_task",
        lambda task_id: {
            "id": task_id,
            "status": "completed",
            "options": {"sequence_source": "visual_groups"},
        },
    )
    response = TestClient(app).post(
        "/api/tasks/group-task/robot",
        json={
            "linear_speed_mm_s": 600,
            "angular_speed_deg_s": 45,
            "sample_period_s": 0.1,
            "outside_margin_mm": 800,
            "preinsert_distance_mm": 250,
            "retreat_distance_mm": 300,
            "grasp_fraction": 0.5,
        },
    )
    assert response.status_code == 409
    assert "多点夹具" in response.json()["detail"]
