"""Optional PostgreSQL adapter. SQLite remains the default for local Windows."""
import ast
import os
import re
import sqlite3
from pathlib import Path


class Row(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


class Cursor:
    def __init__(self, cursor, returning=False):
        self.cursor = cursor
        self.rowcount = cursor.rowcount
        self.lastrowid = None
        if returning:
            row = cursor.fetchone()
            self.lastrowid = row[0] if row else None

    def fetchone(self):
        row = self.cursor.fetchone()
        if row is None:
            return None
        return Row(zip([c.name for c in self.cursor.description], row))

    def fetchall(self):
        return list(self)

    def __iter__(self):
        while (row := self.fetchone()) is not None:
            yield row


def connection_options():
    options = {'connect_timeout': 10, 'prepare_threshold': None}
    password = os.getenv('MEETING_DATABASE_PASSWORD')
    if password:
        options['password'] = password
    return options


class Connection:
    def __init__(self, url):
        import psycopg
        self.conn = psycopg.connect(url, **connection_options())
        self.conn.execute('SET search_path TO meeting_app, public')

    def execute(self, sql, args=()):
        sql = sql.strip()
        if sql == 'BEGIN IMMEDIATE':
            return Cursor(self.conn.execute('SELECT pg_advisory_xact_lock(735103)'))
        sql = sql.replace("datetime('now')", "to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS')")
        # SQLite source timestamps are strings in UTC, intentionally matching existing sessions.
        sql = sql.replace('CURRENT_TIMESTAMP', "to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS')")
        ignore = sql.upper().startswith('INSERT OR IGNORE ')
        sql = sql.replace('INSERT OR IGNORE ', 'INSERT ')
        if ignore:
            sql += ' ON CONFLICT DO NOTHING'
        table = re.match(r'INSERT INTO (\w+)', sql, re.I)
        returning = bool(table and table[1] in {'users','projects','meetings','tasks','incoming_errors'})
        if returning:
            sql += ' RETURNING id'
        sql = sql.replace('%', '%%').replace('?', '%s')
        try:
            return Cursor(self.conn.execute(sql, args), returning)
        except Exception as exc:
            from psycopg.errors import UniqueViolation
            if isinstance(exc, UniqueViolation):
                raise sqlite3.IntegrityError('Unique constraint') from None
            raise

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        try:
            self.conn.rollback() if kind else self.conn.commit()
        finally:
            self.conn.close()


def migrate(url):
    import psycopg
    # Reuse the immutable v1 table definitions; PostgreSQL has its own versioned schema.
    tree = ast.parse(Path(__file__).with_name('db.py').read_text(encoding='utf-8'))
    base = next(n.args[0].value for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'executescript')
    base = base.replace('INTEGER PRIMARY KEY', 'BIGSERIAL PRIMARY KEY').replace(' COLLATE NOCASE','')
    base = base.replace("('audio','text')", "('audio','text','video')")
    base = base.replace('CURRENT_TIMESTAMP', "(to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS'))")
    with psycopg.connect(url, **connection_options()) as conn:
        conn.execute('SELECT pg_advisory_xact_lock(735102)')
        conn.execute('CREATE SCHEMA IF NOT EXISTS meeting_app')
        conn.execute('SET search_path TO meeting_app, public')
        conn.execute('CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY)')
        if conn.execute('SELECT version FROM schema_version').fetchone():
            return
        for statement in base.split(';'):
            if statement.strip() and not statement.strip().startswith('PRAGMA'):
                conn.execute(statement)
        for definition in ('video_name TEXT','original_name TEXT','content_hash TEXT','video_format TEXT','video_size BIGINT','intake TEXT','auto_date INTEGER NOT NULL DEFAULT 0','stage TEXT','processed_at TEXT','attempts INTEGER NOT NULL DEFAULT 0'):
            conn.execute('ALTER TABLE meetings ADD COLUMN '+definition)
        conn.execute('ALTER TABLE tasks ADD COLUMN source_key TEXT')
        conn.execute('CREATE UNIQUE INDEX ix_users_email_lower ON users(lower(email))')
        conn.execute('CREATE UNIQUE INDEX ix_video_hash ON meetings(owner_id,project_id,content_hash)')
        conn.execute('CREATE UNIQUE INDEX ix_task_source ON tasks(owner_id,meeting_id,source_key)')
        conn.execute("CREATE TABLE jobs(meeting_id BIGINT PRIMARY KEY REFERENCES meetings(id) ON DELETE CASCADE,owner_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,state TEXT NOT NULL DEFAULT 'uploaded',attempt INTEGER NOT NULL DEFAULT 0,interruptions INTEGER NOT NULL DEFAULT 0,token TEXT,lease DOUBLE PRECISION)")
        conn.execute('CREATE TABLE incoming_settings(project_id BIGINT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,owner_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,enabled INTEGER NOT NULL DEFAULT 1)')
        conn.execute("CREATE TABLE incoming_errors(id BIGSERIAL PRIMARY KEY,owner_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,filename TEXT,error TEXT,created_at TEXT DEFAULT (to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS')))")
        conn.execute('CREATE TABLE uploads(id TEXT PRIMARY KEY,owner_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,path TEXT NOT NULL,title TEXT NOT NULL,meeting_at TEXT NOT NULL,filename TEXT NOT NULL,created_at DOUBLE PRECISION NOT NULL,meeting_id BIGINT REFERENCES meetings(id) ON DELETE SET NULL)')
        # This schema is deliberately outside Supabase's exposed public schema.
        conn.execute('INSERT INTO schema_version VALUES(1)')
