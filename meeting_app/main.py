import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from mutagen import File as AudioFile
from pydantic import BaseModel, Field

from . import ai, incoming
from .db import AUDIO_DIR, DATA_DIR, connect, migrate
from .security import current_user, hash_password, protect, verify_password

@asynccontextmanager
async def lifespan(_app):
    migrate()
    service = incoming.Service()
    service.start()
    try:
        yield
    finally:
        service.close()


app = FastAPI(title="Встречи и задачи", docs_url=None, redoc_url=None, lifespan=lifespan)
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")
pending = {}  # meeting_id -> transient transcript; never written to disk
pending_lock = threading.Lock()
MAX_AUDIO = 100 * 1024 * 1024
MAX_TEXT = 2 * 1024 * 1024


def rowdict(row):
    return dict(row) if row else None


def require_admin(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "Только для администратора")
    return user


def owned(table, item_id, user_id):
    if table not in ("projects", "meetings", "tasks"):
        raise ValueError("Invalid table")
    with connect() as db:
        row = db.execute(f"SELECT * FROM {table} WHERE id=? AND owner_id=?", (item_id, user_id)).fetchone()
    if not row:
        raise HTTPException(404, "Не найдено")
    return rowdict(row)


def public_meeting(row):
    d = rowdict(row)
    d.pop("audio_name", None)
    d.pop("video_name", None)
    d["results"] = json.loads(d["results"]) if d["results"] else None
    d["has_audio"] = bool(row["audio_name"])
    return d


class Login(BaseModel):
    email: str
    password: str


class NewUser(Login):
    display_name: str = ""


class Password(BaseModel):
    password: str


class Name(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class Profile(BaseModel):
    display_name: str = Field(max_length=200)
    aliases: list[str] = Field(max_length=20)
    password: str | None = None


class TaskEdit(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=4000)
    status: str = "К выполнению"
    assignee: str = Field(default="", max_length=200)
    due_date: str | None = None
    project_id: int
    meeting_id: int | None = None


def valid_status(status):
    if status not in ("К выполнению", "В работе", "Готово"):
        raise HTTPException(400, "Недопустимый статус")


def valid_date(value):
    if value:
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "Срок должен быть в формате ГГГГ-ММ-ДД")


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/bootstrap")
def bootstrap():
    with connect() as db:
        exists = db.execute("SELECT 1 FROM users LIMIT 1").fetchone()
    return {"needs_admin": not bool(exists)}


@app.post("/api/bootstrap")
def create_admin(data: NewUser):
    with connect() as db:
        if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            raise HTTPException(403, "Администратор уже создан")
        db.execute("INSERT INTO users(email,password_hash,role,display_name) VALUES(?,?,?,?)",
                   (data.email.strip().lower(), hash_password(data.password), "admin", data.display_name.strip()))
    return {"ok": True}


@app.post("/api/login")
def login(data: Login, response: Response, request: Request):
    with connect() as db:
        user = db.execute("SELECT * FROM users WHERE email=?", (data.email.strip().lower(),)).fetchone()
        if not user or not verify_password(data.password, user["password_hash"]):
            raise HTTPException(401, "Неверная почта или пароль")
        token, csrf = secrets.token_urlsafe(40), secrets.token_urlsafe(30)
        expires = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        db.execute("INSERT INTO sessions VALUES(?,?,?,?)",
                   (hashlib.sha256(token.encode()).hexdigest(), user["id"], csrf, expires))
    response.set_cookie("meeting_session", token, httponly=True, samesite="strict", secure=request.url.scheme == "https",
                        max_age=7*24*3600, path="/")
    return {"csrf": csrf}


@app.post("/api/logout")
def logout(request: Request, response: Response, user=Depends(current_user)):
    token = request.cookies.get("meeting_session", "")
    with connect() as db:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
    response.delete_cookie("meeting_session")
    return {"ok": True}


@app.get("/api/me")
def me(user=Depends(current_user)):
    return {"id": user["id"], "email": user["email"], "role": user["role"],
            "display_name": user["display_name"], "aliases": json.loads(user["aliases"]),
            "must_change_password": bool(user["must_change_password"]), "csrf": user["csrf"]}


@app.patch("/api/me")
def update_me(data: Profile, user=Depends(current_user)):
    aliases = [x.strip() for x in data.aliases if x.strip()]
    with connect() as db:
        db.execute("UPDATE users SET display_name=?, aliases=?, must_change_password=? WHERE id=?",
                   (data.display_name.strip(), json.dumps(aliases, ensure_ascii=False),
                    0 if data.password else user["must_change_password"], user["id"]))
        if data.password:
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(data.password), user["id"]))
    return {"ok": True}


@app.get("/api/admin/users")
def users(user=Depends(require_admin)):
    with connect() as db:
        return [dict(x) for x in db.execute("SELECT id,email,role,display_name,must_change_password FROM users ORDER BY id")]


@app.post("/api/admin/users")
def create_user(data: NewUser, user=Depends(require_admin)):
    try:
        with connect() as db:
            db.execute("INSERT INTO users(email,password_hash,role,display_name,must_change_password) VALUES(?,?,?,?,1)",
                       (data.email.strip().lower(), hash_password(data.password), "user", data.display_name.strip()))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Почта уже занята")
    return {"ok": True}


@app.put("/api/admin/users/{user_id}/password")
def reset_password(user_id: int, data: Password, user=Depends(require_admin)):
    with connect() as db:
        result = db.execute("UPDATE users SET password_hash=?,must_change_password=1 WHERE id=? AND role='user'",
                            (hash_password(data.password), user_id))
        if not result.rowcount:
            raise HTTPException(404, "Пользователь не найден")
        db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    return {"ok": True}


@app.delete("/api/admin/users/{user_id}")
@incoming.serialized
def delete_user(user_id: int, user=Depends(require_admin)):
    with connect() as db:
        target = db.execute("SELECT id FROM users WHERE id=? AND role='user'", (user_id,)).fetchone()
        if not target:
            raise HTTPException(404, "Пользователь не найден")
        names = [x[0] for x in db.execute("SELECT audio_name FROM meetings WHERE owner_id=? AND audio_name IS NOT NULL", (user_id,))]
        videos = [r[0] for r in db.execute("SELECT video_name FROM meetings WHERE owner_id=? AND video_name IS NOT NULL", (user_id,))]
        db.execute("DELETE FROM users WHERE id=?", (user_id,))
    for name in videos:
        incoming.safe(DATA_DIR / "video" / name).unlink(missing_ok=True)
    for name in names:
        (AUDIO_DIR / name).unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/admin/settings")
def settings(user=Depends(require_admin)):
    return {"key_present": ai.key_present(), "transcription_model": ai.TRANSCRIPTION_MODEL,
            "analysis_model": ai.ANALYSIS_MODEL}


@app.put("/api/admin/key")
def set_key(data: dict, user=Depends(require_admin)):
    key = str(data.get("key", "")).strip()
    if not key.startswith("sk-") or len(key) < 20:
        raise HTTPException(400, "Укажите действительный ключ OpenAI")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = ai.KEY_FILE.with_suffix(".tmp")
    temporary.write_bytes(protect(key))
    temporary.replace(ai.KEY_FILE)
    return {"ok": True}


@app.post("/api/admin/key/check")
def check_key(user=Depends(require_admin)):
    try:
        ai.client().models.list()
        return {"ok": True}
    except Exception as exc:
        from openai import AuthenticationError, PermissionDeniedError, APIConnectionError
        if isinstance(exc, AuthenticationError):
            raise HTTPException(400, "OpenAI отклонил ключ. Проверьте его и замените в настройках")
        if isinstance(exc, PermissionDeniedError):
            raise HTTPException(400, "У ключа нет доступа к OpenAI API")
        if isinstance(exc, APIConnectionError):
            raise HTTPException(503, "Нет соединения с OpenAI. Проверьте сеть, прокси и сертификаты Windows")
        raise HTTPException(400, "OpenAI не подтвердил ключ. Повторите проверку позже")


@app.get("/api/projects")
def projects(user=Depends(current_user)):
    with connect() as db:
        return [dict(x) for x in db.execute("SELECT * FROM projects WHERE owner_id=? ORDER BY id DESC", (user["id"],))]


@app.post("/api/projects")
def create_project(data: Name, user=Depends(current_user)):
    with connect() as db:
        cur = db.execute("INSERT INTO projects(owner_id,name) VALUES(?,?)", (user["id"], data.name.strip()))
        return {"id": cur.lastrowid}


@app.patch("/api/projects/{project_id}")
def rename_project(project_id: int, data: Name, user=Depends(current_user)):
    owned("projects", project_id, user["id"])
    with connect() as db:
        db.execute("UPDATE projects SET name=? WHERE id=?", (data.name.strip(), project_id))
    return {"ok": True}


@app.delete("/api/projects/{project_id}")
@incoming.serialized
def delete_project(project_id: int, user=Depends(current_user)):
    owned("projects", project_id, user["id"])
    with connect() as db:
        names = [r[0] for r in db.execute("SELECT audio_name FROM meetings WHERE project_id=? AND audio_name IS NOT NULL", (project_id,))]
        videos = [r[0] for r in db.execute("SELECT video_name FROM meetings WHERE project_id=? AND video_name IS NOT NULL", (project_id,))]
        db.execute("DELETE FROM projects WHERE id=?", (project_id,))
    for name in videos:
        incoming.safe(DATA_DIR / "video" / name).unlink(missing_ok=True)
    for name in names:
        (AUDIO_DIR / name).unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/projects/{project_id}/meetings")
def meetings(project_id: int, user=Depends(current_user)):
    owned("projects", project_id, user["id"])
    with connect() as db:
        return [public_meeting(x) for x in db.execute("SELECT * FROM meetings WHERE project_id=? AND owner_id=? ORDER BY meeting_at DESC",
                                                   (project_id, user["id"]))]


def inspect_audio(path: Path, filename: str):
    ext = Path(filename).suffix.lower()
    if ext not in (".mp3", ".m4a", ".wav"):
        raise HTTPException(400, "Поддерживаются только MP3, M4A и WAV")
    head = path.open("rb").read(16)
    valid = (ext == ".wav" and head[:4] == b"RIFF" and head[8:12] == b"WAVE") or (
        ext == ".m4a" and head[4:8] == b"ftyp") or (
        ext == ".mp3" and (head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 255 and head[1] & 0xE0 == 0xE0)))
    if not valid:
        raise HTTPException(400, "Содержимое файла не соответствует формату")
    try:
        meta = AudioFile(path)
        duration = float(meta.info.length)
    except Exception:
        raise HTTPException(400, "Не удалось прочитать аудиофайл")
    if duration <= 0 or duration > 300:
        raise HTTPException(400, "Длительность записи должна быть не более 5 минут")
    return ext[1:], duration


async def save_upload(upload, path, limit):
    size = 0
    with path.open("wb") as out:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, "Файл слишком большой")
            out.write(chunk)
    if not size:
        raise HTTPException(400, "Файл пуст")


@app.post("/api/meetings")
async def create_meeting(project_id: int = Form(...), title: str = Form(...), meeting_at: str = Form(...),
                         transcript: str = Form(""), file: UploadFile | None = File(None),
                         user=Depends(current_user)):
    owned("projects", project_id, user["id"])
    if not title.strip() or len(title) > 300:
        raise HTTPException(400, "Укажите название встречи")
    try:
        datetime.fromisoformat(meeting_at)
    except ValueError:
        raise HTTPException(400, "Укажите дату встречи")
    if file and transcript.strip():
        raise HTTPException(400, "Выберите один источник: файл или текст")
    if not file and not transcript.strip():
        raise HTTPException(400, "Добавьте аудио, TXT или текст")
    audio_name = None
    audio_format = None
    duration = None
    source = "text"
    segments = None
    try:
        if file:
            if file.filename and Path(file.filename).suffix.lower() == ".txt":
                raw = await file.read(MAX_TEXT + 1)
                if len(raw) > MAX_TEXT:
                    raise HTTPException(413, "TXT слишком большой")
                try:
                    transcript = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    raise HTTPException(400, "TXT должен быть в UTF-8")
            else:
                source = "audio"
                suffix = Path(file.filename or "").suffix.lower()
                audio_name = uuid.uuid4().hex + suffix
                path = AUDIO_DIR / audio_name
                await save_upload(file, path, MAX_AUDIO)
                audio_format, duration = inspect_audio(path, file.filename or "")
        if source == "text":
            if len(transcript.encode("utf-8")) > MAX_TEXT or not transcript.strip():
                raise HTTPException(400, "Введите текст до 2 МБ")
            segments = [{"start": None, "end": None, "speaker": None, "text": transcript}]
        with connect() as db:
            cur = db.execute("INSERT INTO meetings(owner_id,project_id,title,meeting_at,source_type,audio_name,audio_format,audio_duration) "
                             "VALUES(?,?,?,?,?,?,?,?)", (user["id"], project_id, title.strip(), meeting_at,
                                                        source, audio_name, audio_format, duration))
            mid = cur.lastrowid
        if segments:
            with pending_lock:
                pending[mid] = (time.time(), segments)
        return {"id": mid}
    except Exception:
        if audio_name:
            (AUDIO_DIR / audio_name).unlink(missing_ok=True)
        raise


@app.get("/api/meetings/{meeting_id}")
def meeting(meeting_id: int, user=Depends(current_user)):
    row = owned("meetings", meeting_id, user["id"])
    result = public_meeting(row)
    with pending_lock:
        entry = pending.get(meeting_id)
    result["preview"] = entry[1] if entry and time.time() - entry[0] < 3600 else None
    return result


@app.post("/api/meetings/{meeting_id}/transcribe")
def start_transcription(meeting_id: int, user=Depends(current_user)):
    row = owned("meetings", meeting_id, user["id"])
    if row["source_type"] != "audio":
        raise HTTPException(400, "Для текстовой встречи расшифровка не требуется")
    if not ai.key_present():
        raise HTTPException(400, "Администратор должен настроить ключ OpenAI")
    with pending_lock:
        pending.pop(meeting_id, None)
    with connect() as db:
        db.execute("UPDATE meetings SET status='Расшифровка',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (meeting_id,))
    try:
        segments = ai.transcribe(AUDIO_DIR / row["audio_name"])
        with pending_lock:
            pending[meeting_id] = (time.time(), segments)
        return {"segments": segments}
    except Exception:
        with pending_lock:
            pending.pop(meeting_id, None)
        with connect() as db:
            db.execute("UPDATE meetings SET status='Ошибка',error='Не удалось расшифровать запись. Проверьте ключ, соединение и модель.',updated_at=CURRENT_TIMESTAMP WHERE id=?", (meeting_id,))
        raise HTTPException(502, "Не удалось расшифровать запись. Проверьте ключ, соединение и модель")


@app.post("/api/meetings/{meeting_id}/analyze")
def analyze_meeting(meeting_id: int, user=Depends(current_user)):
    row = owned("meetings", meeting_id, user["id"])
    if not ai.key_present():
        raise HTTPException(400, "Администратор должен настроить ключ OpenAI")
    with pending_lock:
        entry = pending.pop(meeting_id, None)
    if not entry or time.time() - entry[0] > 3600:
        raise HTTPException(400, "Предварительная расшифровка истекла. Для аудио повторите расшифровку; для текста загрузите встречу заново")
    with connect() as db:
        db.execute("UPDATE meetings SET status='Анализ',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (meeting_id,))
        project = db.execute("SELECT name FROM projects WHERE id=?", (row["project_id"],)).fetchone()[0]
    try:
        results = ai.analyze(entry[1], row["title"], row["meeting_at"], project,
                             user["display_name"], json.loads(user["aliases"]))
        with connect() as db:
            db.execute("UPDATE meetings SET status='Готово',results=?,error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (json.dumps(results, ensure_ascii=False), meeting_id))
            incoming.sync_tasks(db, row, results)
        return {"ok": True}
    except Exception as exc:
        from openai import AuthenticationError, PermissionDeniedError, RateLimitError, BadRequestError, NotFoundError, APIConnectionError
        if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
            detail = "OpenAI отклонил ключ или доступ. Администратор должен проверить настройки"
        elif isinstance(exc, RateLimitError):
            detail = "OpenAI ограничил запросы или исчерпана квота. Повторите позже"
        elif isinstance(exc, NotFoundError):
            detail = "Модель анализа не найдена или недоступна для ключа"
        elif isinstance(exc, BadRequestError):
            detail = "OpenAI отклонил запрос анализа. Проверьте модель и формат ответа"
        elif isinstance(exc, APIConnectionError):
            detail = "Нет соединения с OpenAI. Проверьте сеть и сертификаты Windows"
        else:
            detail = "Не удалось получить структурированные итоги. Повторите анализ"
        with connect() as db:
            db.execute("UPDATE meetings SET status='Ошибка',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (detail, meeting_id))
        raise HTTPException(502, detail)


@app.post("/api/meetings/{meeting_id}/retry-text")
def retry_text(meeting_id: int, data: dict, user=Depends(current_user)):
    row = owned("meetings", meeting_id, user["id"])
    if row["source_type"] != "text":
        raise HTTPException(400, "Это не текстовая встреча")
    transcript = str(data.get("transcript", ""))
    if not transcript.strip() or len(transcript.encode("utf-8")) > MAX_TEXT:
        raise HTTPException(400, "Введите текст до 2 МБ")
    with pending_lock:
        pending[meeting_id] = (time.time(), [{"start": None, "end": None, "speaker": None, "text": transcript}])
    with connect() as db:
        db.execute("UPDATE meetings SET status='Загружено',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (meeting_id,))
    return {"ok": True}


@app.put("/api/meetings/{meeting_id}/results")
def edit_results(meeting_id: int, data: dict, user=Depends(current_user)):
    owned("meetings", meeting_id, user["id"])
    try:
        validated = ai.Results.model_validate(data).model_dump()
    except Exception:
        raise HTTPException(400, "Неверная структура итогов")
    with connect() as db:
        db.execute("UPDATE meetings SET results=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (json.dumps(validated, ensure_ascii=False), meeting_id))
    return {"ok": True}


@app.get("/api/meetings/{meeting_id}/audio")
def audio(meeting_id: int, user=Depends(current_user)):
    row = owned("meetings", meeting_id, user["id"])
    if not row["audio_name"]:
        raise HTTPException(404, "Аудио не найдено")
    path = AUDIO_DIR / row["audio_name"]
    if not path.is_file():
        raise HTTPException(404, "Аудиофайл отсутствует")
    return FileResponse(path, media_type={"mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav"}[row["audio_format"]],
                        filename="meeting." + row["audio_format"], content_disposition_type="inline")


@app.delete("/api/meetings/{meeting_id}")
@incoming.serialized
def delete_meeting(meeting_id: int, user=Depends(current_user)):
    row = owned("meetings", meeting_id, user["id"])
    with connect() as db:
        db.execute("DELETE FROM meetings WHERE id=?", (meeting_id,))
    with pending_lock:
        pending.pop(meeting_id, None)
    if row["video_name"]:
        incoming.safe(DATA_DIR / "video" / row["video_name"]).unlink(missing_ok=True)
    if row["audio_name"]:
        (AUDIO_DIR / row["audio_name"]).unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/tasks")
def tasks(status: str = "", project_id: int | None = None, user=Depends(current_user)):
    sql = "SELECT * FROM tasks WHERE owner_id=?"
    args = [user["id"]]
    if status:
        valid_status(status)
        sql += " AND status=?"
        args.append(status)
    if project_id:
        owned("projects", project_id, user["id"])
        sql += " AND project_id=?"
        args.append(project_id)
    with connect() as db:
        return [dict(x) for x in db.execute(sql + " ORDER BY id DESC", args)]


def check_task_refs(data, user_id):
    valid_status(data.status)
    valid_date(data.due_date)
    owned("projects", data.project_id, user_id)
    if data.meeting_id:
        m = owned("meetings", data.meeting_id, user_id)
        if m["project_id"] != data.project_id:
            raise HTTPException(400, "Встреча относится к другому проекту")


@app.post("/api/tasks")
def create_task(data: TaskEdit, user=Depends(current_user)):
    check_task_refs(data, user["id"])
    with connect() as db:
        cur = db.execute("INSERT INTO tasks(owner_id,project_id,meeting_id,title,description,status,assignee,due_date) VALUES(?,?,?,?,?,?,?,?)",
                         (user["id"], data.project_id, data.meeting_id, data.title.strip(), data.description,
                          data.status, data.assignee, data.due_date))
    return {"id": cur.lastrowid}


@app.put("/api/tasks/{task_id}")
def update_task(task_id: int, data: TaskEdit, user=Depends(current_user)):
    owned("tasks", task_id, user["id"])
    check_task_refs(data, user["id"])
    with connect() as db:
        db.execute("UPDATE tasks SET project_id=?,meeting_id=?,title=?,description=?,status=?,assignee=?,due_date=? WHERE id=?",
                   (data.project_id, data.meeting_id, data.title.strip(), data.description,
                    data.status, data.assignee, data.due_date, task_id))
    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int, user=Depends(current_user)):
    owned("tasks", task_id, user["id"])
    with connect() as db:
        db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    return {"ok": True}


from .incoming_routes import install
install(app, owned, public_meeting, save_upload)
