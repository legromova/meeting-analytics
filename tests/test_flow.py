import io
import json
import wave
from pathlib import Path

import imageio_ffmpeg
import pytest
from fastapi.testclient import TestClient

from meeting_app import ai, db, main


def wav_bytes(seconds=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(1)
        wav.setframerate(8000)
        wav.writeframes(b"\x80" * (8000 * seconds))
    return output.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "meetings.sqlite3")
    monkeypatch.setattr(db, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setattr(ai, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ai, "KEY_FILE", tmp_path / "openai.key.dpapi")
    main.pending.clear()
    with TestClient(main.app) as c:
        yield c


def login(c, email, password):
    r = c.post("/api/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": r.json()["csrf"]}


def test_full_flow_and_isolation(client, monkeypatch):
    c = client
    assert c.get("/api/bootstrap").json()["needs_admin"]
    assert c.post("/api/bootstrap", json={"email": "admin@test.local", "password": "password1234", "display_name": "Елена"}).status_code == 200
    h = login(c, "admin@test.local", "password1234")
    assert c.post("/api/admin/users", headers=h, json={"email": "other@test.local", "password": "password5678", "display_name": "Другой"}).status_code == 200
    assert c.post("/api/projects", headers=h, json={"name": "Работа"}).status_code == 200
    pid = c.get("/api/projects").json()[0]["id"]
    assert c.patch("/api/me", headers=h, json={"display_name": "Елена", "aliases": ["Лена"], "password": None}).status_code == 200
    raw = "Елена подготовит протокол. Иван проверит бюджет. Надо обсудить риски."
    r = c.post("/api/meetings", headers=h, data={"project_id": pid, "title": "Планёрка", "meeting_at": "2026-11-08T11:00", "transcript": raw})
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    assert c.get(f"/api/meetings/{mid}").json()["preview"][0]["text"] == raw
    assert c.post(f"/api/meetings/{mid}/analyze", headers=h).status_code == 400

    monkeypatch.setattr(ai, "key_present", lambda: True)
    def fake_analyze(*args):
        return {"summary": "Обсудили протокол и бюджет.", "topics": [], "decisions": [],
                "agreements": [], "open_questions": [], "risks": [], "next_steps": [],
                "tasks": [
                    {"text": "Подготовить протокол", "timestamp_seconds": None, "speaker_label": None, "assignee": "Елена", "is_current_user": True, "due_date": None, "due_date_original": None},
                    {"text": "Проверить бюджет", "timestamp_seconds": None, "speaker_label": None, "assignee": "Иван", "is_current_user": False, "due_date": None, "due_date_original": None},
                    {"text": "Обсудить риски", "timestamp_seconds": None, "speaker_label": None, "assignee": None, "is_current_user": False, "due_date": None, "due_date_original": None}]}
    monkeypatch.setattr(ai, "analyze", fake_analyze)
    assert c.post(f"/api/meetings/{mid}/analyze", headers=h).status_code == 200
    m = c.get(f"/api/meetings/{mid}").json()
    assert m["preview"] is None and m["status"] == "Готово"
    assert len(c.get("/api/tasks").json()) == 1
    assert raw not in db.DB_PATH.read_bytes().decode("utf-8", errors="ignore")
    task = c.get("/api/tasks").json()[0]
    task["status"] = "Готово"
    assert c.put(f"/api/tasks/{task['id']}", headers=h, json=task).status_code == 200
    assert c.get("/api/tasks").json()[0]["status"] == "Готово"
    results = m["results"]
    results["summary"] = "Изменённые итоги"
    assert c.put(f"/api/meetings/{mid}/results", headers=h, json=results).status_code == 200
    assert c.get(f"/api/meetings/{mid}").json()["results"]["summary"] == "Изменённые итоги"

    c.cookies.clear()
    h2 = login(c, "other@test.local", "password5678")
    assert c.get("/api/projects").json() == []
    assert c.get("/api/tasks").json() == []
    assert c.get(f"/api/projects/{pid}/meetings").status_code == 404
    assert c.get(f"/api/meetings/{mid}").status_code == 404
    assert c.get(f"/api/meetings/{mid}/audio").status_code == 404
    assert c.put(f"/api/tasks/{task['id']}", headers=h2, json=task).status_code == 404
    assert c.delete(f"/api/projects/{pid}", headers=h2).status_code == 404
    assert c.get("/api/admin/users").status_code == 403


def test_audio_formats_and_duration(client, monkeypatch, tmp_path):
    c = client
    c.post("/api/bootstrap", json={"email": "admin@test.local", "password": "password1234"})
    h = login(c, "admin@test.local", "password1234")
    pid = c.post("/api/projects", headers=h, json={"name": "Аудио"}).json()["id"]
    wav = tmp_path / "sample.wav"
    wav.write_bytes(wav_bytes())
    for ext in ("wav", "mp3", "m4a"):
        source = wav
        if ext != "wav":
            source = tmp_path / ("sample." + ext)
            import subprocess
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(wav), str(source)], check=True)
        r = c.post("/api/meetings", headers=h, data={"project_id": pid, "title": ext, "meeting_at": "2026-11-08T12:00"},
                   files={"file": (source.name, source.read_bytes())})
        assert r.status_code == 200, r.text
        mid = r.json()["id"]
        audio = c.get(f"/api/meetings/{mid}/audio")
        assert audio.status_code == 200 and audio.content == source.read_bytes()
    too_long = c.post("/api/meetings", headers=h, data={"project_id": pid, "title": "Длинная", "meeting_at": "2026-11-08T12:00"},
                      files={"file": ("long.wav", wav_bytes(301))})
    assert too_long.status_code == 400
    bad = c.post("/api/meetings", headers=h, data={"project_id": pid, "title": "Ложный", "meeting_at": "2026-11-08T12:00"},
                 files={"file": ("fake.mp3", b"not audio")})
    assert bad.status_code == 400


def test_repeat_processing_and_assignment_gate(client, monkeypatch):
    c = client
    c.post("/api/bootstrap", json={"email": "admin@test.local", "password": "password1234", "display_name": "Елена"})
    h = login(c, "admin@test.local", "password1234")
    pid = c.post("/api/projects", headers=h, json={"name": "Повтор"}).json()["id"]
    mid = c.post("/api/meetings", headers=h, data={"project_id": pid, "title": "Звонок", "meeting_at": "2026-11-08T12:00"},
                 files={"file": ("sample.wav", wav_bytes())}).json()["id"]
    monkeypatch.setattr(ai, "key_present", lambda: True)
    monkeypatch.setattr(ai, "transcribe", lambda _path: [{"start": 0.5, "end": 2.0, "speaker": "A", "text": "Елена подготовит протокол."}])
    task = {"text": "Подготовить протокол", "timestamp_seconds": 0.5, "speaker_label": "A", "assignee": "Елена",
            "is_current_user": True, "due_date": None, "due_date_original": None}
    monkeypatch.setattr(ai, "analyze", lambda *args: {"summary": "Протокол.", "topics": [], "decisions": [],
        "agreements": [], "open_questions": [], "risks": [], "next_steps": [], "tasks": [task]})
    for _ in range(2):
        assert c.post(f"/api/meetings/{mid}/transcribe", headers=h).status_code == 200
        assert c.post(f"/api/meetings/{mid}/analyze", headers=h).status_code == 200
    assert len(c.get("/api/tasks").json()) == 1
    assert c.get(f"/api/meetings/{mid}").json()["preview"] is None
    assert ai.explicit_assignment("Елена подготовит протокол.", "Елена")
    assert not ai.explicit_assignment("Мы подготовим протокол.", "Елена")
    assert not ai.explicit_assignment("Иван подготовит протокол.", "Елена")

@pytest.fixture
def video_client(client, monkeypatch):
    from meeting_app import incoming
    monkeypatch.setattr(incoming, 'process_one', lambda: False)
    c = client
    c.post('/api/bootstrap', json={'email': 'video@test.local', 'password': 'password1234', 'display_name': 'Елена'})
    h = login(c, 'video@test.local', 'password1234')
    pid = c.post('/api/projects', headers=h, json={'name': 'Видео'}).json()['id']
    return c, h, pid


def test_video_queue_dedup_pipeline_and_security(video_client, monkeypatch):
    from meeting_app import incoming
    c, h, pid = video_client
    real_process = __import__('importlib').reload(incoming).process_one
    # Stop the fixture's background worker while exercising deterministic processing.
    monkeypatch.setattr(incoming, 'process_one', lambda: False)
    monkeypatch.setattr(incoming, 'media', lambda p: ('mp4', 120))
    data = {'project_id': pid, 'meeting_at': '2026-11-13T12:00'}
    def upload():
        return c.post('/api/incoming', headers=h, data=data, files={'file': ('demo.mp4', b'original video')})
    response = upload()
    assert response.status_code == 202, response.text
    mid = response.json()['id']
    assert upload().json()['id'] == mid
    assert c.get('/api/incoming').json()['items'][0]['status'] == 'uploaded'
    assert c.get(f'/api/meetings/{mid}/video').content == b'original video'
    monkeypatch.setattr(ai, 'key_present', lambda: True)
    speech = 'Елена подготовит протокол. Уникальная исходная фраза 7391.'
    segments = [{'start': 1.0, 'end': 3.0, 'speaker': 'A', 'text': speech}]
    monkeypatch.setattr(ai, 'transcribe', lambda p: segments)
    def analyze(received, *args):
        assert received is segments
        return {'summary': 'Подготовят протокол.', 'topics': [], 'decisions': [], 'agreements': [], 'risks': [], 'next_steps': [], 'open_questions': [], 'tasks': [{'text': 'Подготовить протокол', 'assignee': 'Елена', 'is_current_user': True, 'timestamp_seconds': 999}]}
    monkeypatch.setattr(ai, 'analyze', analyze)
    def decode(args, **kwargs):
        Path(args[-1]).write_bytes(wav_bytes())
    monkeypatch.setattr(incoming.subprocess, 'run', decode)
    assert real_process()
    m = c.get(f'/api/meetings/{mid}').json()
    assert m['status'] == 'ready' and m['results']['tasks'][0]['timestamp_seconds'] is None
    task = c.get('/api/tasks').json()[0]
    task['status'] = 'Готово'
    task['title'] = 'Моя правка'
    assert c.put('/api/tasks/'+str(task['id']), headers=h, json=task).status_code == 200
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET state='error' WHERE meeting_id=?", (mid,))
        conn.execute("UPDATE meetings SET status='error' WHERE id=?", (mid,))
    assert c.post(f'/api/meetings/{mid}/retry', headers=h).status_code == 200
    assert c.post(f'/api/meetings/{mid}/retry', headers=h).status_code == 409
    real_process()
    tasks = c.get('/api/tasks').json()
    assert len(tasks) == 1 and tasks[0]['title'] == 'Моя правка' and tasks[0]['status'] == 'Готово'
    stats = c.get('/api/analytics').json()
    assert stats['counts'] == {'ready': 1, 'processing': 0, 'errors': 0}
    with db.connect() as conn:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    assert speech.encode() not in db.DB_PATH.read_bytes()
    assert not list((db.DATA_DIR/'work').iterdir())
    assert c.patch(f'/api/meetings/{mid}', json={'title':'x','meeting_at':'2026-11-13'}).status_code == 403
    c.post('/api/admin/users', headers=h, json={'email':'other@test.local','password':'password5678'})
    c.cookies.clear()
    h2 = login(c, 'other@test.local', 'password5678')
    assert c.get('/api/incoming').json()['items'] == []
    assert c.get('/api/analytics').json()['counts']['ready'] == 0
    for url in (f'/api/meetings/{mid}/video', f'/api/projects/{pid}/incoming', '/api/analytics?project_id='+str(pid)):
        assert c.get(url).status_code == 404
    assert c.post(f'/api/meetings/{mid}/retry', headers=h2).status_code == 404


def test_lease_recovery_and_folder_stability(video_client, monkeypatch):
    from meeting_app import incoming
    import time
    c, h, pid = video_client
    assert c.put(f'/api/projects/{pid}/incoming', headers=h, json={'enabled': True}).status_code == 200
    folder = Path(c.get(f'/api/projects/{pid}/incoming').json()['path'])
    source = folder/'record.mp4'
    source.write_bytes(b'video')
    monkeypatch.setattr(incoming, 'media', lambda p: ('mp4', 120))
    service = incoming.Service()
    service.scan()
    assert c.get('/api/incoming').json()['items'] == []
    service.seen[str(source)] = ((source.stat().st_size, source.stat().st_mtime_ns), time.time()-11)
    service.scan()
    mid = c.get('/api/incoming').json()['items'][0]['id']
    service.scan()
    assert len(c.get('/api/incoming').json()['items']) == 1
    with db.connect() as conn:
        for count in range(3):
            conn.execute("UPDATE jobs SET state='processing',lease=0,token='old' WHERE meeting_id=?", (mid,))
            incoming.recover(conn)
        assert conn.execute('SELECT state FROM jobs WHERE meeting_id=?', (mid,)).fetchone()[0] == 'error'
    with pytest.raises(ValueError):
        incoming.safe(db.DATA_DIR.parent/'outside')
    assert c.delete(f'/api/meetings/{mid}', headers=h).status_code == 200
    with db.connect() as conn:
        assert not conn.execute('SELECT 1 FROM jobs').fetchone()
    assert not list((db.DATA_DIR/'video').iterdir())


@pytest.mark.parametrize('extension', ['mp4', 'webm'])
def test_real_video_validation(tmp_path, extension):
    from meeting_app import incoming
    import subprocess
    path = tmp_path/('valid.'+extension)
    codec = ['-c:v','libvpx-vp9','-c:a','libopus'] if extension == 'webm' else ['-c:v','libx264','-c:a','aac']
    subprocess.run([incoming.ffmpeg(), '-y', '-v', 'error', '-f','lavfi','-i','color=s=16x16:r=1:d=120', '-f','lavfi','-i','anullsrc=r=8000:cl=mono', '-t','120', *codec, str(path)], check=True, capture_output=True)
    kind, duration = incoming.media(path)
    assert kind == extension and 120 <= duration <= 121
    fake = tmp_path/'fake.mp4'
    fake.write_bytes(b'not video')
    with pytest.raises(ValueError, match='формату'):
        incoming.media(fake)


def test_video_error_keeps_previous_results(video_client, monkeypatch):
    from meeting_app import incoming
    c, h, pid = video_client
    process = __import__('importlib').reload(incoming).process_one
    monkeypatch.setattr(incoming, 'process_one', lambda: False)
    monkeypatch.setattr(incoming, 'media', lambda p: ('mp4',120))
    mid = c.post('/api/incoming', headers=h, data={'project_id':pid,'meeting_at':'2026-11-13T12:00'}, files={'file':('a.mp4',b'video')}).json()['id']
    monkeypatch.setattr(ai, 'key_present', lambda: True)
    monkeypatch.setattr(incoming.subprocess, 'run', lambda args, **kw: Path(args[-1]).write_bytes(wav_bytes()))
    monkeypatch.setattr(ai, 'transcribe', lambda p: [{'start':0,'end':1,'speaker':'A','text':'Фраза, которую нельзя сохранять 321'}])
    monkeypatch.setattr(ai, 'analyze', lambda *args: {'invalid':'response'})
    with db.connect() as conn:
        conn.execute('UPDATE meetings SET results=? WHERE id=?', (json.dumps({'summary':'Предыдущий результат'}),mid))
    process()
    m = c.get(f'/api/meetings/{mid}').json()
    assert m['status'] == 'error' and m['results']['summary'] == 'Предыдущий результат'
    assert c.get('/api/analytics').json()['counts'] == {'ready':1,'processing':0,'errors':1}
    assert not c.get('/api/tasks').json()
    assert '321' not in m['error']
    assert not list((db.DATA_DIR/'work').iterdir())
    c.post(f'/api/meetings/{mid}/retry',headers=h)
    monkeypatch.setattr(ai, 'transcribe', lambda p: [])
    process()
    assert c.get(f'/api/meetings/{mid}').json()['error'] == 'В записи не обнаружена речь'

@pytest.mark.parametrize('duration,valid', [(119.99,False),(120,True),(300,True),(300.01,False)])
def test_video_duration_boundaries(tmp_path,monkeypatch,duration,valid):
    from meeting_app import incoming
    from types import SimpleNamespace
    path=tmp_path/'v.mp4'
    path.write_bytes(b'0000ftyp'+b'0'*20)
    minutes=int(duration//60)
    seconds=duration-minutes*60
    metadata=f'Duration: 00:{minutes:02}:{seconds:05.2f}, start: 0\nStream #0:0: Video: h264\nStream #0:1: Audio: aac'
    monkeypatch.setattr(incoming.subprocess,'run',lambda *a,**k:SimpleNamespace(stderr=metadata.encode()))
    if valid:
        assert incoming.media(path)[1]==duration
    else:
        with pytest.raises(ValueError,match='от 2 до 5'):
            incoming.media(path)


def test_migration_preserves_populated_v1(tmp_path,monkeypatch):
    import ast
    from meeting_app import db
    monkeypatch.setattr(db,'DATA_DIR',tmp_path)
    monkeypatch.setattr(db,'DB_PATH',tmp_path/'migration.sqlite3')
    monkeypatch.setattr(db,'AUDIO_DIR',tmp_path/'audio')
    tree=ast.parse(Path(db.__file__).read_text(encoding='utf-8'))
    sql=next(n.args[0].value for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='executescript')
    with db.connect() as conn:
        conn.executescript(sql)
        conn.execute("INSERT INTO users(id,email,password_hash,role) VALUES(1,'a','hash','admin')")
        conn.execute("INSERT INTO projects(id,owner_id,name) VALUES(1,1,'Old')")
        conn.execute("INSERT INTO meetings(id,owner_id,project_id,title,meeting_at,source_type) VALUES(1,1,1,'Old','2026-11-13','text')")
        conn.execute("INSERT INTO tasks(owner_id,project_id,meeting_id,title) VALUES(1,1,1,'Task')")
    db.migrate()
    db.migrate()
    with db.connect() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0]==2
        assert conn.execute('SELECT meeting_id FROM tasks').fetchone()[0]==1
        assert conn.execute('SELECT title FROM meetings').fetchone()[0]=='Old'
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        conn.execute("INSERT INTO meetings(owner_id,project_id,title,meeting_at,source_type) VALUES(1,1, 'New','2026-11-13','video')")


def test_cloud_setup_does_not_use_local_database(monkeypatch):
    from meeting_app import cloud
    monkeypatch.setenv('MEETING_CLOUD','1')
    for key in cloud.REQUIRED:
        monkeypatch.delenv(key,raising=False)
    with TestClient(main.app) as c:
        page=c.get('/')
        assert page.status_code==200 and 'DEPLOYMENT.md' in page.text
        health=c.get('/api/health')
        assert health.status_code==503 and 'DATABASE_URL' in health.json()['missing']
        assert c.get('/api/projects').status_code==503
        assert c.get('/api/runtime').json()=={'cloud':True}


def test_cloud_upload_owner_worker_and_private_media(client,monkeypatch):
    import sqlite3
    from meeting_app import cloud,postgres,incoming
    monkeypatch.setenv('MEETING_CLOUD','1')
    monkeypatch.setenv('MEETING_BOOTSTRAP_TOKEN','test-bootstrap-code')
    monkeypatch.setenv('MEETING_WORKER_SECRET','test-worker-code')
    main.app.state.configured=True
    def local_connection(url):
        conn=sqlite3.connect(db.DB_PATH,timeout=30)
        conn.row_factory=sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        return conn
    monkeypatch.setattr(postgres,'Connection',local_connection)
    monkeypatch.setenv('DATABASE_URL','test-local-adapter')
    with db.connect() as conn:
        conn.execute('CREATE TABLE uploads(id TEXT PRIMARY KEY,owner_id INTEGER,project_id INTEGER,path TEXT,title TEXT,meeting_at TEXT,filename TEXT,created_at REAL,meeting_id INTEGER)')
    monkeypatch.setattr(cloud,'signed',lambda path,upload=False:'https://private.invalid/'+path)
    assert client.post('/api/bootstrap',json={'email':'cloud@test','password':'password1234'}).status_code==403
    assert client.post('/api/bootstrap',json={'email':'cloud@test','password':'password1234','bootstrap_token':'test-bootstrap-code','display_name':'Елена'}).status_code==200
    h=login(client,'cloud@test','password1234')
    pid=client.post('/api/projects',headers=h,json={'name':'Облако'}).json()['id']
    data={'project_id':pid,'filename':'../../record.mp4','size':100,'meeting_at':'2026-11-13T12:00'}
    assert client.post('/api/cloud/uploads',json=data).status_code==403
    grant=client.post('/api/cloud/uploads',headers=h,json=data)
    assert grant.status_code==200
    assert '/uploads/1/' in grant.json()['url'] and '..' not in grant.json()['url']
    upload_id=grant.json()['id']
    monkeypatch.setattr(cloud,'download',lambda key,path:Path(path).write_bytes(b'video-original'))
    monkeypatch.setattr(cloud,'put',lambda key,path:None)
    monkeypatch.setattr(cloud,'delete',lambda key:None)
    dispatched=[]
    monkeypatch.setattr(cloud,'enqueue',lambda mid:dispatched.append(mid))
    monkeypatch.setattr(incoming,'media',lambda p:('mp4',120))
    monkeypatch.setattr(incoming,'process_one',lambda mid=None:False)
    accepted=client.post(f'/api/cloud/uploads/{upload_id}/complete',headers=h)
    assert accepted.status_code==202,accepted.text
    mid=accepted.json()['id']
    assert dispatched==[mid]
    assert client.post(f'/api/cloud/uploads/{upload_id}/complete',headers=h).json()['id']==mid
    media=client.get(f'/api/meetings/{mid}/video',follow_redirects=False)
    assert media.status_code==307 and media.headers['location'].startswith('https://private.invalid/video/')
    assert client.post('/api/cloud/worker',json={'meeting_id':mid}).status_code==403
    assert client.post('/api/cloud/worker',headers={'X-Worker-Token':'test-worker-code'},json={'meeting_id':mid}).status_code==503
    assert client.get(f'/api/projects/{pid}/incoming').json()['supported'] is False
    assert client.put('/api/admin/key',headers=h,json={'key':'fake'}).status_code==409
    client.post('/api/admin/users',headers=h,json={'email':'second@test','password':'password5678'})
    client.cookies.clear()
    other=login(client,'second@test','password5678')
    assert client.post(f'/api/cloud/uploads/{upload_id}/complete',headers=other).status_code==404
    assert client.get(f'/api/meetings/{mid}/video').status_code==404
    assert client.post('/api/cloud/uploads',headers=other,json=data).status_code==404


def test_postgres_query_adapter_preserves_parameters(monkeypatch):
    from meeting_app.postgres import Connection
    seen=[]
    class RawCursor:
        rowcount=1
        description=[]
        def fetchone(self):return [7]
    class RawConnection:
        def execute(self,sql,args=()):
            seen.append((sql,args))
            return RawCursor()
    connection=object.__new__(Connection)
    connection.conn=RawConnection()
    cursor=connection.execute('INSERT OR IGNORE INTO tasks(owner_id,title) VALUES(?,?)',(1,'private task'))
    assert cursor.lastrowid==7
    assert seen[-1][0]=='INSERT INTO tasks(owner_id,title) VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING id'
    assert seen[-1][1]==(1,'private task')
    connection.execute("SELECT * FROM sessions WHERE expires_at>datetime('now') AND user_id=?",(1,))
    assert "to_char(now()" in seen[-1][0] and seen[-1][1]==(1,)
    connection.execute('BEGIN IMMEDIATE')
    assert 'pg_advisory_xact_lock' in seen[-1][0]


def test_native_queue_configuration_and_identifier_only_payload(monkeypatch):
    import sys
    from types import ModuleType
    from meeting_app import cloud
    for key in cloud.REQUIRED:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('MEETING_QUEUE_PROVIDER','vercel')
    assert 'QSTASH_TOKEN' not in cloud.missing()
    assert 'MEETING_WORKER_SECRET' not in cloud.missing()
    assert 'DATABASE_URL' in cloud.missing()
    messages=[]
    fake=ModuleType('vercel.queue.sync')
    fake.send=lambda topic,payload:messages.append((topic,payload))
    monkeypatch.setitem(sys.modules,'vercel.queue.sync',fake)
    cloud.enqueue(42)
    assert messages==[('meeting-processing',{'meeting_id':42})]
