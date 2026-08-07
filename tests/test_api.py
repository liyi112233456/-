from fastapi.testclient import TestClient
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
    assert '钢筋空间拓扑' in r.text

def test_sequence_template():
    client=TestClient(app)
    r=client.get('/api/templates/installation-sequence')
    assert r.status_code==200
    assert r.content[:2]==b'PK'
