"""Хранилище SQLite. Одно соединение на операцию — безопасно для потоков FastAPI."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    language TEXT NOT NULL,
    transcript TEXT NOT NULL,
    audio_url TEXT,
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checklists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL,
    data TEXT NOT NULL,
    comment TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    checklist_id INTEGER NOT NULL REFERENCES checklists(id),
    status TEXT NOT NULL,
    results TEXT,
    score TEXT,
    sections TEXT,
    critical INTEGER,
    issues TEXT,
    model TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_usd REAL,
    error TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_eval_conv ON evaluations(conversation_id);
CREATE TABLE IF NOT EXISTS appeals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id INTEGER NOT NULL REFERENCES evaluations(id),
    criterion_id TEXT NOT NULL,
    comment TEXT NOT NULL,
    status TEXT NOT NULL,
    decision TEXT,
    new_answer TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE(evaluation_id, criterion_id)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path() -> Path:
    return Path(os.getenv("DATABASE_PATH", "data/cqa.sqlite"))


@contextmanager
def connect():
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(default_checklist: dict) -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Миграция: модель, выбранная пользователем для оценки
        cols = {r[1] for r in conn.execute("PRAGMA table_info(evaluations)")}
        if "requested_model" not in cols:
            conn.execute("ALTER TABLE evaluations ADD COLUMN requested_model TEXT")
        # Миграция: анализ речи по записи звонка (интонация, дикция, ошибки распознавания)
        if "speech" not in cols:
            conn.execute("ALTER TABLE evaluations ADD COLUMN speech TEXT")
        # Задачи, прерванные перезапуском сервера, не должны висеть вечно.
        conn.execute(
            "UPDATE evaluations SET status='failed', error='Сервер был перезапущен во время оценки', finished_at=? "
            "WHERE status IN ('queued','running')",
            (now(),),
        )
        if conn.execute("SELECT COUNT(*) FROM checklists").fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO checklists(version, data, comment, created_at) VALUES (1, ?, ?, ?)",
                (json.dumps(default_checklist, ensure_ascii=False), "Начальная версия из PDF", now()),
            )


def loads(value, default=None):
    return json.loads(value) if value else default


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)
