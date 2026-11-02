import os
import tempfile
from unittest.mock import patch
os.environ['DB_PATH'] = tempfile.mktemp(suffix='.sqlite')
os.environ['DEMO_MODE'] = 'true'
from fastapi.testclient import TestClient
from app import main as m

def test_demo_records_moves_without_http():
    with patch.object(m.session, 'get', side_effect=AssertionError('network not allowed')):
        m.poll_once(); m.poll_once()
    with m.db_connect() as db:
        assert db.execute('select count(*) from nodes').fetchone()[0] == 2
        assert db.execute('select count(*) from moves').fetchone()[0] >= 1

def test_normalization():
    assert m.normalize_container({'Id':'x', 'Names':['/example']})['name'] == 'example'
    assert m.normalize_status(2) == 'down'

def test_routes():
    m.poll_once()
    c=TestClient(m.app)
    for url in ['/', '/node/1', '/moves', '/health']:
        assert c.get(url).status_code == 200
    assert c.get('/node/999').status_code == 404
