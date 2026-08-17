import sqlite3
import threading
import uuid
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, task_id TEXT, trigger_type TEXT, source TEXT,
  status TEXT, engine TEXT, engine_version TEXT,
  started_at TEXT, finished_at TEXT, report_path TEXT, error_msg TEXT,
  source_text TEXT);
CREATE TABLE IF NOT EXISTS alerts (
  alert_id TEXT PRIMARY KEY, run_id TEXT, source_group TEXT, severity TEXT,
  summary TEXT, detail TEXT, status TEXT, created_at TEXT, acked_at TEXT,
  ignore_until TEXT, reopen_at TEXT);
CREATE TABLE IF NOT EXISTS messages (
  msg_id TEXT PRIMARY KEY, group_name TEXT, sender TEXT, received_at TEXT,
  matched_entry_id TEXT, matched_rule TEXT, run_id TEXT);
CREATE TABLE IF NOT EXISTS audit_logs (
  log_id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, category TEXT,
  action TEXT, target TEXT, detail TEXT, run_id TEXT);
CREATE TABLE IF NOT EXISTS auth_exec (
  id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT, run_id TEXT,
  action_type TEXT, matched INTEGER, reject_reason TEXT, exec_result TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS reports_meta (
  report_id TEXT PRIMARY KEY, run_id TEXT, title TEXT, file_path TEXT,
  created_at TEXT, feedback_state TEXT);
CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, type TEXT,
  file_path TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS conn_state (
  key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
"""


def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # 单连接跨线程共享（dws 轮询/pipeline/FastAPI 线程池），必须串行化防 Row 状态错乱
        self._lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        try:
            self.conn.execute("ALTER TABLE runs ADD COLUMN source_text TEXT")
        except Exception:
            pass
        try:
            self.conn.execute("ALTER TABLE auth_exec ADD COLUMN payload TEXT")
        except Exception:
            pass
        try:
            self.conn.execute("ALTER TABLE messages ADD COLUMN source_text TEXT")
        except Exception:
            pass
        try:
            self.conn.execute("ALTER TABLE messages ADD COLUMN parsed_json TEXT")
        except Exception:
            pass
        try:
            self.conn.execute("ALTER TABLE messages ADD COLUMN msg_time TEXT")
        except Exception:
            pass
        for idx, sql in [
            ("idx_runs_started", "CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at)"),
            ("idx_alerts_created", "CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at)"),
            ("idx_msgs_received", "CREATE INDEX IF NOT EXISTS idx_msgs_received ON messages(received_at)"),
            ("idx_auth_ts", "CREATE INDEX IF NOT EXISTS idx_auth_ts ON auth_exec(ts)"),
            ("idx_audit_ts", "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(ts)"),
        ]:
            try:
                self.conn.execute(sql)
            except Exception:
                pass
        self.conn.commit()

    def insert(self, table, ignore=False, **kw):
        verb = "INSERT OR IGNORE" if ignore else "INSERT"
        cols = ", ".join(kw)
        ph = ", ".join("?" for _ in kw)
        with self._lock:
            self.conn.execute(f"{verb} INTO {table} ({cols}) VALUES ({ph})", list(kw.values()))
            self.conn.commit()

    def update(self, table, key, key_val, **kw):
        sets = ", ".join(f"{k} = ?" for k in kw)
        with self._lock:
            self.conn.execute(f"UPDATE {table} SET {sets} WHERE {key} = ?",
                              list(kw.values()) + [key_val])
            self.conn.commit()

    def exec(self, sql, *args):
        with self._lock:
            self.conn.execute(sql, args)
            self.conn.commit()

    def q(self, sql, *args):
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql, *args):
        with self._lock:
            return self.conn.execute(sql, args).fetchone()

    def audit(self, category, action, target="", detail="", run_id=None):
        self.insert("audit_logs", ts=now(), category=category, action=action,
                    target=target, detail=detail, run_id=run_id)

    def set_state(self, key, value):
        with self._lock:
            self.conn.execute("INSERT INTO conn_state (key, value, updated_at) VALUES (?,?,?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                              (key, value, now()))
            self.conn.commit()

    def get_state(self, key, default=None):
        r = self.one("SELECT value FROM conn_state WHERE key=?", key)
        return r["value"] if r else default

    def count_exec(self, entry_id):
        r = self.one("SELECT COUNT(*) c FROM auth_exec WHERE entry_id=? AND matched=1", entry_id)
        return r["c"] if r else 0
