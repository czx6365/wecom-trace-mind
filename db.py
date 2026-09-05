"""数据库层：BASE_DIR / DB_PATH / env 加载 / schema 统一入口。

app.py 与所有 scripts（独立进程）都从这里拿连接和建表，
保证新表（messages/events/demands/reports）在任意入口首次使用时都已存在。
"""

import os
import sqlite3
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
PUBLIC_DIR = BASE_DIR / "public"
DB_PATH = BASE_DIR / "feedback.db"
RUNTIME_PATH = BASE_DIR / ".runtime.json"


def load_env():
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_db():
    # WAL 模式下 HTTP 线程 / 分类线程 / 导入子进程并发写，靠 busy_timeout 排队
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def rows_to_dicts(rows):
    return [dict(row) for row in rows]


def get_setting(key, default=None):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )


def init_db():
    with get_db() as conn:
        # 库级持久设置，执行一次即可；后续连接只受影响
        conn.execute("PRAGMA journal_mode = WAL")

        # ---- 旧表（保持不变，仅保证存在）----
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feedbacks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source_group TEXT NOT NULL,
                customer_name TEXT,
                type TEXT NOT NULL,
                urgency TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                summary_id INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                feedback_count INTEGER NOT NULL,
                content TEXT NOT NULL,
                pushed_to_wechat INTEGER NOT NULL DEFAULT 0,
                error TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS monitored_chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                kind TEXT,
                enabled INTEGER NOT NULL DEFAULT 0,
                last_message_time INTEGER,
                last_message_id INTEGER,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS imported_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                message_key TEXT NOT NULL,
                feedback_id INTEGER NOT NULL,
                imported_at TEXT NOT NULL,
                UNIQUE(source, message_key)
            )
            """
        )

        # ---- 新表：消息级结构化管线 ----
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_group TEXT NOT NULL,
                message_key TEXT NOT NULL,
                server_id INTEGER,
                message_id INTEGER,
                sequence INTEGER,
                sender_id TEXT,
                sender_name TEXT,
                sender_key TEXT NOT NULL DEFAULT 'unknown',
                content_type INTEGER NOT NULL DEFAULT 0,
                send_time INTEGER,
                created_at TEXT NOT NULL,
                content TEXT NOT NULL,
                is_noise INTEGER NOT NULL DEFAULT 0,
                noise_rule TEXT,
                classified INTEGER NOT NULL DEFAULT 0,
                classify_error TEXT,
                risk_pushed TEXT,
                UNIQUE(source, message_key)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_time ON messages(created_at)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_pending ON messages(classified, is_noise, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_group_time ON messages(source_group, created_at)"
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                source_group TEXT NOT NULL,
                sender_key TEXT,
                key_field TEXT,
                key_field2 TEXT,
                value_text TEXT,
                payload TEXT NOT NULL DEFAULT '{}',
                demand_id INTEGER,
                risk_level TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                status_note TEXT,
                status_at TEXT,
                push_status TEXT NOT NULL DEFAULT 'not_needed',
                pushed_at TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type_time ON events(event_type, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_status ON events(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_msg ON events(message_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_demand ON events(demand_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_group_time ON events(source_group, created_at)"
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS demands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                direction TEXT,
                first_seen TEXT NOT NULL,
                is_interesting INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                note TEXT,
                created_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_type TEXT NOT NULL,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                stats_json TEXT NOT NULL DEFAULT '{}',
                generated_at TEXT NOT NULL,
                pushed_to_wechat INTEGER NOT NULL DEFAULT 0,
                push_error TEXT,
                UNIQUE(report_type, period_start)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        # 首次启动：用 .env 的 SUMMARY_INTERVAL_MINUTES 作为默认自动采集间隔
        seeded = conn.execute(
            "SELECT 1 FROM settings WHERE key = 'auto_interval_minutes'"
        ).fetchone()
        if seeded is None:
            default_interval = int(os.environ.get("SUMMARY_INTERVAL_MINUTES", "0") or "0")
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('auto_interval_minutes', ?)",
                (str(max(default_interval, 0)),),
            )
