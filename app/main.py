"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional

import bson.json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
MONTH_REGEX = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


# --------------------------------------------------------------------------- #
# Helpers & Conversions
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


def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    """Round a number half-up to given decimal places (R4, R8)."""
    if val is None:
        return None
    fmt = "0." + "0" * decimals if decimals > 0 else "1"
    return float(Decimal(str(val)).quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def get_month_days_range(month_str: str) -> tuple[str, str, int]:
    """Given 'YYYY-MM', returns (first_day_str, last_day_str, days_in_month)."""
    year, month = map(int, month_str.split("-"))
    _, num_days = calendar.monthrange(year, month)
    first_day = f"{month_str}-01"
    last_day = f"{month_str}-{num_days:02d}"
    return first_day, last_day, num_days


def count_working_days(start_date_str: str, end_date_str: str) -> int:
    """R7: Count Mon-Fri days between start_date_str and end_date_str inclusive."""
    start = datetime.strptime(start_date_str, "%Y-%m-%d").date()
    end = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    if start > end:
        return 0
    cur = start
    count = 0
    while cur <= end:
        if cur.weekday() < 5:  # Monday=0, ..., Friday=4
            count += 1
        cur += timedelta(days=1)
    return count


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
    10:00 (600s) after shift_start. When late, late_minutes = whole minutes (floor)
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
# Pipelines for Analytics & Explain
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    first_day, last_day, _ = get_month_days_range(month)
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": first_day, "$lte": last_day},
            }
        },
        {
            "$project": {
                "emp_code": 1,
                "date": 1,
                "status": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "half_day": 1,
                "dayOfWeek": {
                    "$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$lte": ["$dayOfWeek", 5]},  # Mon-Fri only
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    first_day, last_day, _ = get_month_days_range(month)
    match_stage: Dict[str, Any] = {"joined_on": {"$lte": last_day}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", first_day]},
                                    {"$lte": ["$date", last_day]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "logs_arrays": {"$push": "$logs"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "all_logs": {
                    "$reduce": {
                        "input": "$logs_arrays",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$$this.date"}}}, 5]},
                                            ]
                                        },
                                        {"$cond": ["$$this.half_day", 0.5, 1.0]},
                                        0.0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$all_logs",
                            "cond": {"$gt": [{"$ifNull": ["$$this.late_minutes", 0]}, 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": 0,
                        "in": {"$add": ["$$value", {"$ifNull": ["$$this.late_minutes", 0]}]},
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$all_logs",
                            "cond": {"$eq": ["$$this.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$all_logs",
                            "cond": {"$eq": ["$$this.status", "ON_DUTY"]},
                        }
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$all_logs",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$this.work_hours", None]},
                                        ]
                                    },
                                }
                            },
                            "as": "item",
                            "in": "$$item.work_hours",
                        }
                    }
                },
            }
        },
        {"$sort": {"department": 1}},
    ]


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> List[Dict[str, Any]]:
    first_day, last_day, _ = get_month_days_range(month)
    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": first_day, "$lte": last_day},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline


def build_department_trend_pipeline(department: str, from_str: str, to_str: str) -> List[Dict[str, Any]]:
    from_d = datetime.strptime(from_str, "%Y-%m-%d").date()
    to_d = datetime.strptime(to_str, "%Y-%m-%d").date()
    from_dt = datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc)
    to_dt = datetime(to_d.year, to_d.month, to_d.day, tzinfo=timezone.utc) + timedelta(days=1)

    return [
        {
            "$match": {
                "date": {"$gte": from_str, "$lte": to_str}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "d": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "d",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt],
                },
            }
        },
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "dayOfWeek": {"$isoDayOfWeek": "$d"},
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$lte": ["$dayOfWeek", 5]}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"current_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$current_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {"$round": ["$moving_avg_7d", 4]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


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


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeIn(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


# --------------------------------------------------------------------------- #
# Lifespan
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure indexes exist idempotently at startup
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index("joined_on")

    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index("date")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Endpoints
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


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        ts = epoch_ms_to_dt(body.punched_at)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)

    # Find the most recent record whose punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(404, "No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "Record is already punched out")

    p_in = record["punch_in"]
    if p_in.tzinfo is None:
        p_in = p_in.replace(tzinfo=timezone.utc)

    diff_seconds = (ts - p_in).total_seconds()
    if diff_seconds <= 0 or diff_seconds > 24 * 3600:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")

    work_hours = compute_work_hours(p_in, ts)
    overtime_minutes = compute_overtime(ts, emp["shift_end"], record["date"], emp["shift_start"])
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "Record is already punched out")

    return doc_to_attendance_record(updated)


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


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, "Invalid date format")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Attendance record not found")

    # Determine target status and punches
    old_status = record.get("status")
    target_status = body.status if body.status is not None else old_status

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "Cannot supply punch_in or punch_out when status is ABSENT or LEAVE")
        target_punch_in = None
        target_punch_out = None
    else:
        if body.punch_in is not None:
            target_punch_in = epoch_ms_to_dt(body.punch_in)
        else:
            target_punch_in = record.get("punch_in")
            if target_punch_in and target_punch_in.tzinfo is None:
                target_punch_in = target_punch_in.replace(tzinfo=timezone.utc)

        if body.punch_out is not None:
            target_punch_out = epoch_ms_to_dt(body.punch_out)
        else:
            target_punch_out = record.get("punch_out")
            if target_punch_out and target_punch_out.tzinfo is None:
                target_punch_out = target_punch_out.replace(tzinfo=timezone.utc)

        # Presence status requires punch_in
        if target_status in ("PRESENT", "WFH", "ON_DUTY") and target_punch_in is None:
            raise HTTPException(422, "Presence status requires punch_in")

        # punch_in must stay on record's attendance date (R1)
        if target_punch_in is not None:
            derived_date = get_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
            if derived_date != date:
                raise HTTPException(422, "punch_in must stay on the record's attendance date")

        # punch_out must be after punch_in and within 24 hours
        if target_punch_out is not None:
            if target_punch_in is None:
                raise HTTPException(422, "punch_out cannot be set without punch_in")
            diff_sec = (target_punch_out - target_punch_in).total_seconds()
            if diff_sec <= 0 or diff_sec > 24 * 3600:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    # Compute target derived values
    if target_status in ("ABSENT", "LEAVE"):
        new_late_minutes = 0
        new_work_hours = None
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], date)
        if target_punch_out is not None:
            new_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            new_overtime_minutes = compute_overtime(target_punch_out, emp["shift_end"], date, emp["shift_start"])
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Collect actual changes
    changes: Dict[str, Any] = {}
    if old_status != target_status:
        changes["status"] = {"from": old_status, "to": target_status}

    old_p_in = record.get("punch_in")
    if old_p_in and old_p_in.tzinfo is None:
        old_p_in = old_p_in.replace(tzinfo=timezone.utc)
    if old_p_in != target_punch_in:
        changes["punch_in"] = {"from": old_p_in, "to": target_punch_in}

    old_p_out = record.get("punch_out")
    if old_p_out and old_p_out.tzinfo is None:
        old_p_out = old_p_out.replace(tzinfo=timezone.utc)
    if old_p_out != target_punch_out:
        changes["punch_out"] = {"from": old_p_out, "to": target_punch_out}

    old_work_hours = record.get("work_hours")
    if old_work_hours != new_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}

    old_late_minutes = record.get("late_minutes", 0)
    if old_late_minutes != new_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}

    old_overtime_minutes = record.get("overtime_minutes", 0)
    if old_overtime_minutes != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}

    old_half_day = record.get("half_day", False)
    if old_half_day != new_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(422, "Request changes nothing")

    history_now = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": history_now,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    cur_history_len = len(record.get("history", []))
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": cur_history_len},
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "Concurrent modification conflict")

    return doc_to_attendance_record(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    first_day, last_day, _ = get_month_days_range(month)
    if emp["joined_on"] > last_day:
        working_days = 0
    else:
        effective_start = max(first_day, emp["joined_on"])
        working_days = count_working_days(effective_start, last_day)

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        agg = results[0]
        present_days = float(agg.get("present_days", 0.0))
        leave_days = int(agg.get("leave_days", 0))
        late_count = int(agg.get("late_count", 0))
        total_late_minutes = int(agg.get("total_late_minutes", 0))
        total_overtime_minutes = int(agg.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up(present_days / working_days * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    items = list(db.employees.aggregate(pipeline))

    for item in items:
        if item.get("avg_work_hours") is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)
        if item.get("present_days") is not None:
            item["present_days"] = float(item["present_days"])

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to_: str = Query(..., alias="to"),
):
    try:
        from_d = datetime.strptime(from_, "%Y-%m-%d")
        to_d = datetime.strptime(to_, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, "Invalid date format for from or to")

    if to_d < from_d:
        raise HTTPException(422, "to date cannot be earlier than from date")

    if (to_d - from_d).days > 92:
        raise HTTPException(422, "range cannot exceed 92 days")

    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(404, "Department not found")

    pipeline = build_department_trend_pipeline(department, from_, to_)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to_: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    valid_endpoints = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
    if endpoint not in valid_endpoints:
        raise HTTPException(422, f"Invalid endpoint: must be one of {valid_endpoints}")

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from cannot be after date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        cmd = {
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        res = db.command(cmd)
        clean_res = bson.json_util.loads(bson.json_util.dumps(res))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_res}

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, "emp_code is required for employee_monthly")
        if not month or not MONTH_REGEX.match(month):
            raise HTTPException(422, "Valid month is required (YYYY-MM)")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(cmd)
        clean_res = bson.json_util.loads(bson.json_util.dumps(res))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_res}

    elif endpoint == "department_summary":
        if not month or not MONTH_REGEX.match(month):
            raise HTTPException(422, "Valid month is required (YYYY-MM)")
        pipeline = build_department_summary_pipeline(month, department)
        cmd = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(cmd)
        clean_res = bson.json_util.loads(bson.json_util.dumps(res))
        return {"endpoint": endpoint, "collection": "employees", "explain": clean_res}

    elif endpoint == "late_leaderboard":
        if not month or not MONTH_REGEX.match(month):
            raise HTTPException(422, "Valid month is required (YYYY-MM)")
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(cmd)
        clean_res = bson.json_util.loads(bson.json_util.dumps(res))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_res}

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(422, "department is required for department_trend")
        if not from_ or not to_:
            raise HTTPException(422, "from and to are required for department_trend")
        try:
            from_d = datetime.strptime(from_, "%Y-%m-%d")
            to_d = datetime.strptime(to_, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(422, "Invalid date format for from or to")
        if to_d < from_d or (to_d - from_d).days > 92:
            raise HTTPException(422, "Invalid date range")

        pipeline = build_department_trend_pipeline(department, from_, to_)
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(cmd)
        clean_res = bson.json_util.loads(bson.json_util.dumps(res))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_res}
