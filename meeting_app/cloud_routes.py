"""Cloud HTTP entry points. Job payloads contain identifiers only, never speech."""
import hmac
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from fastapi import Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from . import ai, cloud, db, incoming
from .security import current_user


def install(app, owned, inspect_audio):
    def require_cloud():
        if not cloud.enabled():
            raise HTTPException(404, 'Недоступно в локальном режиме')

    @app.post('/api/cloud/uploads')
    def authorize(data: dict, user=Depends(current_user)):
        require_cloud()
        project = int(data.get('project_id',0))
        owned('projects',project,user['id'])
        filename = Path(str(data.get('filename','')).replace('\\','/')).name
        ext = Path(filename).suffix.lower()
        if ext not in ('.mp4','.webm','.mp3','.m4a','.wav'):
            raise HTTPException(400,'Поддерживаются MP4, WebM, MP3, M4A и WAV')
        size = data.get('size')
        if not isinstance(size,int) or size <= 0 or size > cloud.upload_limit():
            raise HTTPException(400,f'Пустой файл или размер превышает {cloud.upload_limit() // (1024*1024)} МиБ')
        when = str(data.get('meeting_at',''))
        try:
            datetime.fromisoformat(when)
        except ValueError:
            raise HTTPException(400,'Укажите дату встречи')
        identifier = uuid.uuid4().hex
        path = f'uploads/{user["id"]}/{identifier}{ext}'
        with db.connect() as conn:
            conn.execute('INSERT INTO uploads(id,owner_id,project_id,path,title,meeting_at,filename,created_at) VALUES(?,?,?,?,?,?,?,?)',(identifier,user['id'],project,path,(str(data.get('title','')).strip() or Path(filename).stem)[:300],when,filename,time.time()))
        return {'id':identifier,'url':cloud.signed(path,upload=True)}

    @app.post('/api/cloud/uploads/{upload_id}/complete',status_code=202)
    async def complete(upload_id: str,user=Depends(current_user)):
        require_cloud()
        with db.connect() as conn:
            row = conn.execute('SELECT * FROM uploads WHERE id=? AND owner_id=?',(upload_id,user['id'])).fetchone()
        if not row:
            raise HTTPException(404,'Загрузка не найдена')
        row = dict(row)
        owned('projects',row['project_id'],user['id'])
        if row['meeting_id']:
            owned('meetings',row['meeting_id'],user['id'])
            return {'id':row['meeting_id']}
        if time.time()-row['created_at']>7200:
            raise HTTPException(410,'Срок загрузки истёк. Загрузите файл заново')
        path = incoming.directory('staging') / (uuid.uuid4().hex+Path(row['filename']).suffix.lower())
        def finish():
            cloud.download(row['path'],path)
            if path.suffix in ('.mp4','.webm'):
                mid = incoming.accept(path,user['id'],row['project_id'],row['title'],row['meeting_at'])
            else:
                if path.stat().st_size > 100*1024*1024:
                    raise ValueError('Аудио превышает 100 МиБ')
                kind,duration = inspect_audio(path,row['filename'])
                # Unique digest also makes completion retries idempotent for audio.
                import hashlib
                with path.open('rb') as content:
                    digest = hashlib.file_digest(content,'sha256').hexdigest()
                with db.connect() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    if not conn.execute('SELECT 1 FROM projects WHERE id=? AND owner_id=?',(row['project_id'],user['id'])).fetchone():
                        raise ValueError('Проект недоступен')
                    existing = conn.execute('SELECT id FROM meetings WHERE owner_id=? AND project_id=? AND content_hash=?',(user['id'],row['project_id'],digest)).fetchone()
                    if existing:
                        mid = existing[0]
                    else:
                        name = uuid.uuid4().hex+'.'+kind
                        cloud.put('audio/'+name,path)
                        cur = conn.execute("INSERT INTO meetings(owner_id,project_id,title,meeting_at,source_type,status,audio_name,audio_format,audio_duration,content_hash,intake) VALUES(?,?,?,?,'audio','uploaded',?,?,?,?,'upload')",(user['id'],row['project_id'],row['title'],row['meeting_at'],name,kind,duration,digest))
                        mid = cur.lastrowid
                        conn.execute('INSERT INTO jobs(meeting_id,owner_id) VALUES(?,?)',(mid,user['id']))
            with db.connect() as conn:
                conn.execute('UPDATE uploads SET meeting_id=? WHERE id=? AND owner_id=?',(mid,upload_id,user['id']))
            try:
                cloud.enqueue(mid)
            except Exception:
                # Persist error instead of leaving accepted jobs permanently waiting.
                with db.connect() as conn:
                    conn.execute("UPDATE jobs SET state='error' WHERE meeting_id=? AND state='uploaded'",(mid,))
                    conn.execute("UPDATE meetings SET status='error',error='Не удалось поставить задание в облачную очередь. Проверьте подключение очереди и повторите обработку.' WHERE id=? AND status='uploaded'",(mid,))
            try:
                cloud.delete(row['path'])
            except Exception:
                pass  # Expired staging objects are also covered by the storage lifecycle policy.
            return {'id':mid}
        try:
            return await run_in_threadpool(finish)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from None
        finally:
            path.unlink(missing_ok=True)

    @app.post('/api/cloud/worker')
    def worker(request: Request,data: dict):
        require_cloud()
        expected = os.getenv('MEETING_WORKER_SECRET','')
        if not expected or not hmac.compare_digest(request.headers.get('x-worker-token',''),expected):
            raise HTTPException(403,'Недопустимое задание')
        mid = data.get('meeting_id')
        if not isinstance(mid,int):
            raise HTTPException(400,'Неверный идентификатор')
        incoming.process_one(mid)
        with db.connect() as conn:
            job = conn.execute('SELECT state FROM jobs WHERE meeting_id=?',(mid,)).fetchone()
        if job and job[0] in ('uploaded','processing'):
            raise HTTPException(503,'Попытка ещё выполняется или ожидает восстановления')
        return {'ok':True}

    @app.post('/api/cloud/text')
    def text(data: dict,user=Depends(current_user)):
        require_cloud()
        source = str(data.get('transcript',''))
        if not source.strip() or len(source.encode())>2*1024*1024:
            raise HTTPException(400,'Введите текст до 2 МиБ')
        mid = data.get('meeting_id')
        if mid:
            m = owned('meetings',int(mid),user['id'])
            if m['source_type'] != 'text':
                raise HTTPException(400,'Это не текстовая встреча')
        else:
            project = int(data.get('project_id',0))
            owned('projects',project,user['id'])
            title = str(data.get('title','')).strip()
            when = str(data.get('meeting_at',''))
            try:
                datetime.fromisoformat(when)
            except ValueError:
                raise HTTPException(400,'Укажите дату встречи')
            if not title or len(title)>300:
                raise HTTPException(400,'Укажите название до 300 символов')
            with db.connect() as conn:
                cur = conn.execute("INSERT INTO meetings(owner_id,project_id,title,meeting_at,source_type,status) VALUES(?,?,?,?,'text','Анализ')",(user['id'],project,title,when))
                mid = cur.lastrowid
            m = owned('meetings',mid,user['id'])
        with db.connect() as conn:
            project = conn.execute('SELECT name FROM projects WHERE id=? AND owner_id=?',(m['project_id'],user['id'])).fetchone()[0]
        segments = [{'start':None,'end':None,'speaker':None,'text':source}]
        try:
            results = ai.Results.model_validate(ai.analyze(segments,m['title'],m['meeting_at'],project,user['display_name'],__import__('json').loads(user['aliases']))).model_dump()
            with db.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                if not conn.execute('SELECT 1 FROM meetings WHERE id=? AND owner_id=?',(mid,user['id'])).fetchone():
                    raise HTTPException(404,'Встреча удалена')
                incoming.sync_tasks(conn,m,results)
                conn.execute("UPDATE meetings SET status='Готово',results=?,error=NULL WHERE id=?",(__import__('json').dumps(results,ensure_ascii=False),mid))
        except HTTPException:
            raise
        except Exception:
            with db.connect() as conn:
                conn.execute("UPDATE meetings SET status='Ошибка',error='Не удалось получить итоги. Проверьте ключ, квоту и соединение; вставьте текст повторно.' WHERE id=? AND owner_id=?",(mid,user['id']))
        finally:
            source = None
            segments.clear()
        return {'id':mid}
