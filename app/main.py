"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to epoch milliseconds integer (UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_dt(ms: Optional[int]) -> Optional[datetime]:
    """Convert epoch milliseconds to timezone-aware UTC datetime truncated to seconds (R1)."""
    if ms is None:
        return None
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def get_attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date is IST calendar date.
    For overnight shifts (shift_end <= shift_start), a punch-in before shift_end
    belongs to the shift that started the previous day.
    """
    p_in = punch_in if punch_in.tzinfo else punch_in.replace(tzinfo=timezone.utc)
    p_in_ist = p_in.astimezone(IST)
    ist_time = p_in_ist.strftime("%H:%M")
    if shift_end <= shift_start and ist_time < shift_end:
        return (p_in_ist.date() - timedelta(days=1)).isoformat()
    return p_in_ist.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: Optional[str] = None) -> int:
    """
    R2: Grace period is 10 minutes. A punch-in is late only if strictly more than
    10:00 (600 seconds) after shift_start. When late, late_minutes = whole minutes (floor)
    elapsed since shift_start, not since grace period ended. Otherwise 0.
    """
    p_in = punch_in if punch_in.tzinfo else punch_in.replace(tzinfo=timezone.utc)
    p_in_ist = p_in.astimezone(IST)
    if not attendance_date:
        attendance_date = p_in_ist.date().isoformat()
    sh, sm = map(int, shift_start.split(":"))
    start = datetime.fromisoformat(attendance_date).replace(hour=sh, minute=sm, second=0, microsecond=0, tzinfo=IST)
    elapsed_seconds = (p_in_ist - start).total_seconds()
    if elapsed_seconds > 600:
        return int(elapsed_seconds // 60)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    Instants are truncated to whole seconds (R1).
    """
    p_in = punch_in if punch_in.tzinfo else punch_in.replace(tzinfo=timezone.utc)
    p_out = punch_out if punch_out.tzinfo else punch_out.replace(tzinfo=timezone.utc)
    sec = int((p_out - p_in).total_seconds())
    if sec <= 0:
        return 0.0
    hours = (Decimal(sec) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str = "09:30") -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; otherwise 0.
    Handles overnight shifts across midnight (shift_end <= shift_start).
    """
    p_out = punch_out if punch_out.tzinfo else punch_out.replace(tzinfo=timezone.utc)
    p_out_ist = p_out.astimezone(IST)
    eh, em = map(int, shift_end.split(":"))
    if shift_end <= shift_start:
        end_date = datetime.fromisoformat(date_str).date() + timedelta(days=1)
        end = datetime(end_date.year, end_date.month, end_date.day, eh, em, 0, tzinfo=IST)
    else:
        end = datetime.fromisoformat(date_str).replace(hour=eh, minute=em, second=0, microsecond=0, tzinfo=IST)

    elapsed_seconds = (p_out_ist - end).total_seconds()
    if elapsed_seconds < 0:
        return 0
    minutes = int(elapsed_seconds // 60)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: After punch-out, half_day is true when work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def doc_to_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize MongoDB employee document to API response shape."""
    res = dict(doc)
    res.pop("_id", None)
    if "created_at" in res and isinstance(res["created_at"], datetime):
        res["created_at"] = dt_to_epoch_ms(res["created_at"])
    return res


def doc_to_attendance_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize MongoDB attendance document to API response shape (no id/_id, epoch ms)."""
    res = dict(doc)
    res.pop("_id", None)
    res.pop("id", None)

    res["punch_in"] = dt_to_epoch_ms(res.get("punch_in"))
    res["punch_out"] = dt_to_epoch_ms(res.get("punch_out"))

    if "half_day" not in res or res["half_day"] is None:
        res["half_day"] = False
    if "overtime_minutes" not in res or res["overtime_minutes"] is None:
        res["overtime_minutes"] = 0
    if "late_minutes" not in res or res["late_minutes"] is None:
        res["late_minutes"] = 0
    if "history" not in res or res["history"] is None:
        res["history"] = []
    else:
        formatted_history = []
        for h in res["history"]:
            h_copy = dict(h)
            if "at" in h_copy and isinstance(h_copy["at"], datetime):
                h_copy["at"] = dt_to_epoch_ms(h_copy["at"])
            if "changes" in h_copy and isinstance(h_copy["changes"], dict):
                changes_copy = {}
                for k, v in h_copy["changes"].items():
                    v_copy = dict(v)
                    if k in ("punch_in", "punch_out"):
                        if isinstance(v_copy.get("from"), datetime):
                            v_copy["from"] = dt_to_epoch_ms(v_copy["from"])
                        if isinstance(v_copy.get("to"), datetime):
                            v_copy["to"] = dt_to_epoch_ms(v_copy["to"])
                    changes_copy[k] = v_copy
                h_copy["changes"] = changes_copy
            formatted_history.append(h_copy)
        res["history"] = formatted_history

    return res


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @model_validator(mode="after")
    def validate_employee(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            datetime.strptime(self.joined_on, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on must be a valid date in YYYY-MM-DD format")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


# --------------------------------------------------------------------------- #
# Lifespan
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure required indexes exist idempotently at startup
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Endpoints provided (Fixed in Part A)
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return doc_to_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [doc_to_employee(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        ts = epoch_ms_to_dt(body.punched_at)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)

    attendance_date = get_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(ts, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return doc_to_attendance_record(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        try:
            datetime.strptime(date_from, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(422, "Invalid date_from format")
    if date_to:
        try:
            datetime.strptime(date_to, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(422, "Invalid date_to format")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be after date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        if status not in ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]:
            raise HTTPException(422, "Invalid status")
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [doc_to_attendance_record(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
