"""Local API and map workbench for the office-location lab pilot.

Run: uvicorn app:app --host 127.0.0.1 --port 8011
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from functools import reduce
from pathlib import Path
from typing import Annotated
from urllib import error as urlerror
from urllib import request as urlrequest
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints
from shapely.geometry import mapping, shape


ROOT = Path(__file__).parent
DB_PATH = Path(os.environ.get("OFFICE_PILOT_DATABASE", ROOT / ".local" / "office-pilot.sqlite3"))
SESSION_SECONDS = 24 * 60 * 60
TELEGRAM_TOKEN = os.environ.get("DELIVERY_LAB_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("DELIVERY_LAB_TELEGRAM_CHAT_ID", "")
bearer_scheme = HTTPBearer(auto_error=False, scheme_name="BearerAuth")

EMPLOYEES = {
    row["id"]: row
    for row in csv.DictReader((ROOT / "employees.csv").open(encoding="utf-8", newline=""))
}
EMPLOYEE_POINTS = json.loads((ROOT / "employees.geojson").read_text(encoding="utf-8"))["features"]
OFFICES = json.loads((ROOT / "offices.geojson").read_text(encoding="utf-8"))["features"]
PUBLIC_OFFICES = [
    {**feature, "properties": {"id": feature["properties"]["id"], "address": feature["properties"]["address"]}}
    for feature in OFFICES
]
ZONES = {
    feature["properties"]["employee_id"]: feature
    for feature in json.loads((ROOT / "zones.geojson").read_text(encoding="utf-8"))["features"]
}
MATRIX = list(csv.DictReader((ROOT / "teacher-matrix.csv").open(encoding="utf-8", newline="")))
VALID_OFFICES = {
    office["properties"]["id"]
    for office in OFFICES
    if all(float(row[office["properties"]["id"]]) <= 30 for row in MATRIX)
}
OFFICE_IDS = {office["properties"]["id"] for office in OFFICES}

app = FastAPI(
    title="Выбираем офис: CSV, JSON и GeoJSON",
    description="Зарегистрируйтесь, подключите Bearer-токен, получите исходные файлы, запросите восемь зон и отправьте результат.",
    version="0.2.0",
)
app.mount("/assets", StaticFiles(directory=ROOT / "assets"), name="assets")


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                surname TEXT NOT NULL,
                first_name TEXT NOT NULL,
                group_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                token_hash TEXT UNIQUE,
                employees_loaded INTEGER NOT NULL DEFAULT 0,
                offices_loaded INTEGER NOT NULL DEFAULT 0,
                processed_json TEXT NOT NULL DEFAULT '[]',
                decision_json TEXT,
                revision INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                occurred_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                employee_id TEXT,
                detail_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS events_session_idx ON events(session_id, event_id);
            CREATE TABLE IF NOT EXISTS request_log (
                request_id TEXT PRIMARY KEY,
                session_id TEXT REFERENCES sessions(session_id),
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                status_code INTEGER NOT NULL,
                duration_ms REAL NOT NULL,
                occurred_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS request_log_session_idx ON request_log(session_id, occurred_at);
            CREATE TABLE IF NOT EXISTS submissions (
                session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
                result TEXT NOT NULL CHECK(result IN ('passed', 'not_passed')),
                submitted_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notifications (
                notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                result TEXT NOT NULL,
                text TEXT NOT NULL,
                delivery_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(delivery_status IN ('pending', 'sent', 'failed')),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                claimed_at TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT
            );
        """)
        # Existing local pilot databases predate bearer tokens. Their cookie
        # sessions remain in the audit trail; they cannot authenticate.
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(sessions)")}
        if "token_hash" not in columns:
            connection.execute("ALTER TABLE sessions ADD COLUMN token_hash TEXT")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS sessions_token_hash_idx ON sessions(token_hash)")
        notification_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(notifications)")
        }
        if "claimed_at" not in notification_columns:
            connection.execute("ALTER TABLE notifications ADD COLUMN claimed_at TEXT")


initialize_database()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def state_from_row(row: sqlite3.Row) -> dict:
    state = dict(row)
    state["employees_loaded"] = bool(row["employees_loaded"])
    state["offices_loaded"] = bool(row["offices_loaded"])
    state["processed"] = json.loads(row["processed_json"])
    state["decision"] = json.loads(row["decision_json"]) if row["decision_json"] else None
    return state


def find_session(authorization: str | None) -> dict | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:].strip()
    if not token:
        return None
    with connect() as connection:
        row = connection.execute("SELECT * FROM sessions WHERE token_hash = ?", (token_digest(token),)).fetchone()
    if row is None or datetime.fromisoformat(row["expires_at"]) <= now_utc():
        return None
    return state_from_row(row)


def authenticated_session(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict:
    authorization = (
        f"{credentials.scheme} {credentials.credentials}" if credentials is not None else None
    )
    state = find_session(authorization)
    if state is None:
        raise HTTPException(
            status_code=401, detail="Подключите действующий Bearer-токен.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.session_id = state["session_id"]
    return state


def record(state: dict, event_type: str, employee_id: str | None = None, detail: dict | None = None) -> None:
    with connect() as connection:
        connection.execute(
            """UPDATE sessions SET employees_loaded = ?, offices_loaded = ?, processed_json = ?,
               decision_json = ?, revision = ? WHERE session_id = ?""",
            (
                int(state["employees_loaded"]), int(state["offices_loaded"]),
                json.dumps(state["processed"], ensure_ascii=False),
                json.dumps(state["decision"], ensure_ascii=False) if state["decision"] is not None else None,
                state["revision"], state["session_id"],
            ),
        )
        connection.execute(
            "INSERT INTO events(session_id, occurred_at, event_type, employee_id, detail_json) VALUES (?, ?, ?, ?, ?)",
            (state["session_id"], now_utc().isoformat(), event_type, employee_id, json.dumps(detail or {}, ensure_ascii=False)),
        )


def common_zone(processed: list[str]) -> dict | None:
    if not processed:
        return None
    geometry = reduce(
        lambda left, right: left.intersection(right),
        (shape(ZONES[employee_id]["geometry"]) for employee_id in processed),
    )
    return {
        "type": "Feature",
        "properties": {
            "name": "Общая зона проверенных сотрудников",
            "checked": len(processed), "total": len(EMPLOYEES), "minutes": 30,
        },
        "geometry": mapping(geometry),
    }


NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class RegistrationRequest(BaseModel):
    surname: NonBlank
    first_name: NonBlank
    group_name: NonBlank


class IsochroneRequest(BaseModel):
    latitude: float = Field(examples=[56.1458246])
    longitude: float = Field(examples=[47.2664731])
    mode: str = Field(examples=["walk"])
    minutes: int = Field(default=30, examples=[30])


class OfficeDecision(BaseModel):
    office_ids: list[str] = Field(description="Коды выбранных офисов из GET /offices.geojson")


@app.middleware("http")
async def audit_request(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    if request.url.path in {
        "/auth/register", "/me", "/employees.csv", "/offices.geojson",
        "/isochrones", "/decision", "/submit", "/reset",
    }:
        try:
            session_id = getattr(request.state, "session_id", None)
            if session_id is None:
                token_session = find_session(request.headers.get("authorization"))
                session_id = token_session["session_id"] if token_session else None
            with connect() as connection:
                connection.execute(
                    """INSERT INTO request_log
                       (request_id, session_id, method, path, status_code, duration_ms, occurred_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid4()), session_id,
                        request.method, request.url.path, response.status_code,
                        round((time.perf_counter() - started) * 1000, 1), now_utc().isoformat(),
                    ),
                )
        except sqlite3.Error:
            # Logging must not replace the student's API response.
            pass
    return response


@app.get("/", include_in_schema=False)
def workbench():
    return FileResponse(ROOT / "workbench.html", media_type="text/html; charset=utf-8")


@app.post("/auth/register", status_code=201, summary="0. Получить персональный токен")
def register(body: RegistrationRequest, request: Request):
    session_id = str(uuid4())
    token = secrets.token_urlsafe(32)
    created_at = now_utc()
    expires_at = created_at + timedelta(seconds=SESSION_SECONDS)
    with connect() as connection:
        connection.execute(
            """INSERT INTO sessions
               (session_id, surname, first_name, group_name, created_at, expires_at, token_hash, revision)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
            (
                session_id, body.surname, body.first_name, body.group_name,
                created_at.isoformat(), expires_at.isoformat(), token_digest(token),
            ),
        )
        connection.execute(
            "INSERT INTO events(session_id, occurred_at, event_type) VALUES (?, ?, 'registered')",
            (session_id, created_at.isoformat()),
        )
    request.state.session_id = session_id
    return JSONResponse(
        {"access_token": token, "token_type": "bearer", "expires_at": expires_at.isoformat()},
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/me", include_in_schema=False)
def me(state: dict = Depends(authenticated_session)):
    return {key: state[key] for key in ("surname", "first_name", "group_name", "created_at", "expires_at")}


@app.get("/employees.csv", summary="1. Получить сотрудников в CSV")
def get_employees(state: dict = Depends(authenticated_session)):
    if not state["employees_loaded"]:
        state["employees_loaded"] = True
        state["revision"] += 1
    record(state, "employees_csv_read")
    return FileResponse(ROOT / "employees.csv", media_type="text/csv; charset=utf-8")


@app.get("/offices.geojson", summary="2. Получить офисы в GeoJSON")
def get_offices(state: dict = Depends(authenticated_session)):
    if not state["offices_loaded"]:
        state["offices_loaded"] = True
        state["revision"] += 1
    record(state, "offices_geojson_read")
    return JSONResponse({"type": "FeatureCollection", "features": PUBLIC_OFFICES}, media_type="application/geo+json")


@app.post("/isochrones", summary="3. Построить зону сотрудника в GeoJSON")
def create_isochrone(body: IsochroneRequest, state: dict = Depends(authenticated_session)):
    previous_submission = submission_for(state["session_id"])
    if previous_submission and previous_submission["result"] == "passed":
        raise HTTPException(status_code=409, detail="Работа уже сдана.")
    if not state["employees_loaded"] or not state["offices_loaded"]:
        raise HTTPException(status_code=409, detail="Сначала получите CSV сотрудников и GeoJSON офисов.")
    if body.minutes != 30:
        raise HTTPException(status_code=422, detail="Предел доступности должен составлять 30 минут.")
    matches = [
        employee for employee in EMPLOYEES.values()
        if abs(body.latitude - float(employee["latitude"])) <= 0.00001
        and abs(body.longitude - float(employee["longitude"])) <= 0.00001
        and body.mode == employee["mode"]
    ]
    if len(matches) != 1:
        raise HTTPException(status_code=422, detail="По этим координатам и способу передвижения не найден один сотрудник в CSV.")
    employee_id = matches[0]["id"]
    first_request = employee_id not in state["processed"]
    if first_request:
        state["processed"].append(employee_id)
        state["revision"] += 1
    record(state, "isochrone_requested", employee_id, {"first_request": first_request})
    return JSONResponse(ZONES[employee_id], media_type="application/geo+json")


@app.post("/decision", summary="4. Выбрать подходящие офисы")
def submit_decision(body: OfficeDecision, state: dict = Depends(authenticated_session)):
    previous_submission = submission_for(state["session_id"])
    if previous_submission and previous_submission["result"] == "passed":
        raise HTTPException(status_code=409, detail="Работа уже сдана.")
    if len(state["processed"]) < len(EMPLOYEES):
        raise HTTPException(status_code=409, detail="Сначала получите зоны всех восьми сотрудников.")
    selected = set(body.office_ids)
    if len(selected) != len(body.office_ids) or not selected <= OFFICE_IDS:
        raise HTTPException(status_code=422, detail="Укажите существующие коды офисов без повторов.")
    accepted = selected == VALID_OFFICES
    state["decision"] = {"office_ids": body.office_ids, "accepted": accepted}
    state["revision"] += 1
    record(state, "office_decision", detail={"office_ids": body.office_ids, "accepted": accepted})
    return {
        "accepted": accepted,
        "message": "Выбор подтверждён." if accepted else "Сравните офисы с общей областью и попробуйте ещё раз.",
    }


def submission_for(session_id: str) -> dict | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT result, submitted_at FROM submissions WHERE session_id = ?", (session_id,)
        ).fetchone()
    return dict(row) if row else None


def notification_text(state: dict, result: str, submitted_at: str) -> str:
    label = "Сдана" if result == "passed" else "Не сдана"
    return (
        f"{label} лабораторная №10: {state['surname']} {state['first_name']}, "
        f"группа {state['group_name']}.\n"
        f"Зоны: {len(state['processed'])}/{len(EMPLOYEES)}; "
        f"выбор офисов: {'верный' if state['decision'] and state['decision']['accepted'] else 'не подтверждён'}.\n"
        f"Время: {submitted_at}"
    )


def deliver_notification(notification_id: int) -> str:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return "pending"
    claim_time = now_utc()
    stale_claim = (claim_time - timedelta(minutes=2)).isoformat()
    with connect() as connection:
        row = connection.execute(
            "SELECT text, delivery_status FROM notifications WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
        if row is None or row["delivery_status"] == "sent":
            return "sent" if row else "missing"
        claimed = connection.execute(
            """UPDATE notifications SET claimed_at = ?
               WHERE notification_id = ? AND delivery_status IN ('pending', 'failed')
                 AND (claimed_at IS NULL OR claimed_at < ?)""",
            (claim_time.isoformat(), notification_id, stale_claim),
        )
        if claimed.rowcount != 1:
            return "pending"
    payload = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": row["text"]}).encode("utf-8")
    endpoint = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    request = urlrequest.Request(endpoint, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlrequest.urlopen(request, timeout=5) as response:
            result = json.load(response)
        success = bool(result.get("ok"))
        error_code = None if success else "telegram_rejected"
    except urlerror.HTTPError as exc:
        success = False
        error_code = f"telegram_http_{exc.code}"
    except (urlerror.URLError, TimeoutError, OSError, ValueError):
        success = False
        error_code = "telegram_network_error"
    with connect() as connection:
        connection.execute(
            """UPDATE notifications SET delivery_status = ?, attempt_count = attempt_count + 1,
               last_error = ?, claimed_at = NULL, sent_at = ? WHERE notification_id = ?""",
            (
                "sent" if success else "failed", error_code,
                now_utc().isoformat() if success else None, notification_id,
            ),
        )
    return "sent" if success else "failed"


@app.post("/submit", summary="5. Отправить работу и получить результат")
def submit_work(state: dict = Depends(authenticated_session)):
    if len(state["processed"]) < len(EMPLOYEES) or state["decision"] is None:
        raise HTTPException(status_code=409, detail="Сначала получите восемь зон и отправьте выбор офисов.")
    result = (
        "passed"
        if len(state["processed"]) == len(EMPLOYEES)
        and state["decision"] is not None
        and state["decision"]["accepted"]
        else "not_passed"
    )
    existing = submission_for(state["session_id"])
    if existing and existing["result"] == result:
        with connect() as connection:
            last_notification = connection.execute(
                "SELECT delivery_status FROM notifications WHERE session_id = ? ORDER BY notification_id DESC LIMIT 1",
                (state["session_id"],),
            ).fetchone()
        return {
            "result": result, "submitted_at": existing["submitted_at"],
            "notification": last_notification["delivery_status"] if last_notification else "pending",
        }
    submitted_at = now_utc().isoformat()
    message = notification_text(state, result, submitted_at)
    with connect() as connection:
        connection.execute(
            """INSERT INTO submissions(session_id, result, submitted_at) VALUES (?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET result = excluded.result, submitted_at = excluded.submitted_at""",
            (state["session_id"], result, submitted_at),
        )
        connection.execute(
            "INSERT INTO events(session_id, occurred_at, event_type, detail_json) VALUES (?, ?, 'work_submitted', ?)",
            (state["session_id"], submitted_at, json.dumps({"result": result})),
        )
        cursor = connection.execute(
            "INSERT INTO notifications(session_id, result, text, created_at) VALUES (?, ?, ?, ?)",
            (state["session_id"], result, message, submitted_at),
        )
        notification_id = cursor.lastrowid
        state["revision"] += 1
        connection.execute(
            "UPDATE sessions SET revision = ? WHERE session_id = ?",
            (state["revision"], state["session_id"]),
        )
    delivery = deliver_notification(notification_id)
    return {"result": result, "submitted_at": submitted_at, "notification": delivery}


@app.get("/state", include_in_schema=False)
def get_state(request: Request):
    state = find_session(request.headers.get("authorization"))
    if state is None:
        return {
            "version": "anonymous", "revision": 0, "registered": False,
            "student": None, "employees_loaded": False, "offices_loaded": False,
            "processed": [], "total": len(EMPLOYEES), "employees": [],
            "offices": [], "zones": [], "common_zone": None,
            "decision": None, "submission": None,
        }
    request.state.session_id = state["session_id"]
    processed = state["processed"]
    return {
        "version": f"{state['created_at']}:{state['revision']}",
        "revision": state["revision"],
        "registered": True,
        "student": {key: state[key] for key in ("surname", "first_name", "group_name")},
        "employees_loaded": state["employees_loaded"],
        "offices_loaded": state["offices_loaded"],
        "processed": processed,
        "total": len(EMPLOYEES),
        "employees": EMPLOYEE_POINTS if state["employees_loaded"] else [],
        "offices": PUBLIC_OFFICES if state["offices_loaded"] else [],
        "zones": [ZONES[employee_id] for employee_id in processed],
        "common_zone": common_zone(processed),
        "decision": state["decision"],
        "submission": submission_for(state["session_id"]),
    }


@app.post("/reset", include_in_schema=False)
def reset(state: dict = Depends(authenticated_session)):
    if submission_for(state["session_id"]) is not None:
        raise HTTPException(status_code=409, detail="После отправки работы начните новую сессию через регистрацию.")
    state.update({"employees_loaded": False, "offices_loaded": False, "processed": [], "decision": None})
    state["revision"] += 1
    record(state, "progress_reset")
    return {"message": "Прогресс очищен, регистрация сохранена. Начните с получения CSV и GeoJSON офисов."}
