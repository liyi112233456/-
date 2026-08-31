from fastapi.testclient import TestClient
from io import BytesIO
from openpyxl import load_workbook
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
    assert 'id="visualSequenceEditor"' in r.text
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
