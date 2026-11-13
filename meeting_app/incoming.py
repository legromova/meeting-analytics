"""Durable video intake and worker. No speech text is persisted."""
import hashlib
import json
import math
import os
import re
from functools import wraps
import stat
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import imageio_ffmpeg
from fastapi import HTTPException
from . import db, ai

MAX_VIDEO = 200 * 1024 * 1024
LOCK = threading.RLock()


def directory(name):
    path = db.DATA_DIR / name
    safe(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe(path):
    root = db.DATA_DIR.resolve()
    absolute = Path(os.path.abspath(path))
    if not absolute.is_relative_to(root):
        raise ValueError('Unsafe path')
    for part in [absolute, *absolute.parents]:
        if part.exists() and (part.is_symlink() or getattr(part.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Unsafe link')
        if part == root:
            break
    return absolute


def ffmpeg():
    return os.getenv('MEETING_FFMPEG') or imageio_ffmpeg.get_ffmpeg_exe()


def serialized(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with LOCK:
            return fn(*args, **kwargs)
    return wrapped


def media(path):
    """Probe and fully decode with explicit bundled FFmpeg; no ffprobe dependency."""
    if path.stat().st_size > MAX_VIDEO:
        raise ValueError('Размер файла превышает 200 МиБ')
    ext = path.suffix.lower()
    if ext not in ('.mp4', '.webm'):
        raise ValueError('Поддерживаются WebM и MP4')
    try:
        with path.open('rb') as source:
            header = source.read(4096)
        if (ext == '.mp4' and header[4:8] != b'ftyp') or (ext == '.webm' and (header[:4] != bytes.fromhex('1a45dfa3') or b'webm' not in header)):
            raise ValueError('Содержимое файла не соответствует формату')
        probe = subprocess.run([ffmpeg(), '-hide_banner', '-i', str(path)], capture_output=True, timeout=30)
        metadata = probe.stderr.decode('utf-8', errors='replace')
        if not re.search(r'Stream #.*Video:', metadata):
            raise ValueError('Файл не содержит видеопоток')
        if not re.search(r'Stream #.*Audio:', metadata):
            raise ValueError('В видео отсутствует звуковая дорожка')
        match = re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', metadata)
        if not match:
            raise ValueError('Файл повреждён или кодек не поддерживается')
        duration = int(match[1])*3600 + int(match[2])*60 + float(match[3])
        if not 120 <= duration <= 300:
            raise ValueError('Запись должна длиться от 2 до 5 минут')
        subprocess.run([ffmpeg(), '-v', 'error', '-xerror', '-i', str(path), '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'], capture_output=True, timeout=120, check=True)
        return ext[1:], duration
    except (OSError, subprocess.SubprocessError):
        raise ValueError('Файл повреждён или кодек не поддерживается') from None


def accept(path, owner, project, title, when, intake='upload'):
    kind, duration = media(path)
    with path.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    name = uuid.uuid4().hex + '.' + kind
    target = directory('video') / name
    with LOCK, db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if not conn.execute('SELECT 1 FROM projects WHERE id=? AND owner_id=?', (project, owner)).fetchone():
            raise ValueError('Проект недоступен')
        existing = conn.execute('SELECT id FROM meetings WHERE owner_id=? AND project_id=? AND content_hash=?', (owner, project, digest)).fetchone()
        if existing:
            path.unlink(missing_ok=True)
            return existing[0]
        original = path.name.split('__', 1)[-1]
        path.replace(target)
        try:
            cur = conn.execute("INSERT INTO meetings(owner_id,project_id,title,meeting_at,source_type,status,video_name,original_name,content_hash,video_format,video_size,audio_duration,intake,auto_date) VALUES(?,?,?,?,'video','uploaded',?,?,?,?,?,?,?,?)", (owner, project, title, when, name, original, digest, kind, target.stat().st_size, duration, intake, int(intake == 'folder')))
            mid = cur.lastrowid
            conn.execute('INSERT INTO jobs(meeting_id,owner_id) VALUES(?,?)', (mid, owner))
            conn.commit()
            return mid
        except Exception:
            target.unlink(missing_ok=True)
            raise


def sync_tasks(conn, meeting, results):
    import difflib
    previous = json.loads(meeting.get('results') or '{}').get('tasks', [])
    used = set()
    for index, task in enumerate(results['tasks']):
        if not task['is_current_user']:
            continue
        key = hashlib.sha256((' '.join(task['text'].casefold().split()) + '|' + (task['assignee'] or '').casefold()).encode()).hexdigest()
        for prior in previous:
            prior_key = hashlib.sha256((' '.join(prior['text'].casefold().split()) + '|' + (prior.get('assignee') or '').casefold()).encode()).hexdigest()
            if prior_key not in used and (prior.get('assignee') or '').casefold() == (task.get('assignee') or '').casefold() and difflib.SequenceMatcher(None, prior['text'].casefold(), task['text'].casefold()).ratio() >= .8:
                key = prior_key
                break
        used.add(key)
        old = conn.execute('SELECT id FROM tasks WHERE owner_id=? AND meeting_id=? AND (source_key=? OR (source_key IS NULL AND lower(title)=lower(?)))', (meeting['owner_id'], meeting['id'], key, task['text'])).fetchone()
        if old:
            conn.execute('UPDATE tasks SET source_key=? WHERE id=?', (key, old[0]))
        else:
            conn.execute('INSERT OR IGNORE INTO tasks(owner_id,project_id,meeting_id,source_key,title,assignee,due_date) VALUES(?,?,?,?,?,?,?)', (meeting['owner_id'], meeting['project_id'], meeting['id'], key, task['text'], task['assignee'] or '', task['due_date']))


def recover(conn):
    expired = list(conn.execute("SELECT * FROM jobs WHERE state='processing' AND lease<?", (time.time(),)))
    for job in expired:
        state = 'error' if job['interruptions'] + 1 >= 3 else 'uploaded'
        conn.execute('UPDATE jobs SET state=?,interruptions=interruptions+1,token=NULL,lease=NULL WHERE meeting_id=?', (state, job['meeting_id']))
        conn.execute('UPDATE meetings SET status=?,stage=NULL,error=? WHERE id=?', (state, 'Обработка прерывалась три раза. Повторите обработку.' if state == 'error' else None, job['meeting_id']))


def process_one():
    with LOCK, db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        recover(conn)
        row = conn.execute("SELECT m.*,u.display_name,u.aliases,p.name AS project_name FROM jobs j JOIN meetings m ON m.id=j.meeting_id AND m.owner_id=j.owner_id JOIN users u ON u.id=m.owner_id JOIN projects p ON p.id=m.project_id AND p.owner_id=m.owner_id WHERE j.state='uploaded' ORDER BY m.id LIMIT 1").fetchone()
        if not row:
            return False
        m = dict(row)
        token = uuid.uuid4().hex
        conn.execute("UPDATE jobs SET state='processing',attempt=attempt+1,token=?,lease=? WHERE meeting_id=?", (token, time.time()+30, m['id']))
        conn.execute("UPDATE meetings SET status='processing',stage='Извлечение аудио',attempts=attempts+1,error=NULL WHERE id=?", (m['id'],))
    done = threading.Event()
    def heartbeat():
        while not done.wait(5):
            with db.connect() as conn:
                conn.execute('UPDATE jobs SET lease=? WHERE meeting_id=? AND token=?', (time.time()+30, m['id'], token))
    beat = threading.Thread(target=heartbeat, daemon=True)
    beat.start()
    temporary = directory('work') / (token + '.wav')
    audio_target = None
    segments = None
    try:
        if not ai.key_present():
            raise ValueError('Администратору нужно настроить ключ OpenAI')
        subprocess.run([ffmpeg(), '-y', '-v', 'error', '-i', str(safe(directory('video') / m['video_name'])), '-map', '0:a:0', '-vn', '-c:a', 'pcm_s32le', str(temporary)], check=True, capture_output=True, timeout=120)
        def stage(text):
            with db.connect() as conn:
                conn.execute('UPDATE meetings SET stage=? WHERE id=? AND EXISTS(SELECT 1 FROM jobs WHERE meeting_id=? AND token=?)', (text, m['id'], m['id'], token))
        stage('Распознавание речи')
        segments = ai.transcribe(temporary)
        if not any(s.get('text', '').strip() for s in segments):
            raise ValueError('В записи не обнаружена речь')
        stage('Анализ содержания')
        results = ai.Results.model_validate(ai.analyze(segments, m['title'], m['meeting_at'], m['project_name'], m['display_name'], json.loads(m['aliases']))).model_dump()
        ai.validate_evidence(results, segments, m['audio_duration'], m['display_name'], json.loads(m['aliases']))
        stage('Сохранение результатов')
        with LOCK, db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute("SELECT 1 FROM jobs WHERE meeting_id=? AND token=? AND state='processing' AND lease>?", (m['id'], token, time.time())).fetchone():
                return True
            audio_name = m['audio_name'] or (uuid.uuid4().hex + '.wav')
            audio_target = safe(db.AUDIO_DIR / audio_name)
            temporary.replace(audio_target)
            sync_tasks(conn, m, results)
            conn.execute("UPDATE meetings SET status='ready',stage=NULL,error=NULL,results=?,audio_name=?,audio_format='wav',processed_at=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(results, ensure_ascii=False), audio_name, m['id']))
            conn.execute("UPDATE jobs SET state='ready',interruptions=0,token=NULL,lease=NULL WHERE meeting_id=? AND token=?", (m['id'], token))
    except Exception as exc:
        from openai import APIConnectionError, AuthenticationError, PermissionDeniedError, RateLimitError
        if isinstance(exc, ValueError) and str(exc) in ('В записи не обнаружена речь', 'Администратору нужно настроить ключ OpenAI'):
            error = str(exc)
        elif isinstance(exc, (AuthenticationError, PermissionDeniedError)):
            error = 'Администратору нужно проверить ключ OpenAI'
        elif isinstance(exc, RateLimitError):
            error = 'Исчерпана квота или превышен лимит запросов'
        elif isinstance(exc, APIConnectionError):
            error = 'Нет соединения с сервисом распознавания'
        elif isinstance(exc, subprocess.SubprocessError):
            error = 'Файл повреждён или кодек не поддерживается'
        else:
            error = 'Не удалось получить структурированные итоги'
        with db.connect() as conn:
            if conn.execute('SELECT 1 FROM jobs WHERE meeting_id=? AND token=?', (m['id'], token)).fetchone():
                conn.execute("UPDATE meetings SET status='error',stage=NULL,error=? WHERE id=?", (error, m['id']))
                conn.execute("UPDATE jobs SET state='error',token=NULL,lease=NULL WHERE meeting_id=? AND token=?", (m['id'], token))
    finally:
        segments = None
        temporary.unlink(missing_ok=True)
        done.set()
        beat.join()
    return True


class Service:
    def __init__(self):
        self.stop = threading.Event()
        self.seen = {}
        self.threads = []

    def scan(self):
        with db.connect() as conn:
            settings = list(conn.execute('SELECT s.* FROM incoming_settings s JOIN projects p ON p.id=s.project_id AND p.owner_id=s.owner_id WHERE enabled=1'))
        for captured in directory('staging').iterdir():
            match = re.match(r'(\d+)_(\d+)_[0-9a-f]+__(.+)', captured.name)
            if not match:
                continue
            owner, project = int(match[1]), int(match[2])
            try:
                accept(captured, owner, project, Path(match[3]).stem[:300], datetime.fromtimestamp(captured.stat().st_mtime).isoformat(), 'folder')
            except ValueError:
                captured.replace(directory('quarantine') / captured.name)
        for s in settings:
            try:
                folder = safe(directory('incoming') / str(s['owner_id']) / str(s['project_id']))
                folder.mkdir(parents=True, exist_ok=True)
                for path in folder.iterdir():
                    if path.suffix.lower() not in ('.mp4', '.webm') or not stat.S_ISREG(path.lstat().st_mode):
                        continue
                    safe(path)
                    st = path.stat()
                    signature = (st.st_size, st.st_mtime_ns)
                    old = self.seen.get(str(path))
                    if not old or old[0] != signature:
                        self.seen[str(path)] = (signature, time.time())
                        continue
                    if time.time()-old[1] < 10:
                        continue
                    captured = directory('staging') / (str(s['owner_id']) + '_' + str(s['project_id']) + '_' + uuid.uuid4().hex + '__' + path.name)
                    with path.open('rb'):
                        pass
                    path.replace(captured)
                    if (captured.stat().st_size, captured.stat().st_mtime_ns) != signature:
                        captured.replace(path)
                        self.seen.pop(str(path), None)
                        continue
                    try:
                        accept(captured, s['owner_id'], s['project_id'], path.stem[:300], datetime.fromtimestamp(old[1]).isoformat(timespec='seconds'), 'folder')
                    except ValueError as exc:
                        captured.replace(directory('quarantine') / captured.name)
                        with db.connect() as conn:
                            conn.execute('INSERT INTO incoming_errors(owner_id,project_id,filename,error) VALUES(?,?,?,?)', (s['owner_id'], s['project_id'], path.name, str(exc)))
                    self.seen.pop(str(path), None)
            except (OSError, ValueError):
                continue

    def start(self):
        # Work derivatives never contain transcripts. Only clear files not owned by live leases.
        with db.connect() as conn:
            recover(conn)
            active = {r[0] + '.wav' for r in conn.execute("SELECT token FROM jobs WHERE state='processing' AND token IS NOT NULL")}
        for path in directory('work').iterdir():
            if path.is_file() and path.name not in active:
                safe(path).unlink()
        def worker():
            while not self.stop.is_set():
                try:
                    process_one()
                except Exception:
                    pass
                self.stop.wait(1)
        def scanner():
            while not self.stop.is_set():
                try:
                    self.scan()
                except Exception:
                    pass
                self.stop.wait(5)
        self.threads = [threading.Thread(target=f, daemon=True) for f in (worker, scanner)]
        for thread in self.threads:
            thread.start()

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join()
