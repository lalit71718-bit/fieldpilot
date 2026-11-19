"""SQLite store that stands in for the company's enterprise systems
(ticketing/CMMS, inventory/ERP, notifications). Reset it any time with reset_db()."""
import json
import sqlite3
import threading
from datetime import datetime, timedelta

from . import config

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE technicians (id TEXT PRIMARY KEY, name TEXT, region TEXT, base_warehouse TEXT,
    supervisor_name TEXT, supervisor_channel TEXT, preferences TEXT);
CREATE TABLE assets (id TEXT PRIMARY KEY, name TEXT, model TEXT, site TEXT, installed_on TEXT,
    warranty_until TEXT);
CREATE TABLE asset_history (id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id TEXT, date TEXT,
    technician TEXT, summary TEXT);
CREATE TABLE tickets (id TEXT PRIMARY KEY, asset_id TEXT, technician_id TEXT, title TEXT,
    description TEXT, priority TEXT, status TEXT, opened_at TEXT, sla_due TEXT, notes TEXT);
CREATE TABLE parts (sku TEXT PRIMARY KEY, name TEXT, compatible_models TEXT, unit_price INTEGER);
CREATE TABLE stock (sku TEXT, warehouse TEXT, qty INTEGER, eta_hours INTEGER,
    PRIMARY KEY (sku, warehouse));
CREATE TABLE orders (id TEXT PRIMARY KEY, ticket_id TEXT, sku TEXT, qty INTEGER, warehouse TEXT,
    total INTEGER, status TEXT, eta TEXT, created_at TEXT);
CREATE TABLE pending_actions (id TEXT PRIMARY KEY, kind TEXT, payload TEXT, summary TEXT,
    status TEXT, created_at TEXT);
CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT, recipient TEXT,
    message TEXT, delivered INTEGER, created_at TEXT);
CREATE TABLE followups (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, when_at TEXT,
    purpose TEXT, created_at TEXT);
CREATE TABLE reports (id TEXT PRIMARY KEY, ticket_id TEXT, body TEXT, created_at TEXT);
CREATE TABLE activity (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, title TEXT,
    detail TEXT);
CREATE TABLE memory (key TEXT PRIMARY KEY, value TEXT);
"""


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.isoformat(sep=" ")


def _seed(c: sqlite3.Connection) -> None:
    t = now()
    c.executemany("INSERT INTO technicians VALUES (?,?,?,?,?,?,?)", [
        ("T-07", "Ravi", "Bengaluru East", "Whitefield", "Priya (Service Manager)", "#field-ops",
         json.dumps({"language": "English", "style": "short step-by-step instructions",
                     "units": "metric"})),
    ])
    c.executemany("INSERT INTO assets VALUES (?,?,?,?,?,?)", [
        ("A-CH-03", "Chiller 3", "CoolMax CX-300", "Prestige Tech Park, Tower B, Whitefield",
         "2021-03-14", "2026-03-13"),
        ("A-DG-01", "DG Set 1", "PowerGen PG-50", "Embassy Tech Square, Block C, Marathahalli",
         "2022-08-02", "2027-08-01"),
    ])
    c.executemany("INSERT INTO asset_history (asset_id,date,technician,summary) VALUES (?,?,?,?)", [
        ("A-CH-03", iso(t - timedelta(days=210))[:10], "Ravi",
         "Quarterly service. Condenser coil cleaned, refrigerant pressure normal."),
        ("A-CH-03", iso(t - timedelta(days=64))[:10], "Suresh",
         "Error E4 twice in one week. Condenser fan run capacitor measured 38 uF (rated 45). "
         "Advised replacement, part not available on van."),
        ("A-CH-03", iso(t - timedelta(days=20))[:10], "Ravi",
         "Customer reported warm air in afternoon. Reset unit, E4 cleared after reset."),
        ("A-DG-01", iso(t - timedelta(days=30))[:10], "Ravi",
         "250-hour service. Oil and fuel filter replaced."),
    ])
    c.executemany("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("TK-1042", "A-CH-03", "T-07", "Chiller 3 tripping with error E4",
         "Server room cooling unit trips every afternoon with E4 (high discharge pressure). "
         "Customer escalated: server room temperature reached 29 C.",
         "high", "open", iso(t - timedelta(hours=3)), iso(t + timedelta(hours=5)), ""),
        ("TK-1043", "A-DG-01", "T-07", "DG set low coolant warning",
         "Low coolant warning shown during weekly test run.",
         "medium", "open", iso(t - timedelta(hours=26)), iso(t + timedelta(hours=22)), ""),
    ])
    c.executemany("INSERT INTO parts VALUES (?,?,?,?)", [
        ("CAP-45", "Condenser fan run capacitor 45/5 uF", "CoolMax CX-300", 1200),
        ("FAN-CX", "Condenser fan motor 1/2 HP", "CoolMax CX-300", 8500),
        ("PSW-HP", "High pressure switch", "CoolMax CX-300", 2300),
        ("CLT-5L", "Coolant concentrate 5 L", "PowerGen PG-50", 1800),
    ])
    c.executemany("INSERT INTO stock VALUES (?,?,?,?)", [
        ("CAP-45", "Whitefield", 0, 4), ("CAP-45", "HSR Layout", 2, 18), ("CAP-45", "Peenya", 6, 30),
        ("FAN-CX", "Whitefield", 1, 4), ("FAN-CX", "Peenya", 2, 30),
        ("PSW-HP", "Whitefield", 3, 4),
        ("CLT-5L", "Whitefield", 10, 4),
    ])
    c.execute("INSERT INTO memory VALUES ('active_ticket', '')")


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = connect()
        exists = _conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tickets'").fetchone()
        if not exists:
            reset_db()
    return _conn


def reset_db() -> None:
    c = _conn if _conn is not None else conn()
    with _lock:
        tables = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for name in tables:
            c.execute(f"DROP TABLE {name}")
        c.executescript(SCHEMA)
        _seed(c)
        c.commit()


def q(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql: str, args: tuple = ()) -> dict | None:
    rows = q(sql, args)
    return rows[0] if rows else None


def run(sql: str, args: tuple = ()) -> None:
    with _lock:
        c = conn()
        c.execute(sql, args)
        c.commit()


def log(kind: str, title: str, detail: str = "") -> None:
    run("INSERT INTO activity (ts,kind,title,detail) VALUES (?,?,?,?)",
        (iso(now()), kind, title, detail))


def remember(key: str, value: str) -> None:
    run("INSERT INTO memory VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value))


def recall(key: str) -> str:
    row = one("SELECT value FROM memory WHERE key=?", (key,))
    return row["value"] if row else ""
