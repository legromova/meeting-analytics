"""Authenticated incoming and analytics routes."""
import json
import uuid
from datetime import datetime, date, timedelta
from pathlib import Path
from fastapi import Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from . import db, incoming, cloud
from .security import current_user


def install(app, owned, public_meeting, save_upload):
    @app.post('/api/incoming', status_code=202)
    async def upload(project_id: int = Form(...), meeting_at: str = Form(...), title: str = Form(''), file: UploadFile = File(...), user=Depends(current_user)):
        owned('projects', project_id, user['id'])
        try:
            datetime.fromisoformat(meeting_at)
        except ValueError:
            raise HTTPException(400, 'Укажите дату встречи')
        if cloud.enabled():
            raise HTTPException(409,'Используйте прямую облачную загрузку')
        filename = Path((file.filename or '').replace('\\', '/')).name
        if Path(filename).suffix.lower() not in ('.mp4', '.webm'):
            raise HTTPException(400, 'Поддерживаются WebM и MP4')
        title = (title.strip() or Path(filename).stem)[:300]
        path = incoming.directory('staging') / (uuid.uuid4().hex + '__' + filename)
        try:
            await save_upload(file, path, incoming.MAX_VIDEO)
            mid = await run_in_threadpool(incoming.accept, path, user['id'], project_id, title, meeting_at)
            return {'id': mid}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        finally:
            path.unlink(missing_ok=True)
            await file.close()

    @app.get('/api/projects/{project_id}/incoming')
    def folder_get(project_id: int, user=Depends(current_user)):
        owned('projects', project_id, user['id'])
        if cloud.enabled():
            return {'enabled':False,'path':'Импорт из локальной папки доступен в режиме Windows','supported':False}
        with db.connect() as conn:
            row = conn.execute('SELECT enabled FROM incoming_settings WHERE project_id=? AND owner_id=?', (project_id, user['id'])).fetchone()
        return {'enabled': bool(row and row[0]), 'path': str(db.DATA_DIR / 'incoming' / str(user['id']) / str(project_id))}

    @app.put('/api/projects/{project_id}/incoming')
    def folder_put(project_id: int, data: dict, user=Depends(current_user)):
        owned('projects', project_id, user['id'])
        if cloud.enabled():
            raise HTTPException(409,'Локальная папка доступна только в режиме Windows')
        folder = incoming.safe(incoming.directory('incoming') / str(user['id']) / str(project_id))
        folder.mkdir(parents=True, exist_ok=True)
        with db.connect() as conn:
            conn.execute('INSERT INTO incoming_settings(project_id,owner_id,enabled) VALUES(?,?,?) ON CONFLICT(project_id) DO UPDATE SET enabled=excluded.enabled', (project_id, user['id'], int(bool(data.get('enabled')))))
        return folder_get(project_id, user)

    @app.get('/api/incoming')
    def listing(user=Depends(current_user)):
        with db.connect() as conn:
            rows = conn.execute("SELECT * FROM meetings WHERE owner_id=? AND source_type='video' ORDER BY id DESC", (user['id'],)).fetchall()
            errors = [dict(x) for x in conn.execute('SELECT * FROM incoming_errors WHERE owner_id=? ORDER BY id DESC LIMIT 100', (user['id'],))]
        return {'items': [public_meeting(r) for r in rows], 'errors': errors}

    @app.get('/api/analytics')
    def analytics(project_id: int | None = None, start: str = '', end: str = '', page: int = 1, user=Depends(current_user)):
        where, args = 'owner_id=?', [user['id']]
        if project_id is not None:
            owned('projects', project_id, user['id'])
            where += ' AND project_id=?'
            args.append(project_id)
        try:
            if start:
                where += ' AND meeting_at>=?'
                args.append(date.fromisoformat(start).isoformat())
            if end:
                where += ' AND meeting_at<?'
                args.append((date.fromisoformat(end)+timedelta(days=1)).isoformat())
        except ValueError:
            raise HTTPException(400, 'Период должен содержать даты ГГГГ-ММ-ДД')
        if page < 1:
            raise HTTPException(400, 'Неверная страница')
        with db.connect() as conn:
            stats = dict(conn.execute("SELECT count(CASE WHEN results IS NOT NULL THEN 1 END) AS ready, count(CASE WHEN status IN ('uploaded','processing','Расшифровка','Анализ') THEN 1 END) AS processing, count(CASE WHEN status IN ('error','Ошибка') THEN 1 END) AS errors FROM meetings WHERE " + where, args).fetchone())
            rows = conn.execute('SELECT * FROM meetings WHERE '+where+' AND results IS NOT NULL ORDER BY meeting_at DESC,id DESC LIMIT 20 OFFSET ?', [*args, (page-1)*20]).fetchall()
            active = conn.execute("SELECT * FROM meetings WHERE "+where+" AND status IN ('uploaded','processing','error','Ошибка','Расшифровка','Анализ') ORDER BY id DESC", args).fetchall()
        return {'counts': stats, 'items': [public_meeting(r) for r in rows], 'active': [public_meeting(r) for r in active], 'page': page, 'pages': max(1, (stats['ready']+19)//20)}

    @app.patch('/api/meetings/{meeting_id}')
    def edit(meeting_id: int, data: dict, user=Depends(current_user)):
        owned('meetings', meeting_id, user['id'])
        title = str(data.get('title', '')).strip()
        when = str(data.get('meeting_at', ''))
        try:
            datetime.fromisoformat(when)
        except ValueError:
            raise HTTPException(400, 'Укажите дату встречи')
        if not title or len(title)>300:
            raise HTTPException(400, 'Укажите название до 300 символов')
        with incoming.LOCK, db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT status FROM meetings WHERE id=? AND owner_id=?', (meeting_id, user['id'])).fetchone()
            if not row:
                raise HTTPException(404, 'Не найдено')
            if row[0] == 'processing':
                raise HTTPException(409, 'Во время обработки нельзя менять название и дату')
            conn.execute('UPDATE meetings SET title=?,meeting_at=?,auto_date=0 WHERE id=?', (title, when, meeting_id))
        return {'ok': True}

    @app.get('/api/meetings/{meeting_id}/video')
    def video(meeting_id: int, download: bool = False, user=Depends(current_user)):
        row = owned('meetings', meeting_id, user['id'])
        if not row['video_name']:
            raise HTTPException(404, 'Видео не найдено')
        if cloud.enabled():
            return RedirectResponse(cloud.signed('video/'+row['video_name']),status_code=307)
        path = incoming.safe(incoming.directory('video') / row['video_name'])
        if not path.is_file():
            raise HTTPException(404, 'Оригинал отсутствует')
        return FileResponse(path, media_type='video/'+row['video_format'], filename='meeting.'+row['video_format'], content_disposition_type='attachment' if download else 'inline')

    @app.post('/api/meetings/{meeting_id}/retry')
    def retry(meeting_id: int, user=Depends(current_user)):
        row = owned('meetings', meeting_id, user['id'])
        if row['source_type'] != 'video' and not (cloud.enabled() and row['source_type']=='audio'):
            raise HTTPException(400, 'Повтор предназначен для видео')
        with incoming.LOCK, db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            job = conn.execute('SELECT state FROM jobs WHERE meeting_id=? AND owner_id=?', (meeting_id, user['id'])).fetchone()
            if not job or job[0] != 'error':
                raise HTTPException(409, 'Повтор доступен только после ошибки')
            conn.execute("UPDATE jobs SET state='uploaded',interruptions=0,token=NULL,lease=NULL WHERE meeting_id=?", (meeting_id,))
            conn.execute("UPDATE meetings SET status='uploaded',stage=NULL,error=NULL WHERE id=?", (meeting_id,))
        if cloud.enabled():
            try:
                cloud.enqueue(meeting_id)
            except Exception:
                with db.connect() as conn:
                    conn.execute("UPDATE jobs SET state='error' WHERE meeting_id=? AND state='uploaded'",(meeting_id,))
                    conn.execute("UPDATE meetings SET status='error',error='Не удалось запустить очередь. Проверьте QStash.' WHERE id=? AND status='uploaded'",(meeting_id,))
                raise HTTPException(503,'Не удалось запустить очередь. Проверьте QStash.')
        return {'ok': True}
