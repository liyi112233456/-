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
