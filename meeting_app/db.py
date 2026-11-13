import os
import sqlite3
from pathlib import Path
from . import cloud

DATA_DIR = Path(os.getenv("MEETING_DATA_DIR", "/tmp/meeting-app" if cloud.enabled() else "data")).resolve()
DB_PATH = DATA_DIR / "meetings.sqlite3"
AUDIO_DIR = DATA_DIR / "audio"


def connect():
    if cloud.enabled():
        from .postgres import Connection
        return Connection(os.environ["DATABASE_URL"])
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    return db


def migrate():
    if cloud.enabled():
        from .postgres import migrate as migrate_postgres
        return migrate_postgres(os.environ["DATABASE_URL"])
    with connect() as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            db.executescript("""
            CREATE TABLE users(id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
              password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','user')),
              display_name TEXT NOT NULL DEFAULT '', aliases TEXT NOT NULL DEFAULT '[]',
              must_change_password INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              csrf TEXT NOT NULL, expires_at TEXT NOT NULL);
            CREATE TABLE projects(id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              name TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE meetings(id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
              title TEXT NOT NULL, meeting_at TEXT NOT NULL, source_type TEXT NOT NULL CHECK(source_type IN ('audio','text')),
              status TEXT NOT NULL DEFAULT 'Загружено', error TEXT, results TEXT,
              audio_name TEXT, audio_format TEXT, audio_duration REAL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE tasks(id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
              meeting_id INTEGER REFERENCES meetings(id) ON DELETE SET NULL,
              source_index INTEGER, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'К выполнению', assignee TEXT NOT NULL DEFAULT '',
              due_date TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(owner_id,meeting_id,source_index));
            CREATE INDEX ix_projects_owner ON projects(owner_id);
            CREATE INDEX ix_meetings_owner ON meetings(owner_id);
            CREATE INDEX ix_tasks_owner ON tasks(owner_id);
            PRAGMA user_version=1;
            """)
    # Rebuild only the CHECK-constrained parent; keep child references intact.
    with connect() as db:
        if db.execute("PRAGMA user_version").fetchone()[0] < 2:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("BEGIN IMMEDIATE")
            try:
                schema = db.execute("SELECT sql FROM sqlite_master WHERE name='meetings'").fetchone()[0]
                db.execute(schema.replace('CREATE TABLE meetings', 'CREATE TABLE meetings_v2', 1).replace("('audio','text')", "('audio','text','video')"))
                db.execute("INSERT INTO meetings_v2 SELECT * FROM meetings")
                db.execute("DROP TABLE meetings")
                db.execute("ALTER TABLE meetings_v2 RENAME TO meetings")
                for definition in (
                    "video_name TEXT", "original_name TEXT", "content_hash TEXT", "video_format TEXT",
                    "video_size INTEGER", "intake TEXT", "auto_date INTEGER NOT NULL DEFAULT 0",
                    "stage TEXT", "processed_at TEXT", "attempts INTEGER NOT NULL DEFAULT 0"):
                    db.execute('ALTER TABLE meetings ADD COLUMN ' + definition)
                db.execute("CREATE INDEX ix_meetings_owner ON meetings(owner_id)")
                db.execute("CREATE UNIQUE INDEX ix_video_hash ON meetings(owner_id,project_id,content_hash)")
                db.execute("ALTER TABLE tasks ADD COLUMN source_key TEXT")
                db.execute("CREATE UNIQUE INDEX ix_task_source ON tasks(owner_id,meeting_id,source_key)")
                db.execute("CREATE TABLE jobs(meeting_id INTEGER PRIMARY KEY REFERENCES meetings(id) ON DELETE CASCADE, owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, state TEXT NOT NULL DEFAULT 'uploaded', attempt INTEGER NOT NULL DEFAULT 0, interruptions INTEGER NOT NULL DEFAULT 0, token TEXT, lease REAL)")
                db.execute("CREATE TABLE incoming_settings(project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE, owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, enabled INTEGER NOT NULL DEFAULT 1)")
                db.execute("CREATE TABLE incoming_errors(id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE, filename TEXT, error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
                db.execute("PRAGMA user_version=2")
                if db.execute('PRAGMA foreign_key_check').fetchone():
                    raise RuntimeError('Migration foreign key check failed')
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.execute("PRAGMA foreign_keys=ON")
