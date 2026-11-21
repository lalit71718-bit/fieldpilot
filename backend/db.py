"""SQLite store that stands in for the family's connected services (calendar, tasks,
shopping list, appliance records, service booking, notifications). Reset with reset_db()."""
import json
import sqlite3
import threading
from datetime import datetime, timedelta

from . import config

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE members (id TEXT PRIMARY KEY, name TEXT, role TEXT, age INTEGER, notes TEXT);
CREATE TABLE household (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, member TEXT,
    start_at TEXT, minutes INTEGER, location TEXT, source TEXT);
CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, assignee TEXT, due_at TEXT,
    status TEXT, created_at TEXT, done_at TEXT);
CREATE TABLE shopping (id INTEGER PRIMARY KEY AUTOINCREMENT, item TEXT, note TEXT,
    status TEXT, created_at TEXT, bought_by TEXT, bought_at TEXT);
CREATE TABLE grocery_orders (id TEXT PRIMARY KEY, items TEXT, total INTEGER, slot_at TEXT,
    status TEXT, created_at TEXT);
CREATE TABLE appliances (id TEXT PRIMARY KEY, name TEXT, model TEXT, room TEXT,
    installed_on TEXT, warranty_until TEXT);
CREATE TABLE appliance_history (id INTEGER PRIMARY KEY AUTOINCREMENT, appliance_id TEXT,
    date TEXT, by_whom TEXT, summary TEXT);
CREATE TABLE slots (id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, rating REAL,
    fee INTEGER, slot_at TEXT, available INTEGER);
CREATE TABLE bookings (id TEXT PRIMARY KEY, appliance_id TEXT, provider TEXT, slot_at TEXT,
    fee INTEGER, status TEXT, created_at TEXT);
CREATE TABLE reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, member TEXT,
    remind_at TEXT, status TEXT, created_at TEXT, fired_at TEXT);
CREATE TABLE pending_actions (id TEXT PRIMARY KEY, kind TEXT, payload TEXT, summary TEXT,
    status TEXT, created_at TEXT);
CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT, recipient TEXT,
    message TEXT, delivered INTEGER, created_at TEXT);
CREATE TABLE activity (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, title TEXT,
    detail TEXT);
CREATE TABLE memory (key TEXT PRIMARY KEY, value TEXT);
"""


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.isoformat(sep=" ")


def _daytime(dt: datetime) -> datetime:
    """Move a time into family-friendly hours (9 AM to 6 PM), next day if needed."""
    dt = dt.replace(minute=dt.minute - dt.minute % 30, second=0, microsecond=0)
    if dt.hour < 9:
        return dt.replace(hour=9, minute=0)
    if dt.hour >= 18:
        return (dt + timedelta(days=1)).replace(hour=10, minute=0)
    return dt


def _seed(c: sqlite3.Connection) -> None:
    """A fictional family. Times are relative to now (kept in daytime), so the demo works at
    any hour: the best technician slot always clashes with Dadi's appointment, the next is free."""
    t = now()
    hour = t.replace(minute=0, second=0)
    rohan_call = _daytime(hour + timedelta(hours=1))
    doctor = _daytime(hour + timedelta(hours=3))
    if doctor <= rohan_call + timedelta(hours=1):
        doctor = rohan_call + timedelta(hours=2)
    free_slot = doctor + timedelta(hours=2, minutes=30)
    if free_slot.hour >= 19:
        free_slot = (free_slot + timedelta(days=1)).replace(hour=14, minute=0)
    tomorrow = (t + timedelta(days=1)).replace(hour=9, minute=0, second=0)
    if tomorrow.date() == doctor.date():
        tomorrow = tomorrow + timedelta(days=1)
    late = t.replace(hour=21, minute=0, second=0)
    if late <= t:
        late = late + timedelta(days=1)
    c.executemany("INSERT INTO members VALUES (?,?,?,?,?)", [
        ("anika", "Anika", "you (parent)", 38, "Works from home on weekdays"),
        ("rohan", "Rohan", "spouse", 40, "Drives past FreshMart on the way home"),
        ("kabir", "Kabir", "son", 10, "Football practice on Saturdays"),
        ("dadi", "Dadi", "grandmother", 72, "Blood pressure tablet at 8 PM daily"),
    ])
    c.executemany("INSERT INTO household VALUES (?,?)", [
        ("diet", "vegetarian household"),
        ("grocery_store", "FreshMart, Indiranagar"),
        ("repair_budget", "under 1,500 rupees per visit unless approved"),
        ("monthly_budget", "8000 rupees a month for food, groceries and repairs"),
        ("quiet_hours", "no service visits after 8 PM"),
        ("family_channel", "#family"),
    ])
    c.executemany("INSERT INTO events (title,member,start_at,minutes,location,source) "
                  "VALUES (?,?,?,?,?,?)", [
        ("Dadi's doctor appointment (BP check-up)", "dadi", iso(doctor),
         90, "Sunrise Clinic", "family calendar"),
        ("Kabir football practice", "kabir", iso(tomorrow), 120, "HAL ground", "family calendar"),
        ("Rohan: team call", "rohan", iso(rohan_call), 60, "home office",
         "family calendar"),
    ])
    c.executemany("INSERT INTO tasks (title,assignee,due_at,status,created_at) "
                  "VALUES (?,?,?,?,?)", [
        ("Take out the recycling", "kabir", iso(t + timedelta(hours=3)), "open", iso(t)),
        ("Pay electricity bill", "rohan", iso(t + timedelta(days=2)), "open", iso(t)),
    ])
    c.executemany("INSERT INTO shopping (item,note,status,created_at) VALUES (?,?,?,?)", [
        ("Bananas", "", "needed", iso(t)),
    ])
    c.executemany("INSERT INTO appliances VALUES (?,?,?,?,?,?)", [
        ("AC-LIVING", "Living room AC", "CoolMax CS-18 split AC", "living room",
         "2022-04-10", "2027-04-09"),
        ("WM-01", "Washing machine", "AquaWash 7 kg front load", "utility",
         "2023-01-15", "2026-01-14"),
    ])
    c.executemany("INSERT INTO appliance_history (appliance_id,date,by_whom,summary) "
                  "VALUES (?,?,?,?)", [
        ("AC-LIVING", iso(t - timedelta(days=200))[:10], "CoolCare technician",
         "Annual service. Filters and coils cleaned. All normal."),
        ("AC-LIVING", iso(t - timedelta(days=64))[:10], "CoolCare technician",
         "Error E4 and warm air. Outdoor fan capacitor measured weak. Advised replacement, "
         "family postponed it."),
        ("AC-LIVING", iso(t - timedelta(days=18))[:10], "Rohan",
         "E4 again in the afternoon. Switched off and on at the breaker, it cleared."),
        ("WM-01", iso(t - timedelta(days=90))[:10], "Anika", "Drain filter cleaned."),
    ])
    c.executemany("INSERT INTO slots (provider,rating,fee,slot_at,available) VALUES (?,?,?,?,?)", [
        ("CoolCare Services", 4.7, 499, iso(doctor), 1),            # clashes with the doctor
        ("CoolCare Services", 4.7, 499, iso(free_slot), 1),         # best free slot
        ("QuickFix Home", 4.3, 399, iso(tomorrow + timedelta(hours=3)), 1),
        ("QuickFix Home", 4.3, 399, iso(late), 1),                  # after 8 PM quiet hours
    ])
    c.execute("INSERT INTO memory VALUES ('active_appliance', '')")


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
            "SELECT name FROM sqlite_master WHERE type='table' AND name='members'").fetchone()
        cols = {r[1] for r in _conn.execute("PRAGMA table_info(shopping)").fetchall()}
        if not exists or "bought_by" not in cols:
            reset_db()  # older database layout: start fresh with the current demo data
        rcols = {r[1] for r in _conn.execute("PRAGMA table_info(reminders)").fetchall()}
        if "fired_at" not in rcols:
            _conn.execute("ALTER TABLE reminders ADD COLUMN fired_at TEXT")
            _conn.commit()
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
