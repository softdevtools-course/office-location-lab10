"""Export local registration, results and request records for the teacher.

The CSV files remain in .local/ and contain no bearer token or token hash.
"""

import csv
import hashlib
import json
from pathlib import Path

from app import DB_PATH, connect


def record_id(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    output_dir = DB_PATH.parent
    with connect() as connection:
        sessions = connection.execute("SELECT * FROM sessions ORDER BY created_at, session_id").fetchall()
        events = connection.execute("SELECT * FROM events ORDER BY event_id").fetchall()
        requests = connection.execute("SELECT * FROM request_log ORDER BY occurred_at, request_id").fetchall()
        submissions = {
            row["session_id"]: row for row in connection.execute("SELECT * FROM submissions")
        }
        notifications = {
            row["session_id"]: row for row in connection.execute("SELECT * FROM notifications ORDER BY notification_id")
        }

    summary = [
        {
            "record_id": record_id(row["session_id"]),
            "surname": row["surname"],
            "first_name": row["first_name"],
            "group_name": row["group_name"],
            "registered_at": row["created_at"],
            "zone_count": len(json.loads(row["processed_json"])),
            "decision_accepted": bool(json.loads(row["decision_json"])["accepted"]) if row["decision_json"] else False,
            "submission_result": submissions[row["session_id"]]["result"] if row["session_id"] in submissions else "",
            "submitted_at": submissions[row["session_id"]]["submitted_at"] if row["session_id"] in submissions else "",
            "telegram_status": notifications[row["session_id"]]["delivery_status"] if row["session_id"] in notifications else "",
        }
        for row in sessions
    ]
    history = [
        {
            "record_id": record_id(row["session_id"]),
            "occurred_at": row["occurred_at"],
            "event_type": row["event_type"],
            "employee_id": row["employee_id"] or "",
            "detail_json": row["detail_json"],
        }
        for row in events
    ]
    request_history = [
        {
            "record_id": record_id(row["session_id"]) if row["session_id"] else "",
            "occurred_at": row["occurred_at"],
            "method": row["method"],
            "path": row["path"],
            "status_code": row["status_code"],
            "duration_ms": row["duration_ms"],
        }
        for row in requests
    ]
    write_csv(output_dir / "teacher-sessions.csv", list(summary[0]) if summary else [
        "record_id", "surname", "first_name", "group_name", "registered_at", "zone_count",
        "decision_accepted", "submission_result", "submitted_at", "telegram_status"
    ], summary)
    write_csv(output_dir / "teacher-events.csv", list(history[0]) if history else [
        "record_id", "occurred_at", "event_type", "employee_id", "detail_json"
    ], history)
    write_csv(output_dir / "teacher-requests.csv", list(request_history[0]) if request_history else [
        "record_id", "occurred_at", "method", "path", "status_code", "duration_ms"
    ], request_history)
    print(f"Exported {len(summary)} sessions, {len(history)} events and {len(request_history)} requests to {output_dir}")


if __name__ == "__main__":
    main()
