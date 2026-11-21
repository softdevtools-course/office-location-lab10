"""Одноразовый перенос SQLite стенда в PostgreSQL после остановки стенда."""
import argparse
import json
from pathlib import Path
import sqlite3
from contextlib import closing
from pg_store import SCHEMA, publish, Connection, DSN

TABLES = {
    "sessions": ("session_id", "surname", "first_name", "group_name", "created_at", "expires_at",
                 "token_hash", "employees_loaded", "offices_loaded", "processed_json", "decision_json", "revision"),
    "events": ("event_id", "session_id", "occurred_at", "event_type", "employee_id", "detail_json"),
    "request_log": ("request_id", "session_id", "method", "path", "status_code", "duration_ms", "occurred_at"),
    "submissions": ("session_id", "result", "submitted_at"),
    "notifications": ("notification_id", "session_id", "result", "text", "delivery_status", "attempt_count",
                      "last_error", "claimed_at", "created_at", "sent_at"),
}


def migrate(path):
    if not DSN:
        raise ValueError("OFFICE_PILOT_POSTGRES не настроен")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
        source.row_factory = sqlite3.Row
        source.execute("BEGIN")
        with Connection() as target:
            target.connection.execute(SCHEMA)
            target.execute("SELECT pg_advisory_xact_lock(hashtext('lab08-migration'))")
            if target.execute("SELECT EXISTS(SELECT 1 FROM lab08.sessions)").fetchone()["exists"]:
                raise ValueError("База ЛР №8 уже содержит данные. Импорт остановлен.")
            counts = {}
            for table, columns in TABLES.items():
                names = ", ".join(columns)
                rows = source.execute(f"SELECT {names} FROM {table}")
                statement = f"INSERT INTO lab08.{table} ({names}) VALUES ({', '.join('?' for _ in columns)})"
                count = 0
                for row in rows:
                    target.execute(statement, tuple(row[name] for name in columns))
                    count += 1
                counts[table] = count
            for table, column in (("events", "event_id"), ("notifications", "notification_id")):
                target.execute("SELECT setval(pg_get_serial_sequence(?, ?), COALESCE((SELECT MAX(" + column +
                               ") FROM lab08." + table + "), 1), EXISTS(SELECT 1 FROM lab08." + table + "))",
                               (f"lab08.{table}", column))
            for session in source.execute("""
                SELECT s.*, (SELECT MIN(e.occurred_at) FROM events e WHERE e.session_id=s.session_id
                  AND e.event_type IN ('employees_csv_read','offices_geojson_read','isochrone_requested','office_decision'))
                  AS started_at, r.result, r.submitted_at
                FROM sessions s LEFT JOIN submissions r ON r.session_id=s.session_id
            """):
                if session["started_at"]:
                    publish(target, session, "started", session["started_at"], imported=True)
                if session["result"] == "passed":
                    publish(target, session, "submitted", session["submitted_at"], imported=True)
            for table, count in counts.items():
                actual = target.execute(f"SELECT count(*) AS n FROM lab08.{table}").fetchone()["n"]
                if actual != count:
                    raise ValueError(f"Количество строк {table} не совпадает")
        return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sqlite", type=Path)
    print(json.dumps(migrate(parser.parse_args().sqlite), ensure_ascii=False))
