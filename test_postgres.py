"""Проверка миграции и транзакционной записи на тестовой базе."""
import os
import sqlite3
import tempfile
from pathlib import Path

os.environ["OFFICE_PILOT_DATABASE"] = str(Path(tempfile.mkdtemp()) / "source.sqlite3")
import app
import pg_store
from migrate_sqlite import migrate

assert pg_store.DSN
app.pg_store.DSN = ""
app.initialize_database()
with app.connect() as db:
    db.execute("""INSERT INTO sessions(session_id,surname,first_name,group_name,created_at,expires_at)
                  VALUES (?,?,?,?,?,?)""", ("test", "Примерова", "Мария", "23-ИС-11с",
                                           "2026-11-01T10:00:00+00:00", "2030-01-01T00:00:00+00:00"))
    db.execute("""INSERT INTO events(session_id,occurred_at,event_type) VALUES (?,?,?)""",
               ("test", "2026-11-01T11:00:00+00:00", "employees_csv_read"))
    db.execute("INSERT INTO submissions VALUES (?,?,?)", ("test", "passed", "2026-11-01T12:00:00+00:00"))
app.pg_store.DSN = os.environ["OFFICE_PILOT_POSTGRES"]
counts = migrate(Path(os.environ["OFFICE_PILOT_DATABASE"]))
assert counts["sessions"] == counts["submissions"] == 1
with pg_store.Connection() as db:
    events = db.execute("SELECT kind,imported FROM course_events.events ORDER BY id").fetchall()
    assert [(x["kind"], x["imported"]) for x in events] == [("started", True), ("submitted", True)]
    row = db.execute("SELECT session_id, surname, first_name, group_name FROM sessions").fetchone()
    assert row["session_id"] == "test"
try:
    migrate(Path(os.environ["OFFICE_PILOT_DATABASE"]))
    raise AssertionError("Duplicate import was not rejected")
except ValueError:
    pass
print("SQLite → PostgreSQL: записи, события и повторная миграция проверены.")
