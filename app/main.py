"""
Employee Attendance & Analytics API
Run from repository root: uvicorn app.main:app --port 8000
Environment: MONGO_URI, MONGO_DB. Real environment variables override .env.
"""
import os
import re
import calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, Any

import math

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

load_dotenv()  # override=False by default: process environment wins.
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000, tz_aware=False)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
@app.on_event("startup")
def startup_event():
    create_indexes()


def ensure_index(collection, keys, *, unique=False, preferred_name=None):
    """Create an index only when an equivalent index is absent.

    Existing index names may differ across databases. Reuse equivalent indexes
    rather than trying to rename them at startup. Never silently accept a
    non-unique index where uniqueness is required.
    """
    wanted = list(keys)
    for existing in collection.list_indexes():
        existing_keys = list(existing["key"].items())
        if existing_keys == wanted:
            existing_unique = bool(existing.get("unique", False))
            if unique and not existing_unique:
                raise RuntimeError(
                    f"Index {collection.name}.{existing['name']} has keys {wanted} "
                    "but is not unique. Resolve duplicate data and migrate this index "
                    "to unique before starting the API."
                )
            if not unique and existing_unique:
                # A unique index is stricter and safely serves a non-unique query.
                return existing["name"]
            return existing["name"]
    options = {"unique": True} if unique else {}
    if preferred_name:
        options["name"] = preferred_name
    return collection.create_index(wanted, **options)


def create_indexes():
    """Idempotently ensure indexes without conflicting on historical index names."""
    ensure_index(db.employees, [("emp_code", 1)], unique=True, preferred_name="emp_code_1")
    ensure_index(db.employees, [("department", 1), ("joined_on", 1)], preferred_name="department_joined")
    ensure_index(db.attendance_logs, [("emp_code", 1), ("date", 1)], unique=True, preferred_name="emp_code_1_date_1")
    ensure_index(db.attendance_logs, [("date", -1), ("emp_code", 1)], preferred_name="attendance_date_employee")
    ensure_index(db.attendance_logs, [("emp_code", 1), ("date", 1), ("status", 1)], preferred_name="attendance_employee_date_status")
    ensure_index(db.attendance_logs, [("status", 1), ("date", -1)], preferred_name="attendance_status_date")


# ------------------------------- Models ------------------------------------
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, v):
        parse_date(v)
        return v

    @model_validator(mode="after")
    def different_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None:
            validate_epoch_ms(v)
        return v


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None:
            validate_epoch_ms(v)
        return v


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None:
            validate_epoch_ms(v)
        return v


# -------------------------------- Helpers ----------------------------------
def parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("Date must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise ValueError("Date must be YYYY-MM-DD")
    return parsed


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    return start, end


def validate_epoch_ms(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise ValueError("Timestamp must be an integer epoch-millisecond value in range")


def epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = truncate_seconds(dt)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    validate_epoch_ms(value)
    return truncate_seconds(datetime.fromtimestamp(value / 1000, tz=UTC).replace(tzinfo=None))


def as_utc_aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def truncate_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def to_ist(dt: datetime) -> datetime:
    return as_utc_aware(dt).astimezone(IST)


def clock_parts(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def attendance_date_for_punch(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = to_ist(punch_in)
    attendance_day = local.date()

    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        attendance_day -= timedelta(days=1)

    return attendance_day.isoformat()


def shift_start_instant(punch_in: datetime, shift_start: str, shift_end: str) -> datetime:
    date_str = attendance_date_for_punch(punch_in, shift_start, shift_end)
    day = parse_date(date_str)
    h, m = clock_parts(shift_start)
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str) -> int:
    local = to_ist(truncate_seconds(punch_in))
    start = shift_start_instant(local, shift_start, shift_end)
    elapsed_seconds = (local - start).total_seconds()
    if elapsed_seconds <= 10 * 60:
        return 0
    return max(0, int(elapsed_seconds // 60))


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (as_utc_aware(punch_out) - as_utc_aware(punch_in)).total_seconds()
    value = Decimal(str(seconds)) / Decimal("3600")
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: str, attendance_date: str) -> int:
    h, m = clock_parts(shift_end)
    day = parse_date(attendance_date)
    end = datetime(day.year, day.month, day.day, h, m, tzinfo=IST)
    punch_out_local = to_ist(truncate_seconds(punch_out))
    if punch_out_local <= end:
        return 0
    minutes = int((punch_out_local - end).total_seconds() // 60)
    return max(0, minutes)


def overtime_for_employee(punch_out: datetime, emp: dict, attendance_date: str) -> int:
    h, m = clock_parts(emp["shift_end"])
    day = parse_date(attendance_date)
    end = datetime(day.year, day.month, day.day, h, m, tzinfo=IST)
    if emp["shift_end"] <= emp["shift_start"]:
        end += timedelta(days=1)
    minutes = int((to_ist(punch_out) - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def serialize_history(history: list) -> list:
    out = []
    for entry in history or []:
        changes = {}
        for key, change in (entry.get("changes") or {}).items():
            changes[key] = {
                "from": epoch_ms(change.get("from")) if key in {"punch_in", "punch_out"} else change.get("from"),
                "to": epoch_ms(change.get("to")) if key in {"punch_in", "punch_out"} else change.get("to"),
            }
        out.append({
            "at": epoch_ms(entry.get("at")),
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": changes,
        })
    return out


def serialize_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": epoch_ms(doc.get("punch_in")),
        "punch_out": epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": serialize_history(doc.get("history", [])),
    }


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": epoch_ms(doc["created_at"]),
    }


def ensure_date_query(date_from: Optional[str], date_to: Optional[str]) -> dict:
    q = {}
    try:
        if date_from:
            parse_date(date_from)
        if date_to:
            parse_date(date_to)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    return q


def validate_pagination(page: int, page_size: int):
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(422, "Invalid pagination parameters")


def half_up(value: float, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def present_fraction_expr():
    return {"$cond": [
        {"$in": ["$status", list(PRESENCE)]},
        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
        0.0,
    ]}


def weekday_count(start: date, end: date) -> int:
    """Weekday count for validation/fallback only; analytics are calculated in MongoDB."""
    if end < start:
        return 0
    days = (end - start).days + 1
    weeks, rem = divmod(days, 7)
    count = weeks * 5
    for i in range(rem):
        if (start.weekday() + i) % 7 < 5:
            count += 1
    return count




from datetime import date, datetime
from bson import ObjectId

def json_safe(value):
    """Convert MongoDB explain output into JSON-safe values."""
    if isinstance(value, dict):
        return {
            str(k): json_safe(v)
            for k, v in value.items()
            if k != "_id"
        }

    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]

    if isinstance(value, ObjectId):
        return str(value)

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, bytes):
        # Binary explain data must not be decoded as UTF-8.
        return value.hex()

    if value.__class__.__name__ == "Timestamp":
        return str(value)

    if hasattr(value, "item") and callable(value.item):
        try:
            return json_safe(value.item())
        except (ValueError, TypeError):
            pass

    return value


# -------------------------------- Endpoints --------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0).replace(tzinfo=None)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    except PyMongoError:
        raise HTTPException(503, "Database unavailable")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    docs = db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    ts = from_epoch_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0).replace(tzinfo=None)
    local = to_ist(ts)
    shift_start, shift_end = emp["shift_start"], emp["shift_end"]
    attendance_day = local.date()
    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        attendance_day -= timedelta(days=1)
    date_str = attendance_day.isoformat()
    doc = {
        "emp_code": body.emp_code, "date": date_str, "status": body.status,
        "punch_in": ts, "punch_out": None, "work_hours": None,
        "late_minutes": compute_late_minutes(ts, shift_start, shift_end),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date")
    except PyMongoError:
        raise HTTPException(503, "Database unavailable")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
   
    ts = (
        from_epoch_ms(body.punched_at)
        if body.punched_at is not None
        else datetime.now(UTC).replace(microsecond=0).replace(tzinfo=None)
    )

    # Check whether a completed punch-in already exists at or before this timestamp.
    previous = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": ts},
            "punch_out": {"$ne": None},
        },
        sort=[("punch_in", -1), ("date", -1)],
    )

    if previous:
        raise HTTPException(409, "Attendance record already punched out")

    # Find the most recent open attendance record.
    doc = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": ts},
            "punch_out": None,
        },
        sort=[("punch_in", -1), ("date", -1)],
    )

    if not doc:
        raise HTTPException(404, "No punch-in found")
    
    delta = (
        as_utc_aware(ts) - as_utc_aware(doc["punch_in"])
    ).total_seconds()

    if delta <= 0 or delta > 24 * 3600:
        raise HTTPException(
            422,
            "punch-out must be after punch-in and within 24 hours",
        )

    work_hours = compute_work_hours(doc["punch_in"], ts)
    overtime = overtime_for_employee(ts, emp, doc["date"])
    half_day = work_hours < 4.50

    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": body.emp_code,
            "date": doc["date"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(
            409, "Attendance record already punched out"
        )

    return serialize_attendance(updated)




@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    q.update(ensure_date_query(date_from, date_to))
    if status is not None:
        if status not in STATUSES:
            raise HTTPException(422, "Invalid status")
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    docs = db.attendance_logs.find(q, {"_id": 0}).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_attendance(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    try:
        parse_date(date)
    except ValueError as e:
        raise HTTPException(422, str(e))
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old:
        raise HTTPException(404, "Attendance record not found")
    status = body.status if body.status is not None else old["status"]
    pin = from_epoch_ms(body.punch_in) if body.punch_in is not None else old.get("punch_in")
    pout = from_epoch_ms(body.punch_out) if body.punch_out is not None else old.get("punch_out")
    if status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot include punch times")
        pin, pout = None, None
    elif pin is None:
        raise HTTPException(422, "Presence status requires punch_in")
    if pin is not None:
        local_pin = to_ist(pin)
        expected_day = parse_date(date)
        actual_day = local_pin.date()
        if emp["shift_end"] <= emp["shift_start"] and local_pin.strftime("%H:%M") < emp["shift_end"]:
            actual_day -= timedelta(days=1)
        if actual_day != expected_day:
            raise HTTPException(422, "punch_in must belong to the attendance date")
    if pout is not None:
        if pin is None:
            raise HTTPException(422, "punch_out requires punch_in")
        delta = (as_utc_aware(pout) - as_utc_aware(pin)).total_seconds()
        if delta <= 0 or delta > 24 * 3600:
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    late = compute_late_minutes(pin, emp["shift_start"], emp["shift_end"]) if pin else 0
    work_hours = compute_work_hours(pin, pout) if pin and pout else None
    overtime = overtime_for_employee(pout, emp, date) if pin and pout else 0
    half_day = bool(work_hours is not None and work_hours < 4.50)
    final_values = {
        "status": status, "punch_in": pin, "punch_out": pout, "work_hours": work_hours,
        "late_minutes": late, "overtime_minutes": overtime, "half_day": half_day,
    }
    changes = {}
    for field, new_value in final_values.items():
        old_value = old.get(field, False if field == "half_day" else 0 if field in {"late_minutes", "overtime_minutes"} else None)
        if field in {"punch_in", "punch_out"}:
            old_cmp, new_cmp = epoch_ms(old_value), epoch_ms(new_value)
        else:
            old_cmp, new_cmp = old_value, new_value
        if old_cmp != new_cmp:
            changes[field] = {"from": old_cmp, "to": new_cmp}
    if not changes:
        raise HTTPException(422, "Request does not change the record")
    now = datetime.now(UTC).replace(microsecond=0).replace(tzinfo=None)
    history_entry = {"at": now, "by": body.regularized_by, "reason": body.reason, "changes": {}}
    for key, value in changes.items():
        if key in {"punch_in", "punch_out"}:
            history_entry["changes"][key] = {"from": old.get(key), "to": final_values[key]}
        else:
            history_entry["changes"][key] = value
    # Compare-and-swap on the old record values prevents silent lost updates/history.
    selector = {"emp_code": emp_code, "date": date}
    selector["history"] = old.get("history", [])
    for key in final_values:
        selector[key] = old.get(key, False if key == "half_day" else 0 if key in {"late_minutes", "overtime_minutes"} else None)
    updated = db.attendance_logs.find_one_and_update(
        selector,
        {"$set": final_values, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "Record changed concurrently; retry regularization")
    return serialize_attendance(updated)


# ------------------------------ Aggregations -------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = parse_month(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    joined = max(start, parse_date(emp["joined_on"]))
    working_days = weekday_count(joined, end)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", list(PRESENCE)]},
                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]},
                ]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]}, 0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    result = list(db.attendance_logs.aggregate(pipeline))
    data = result[0] if result else {}
    pct = None if working_days == 0 else half_up((data.get("present_days", 0) / working_days) * 100, 2)
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": data.get("present_days", 0), "leave_days": data.get("leave_days", 0),
        "late_count": data.get("late_count", 0), "total_late_minutes": data.get("total_late_minutes", 0),
        "total_overtime_minutes": data.get("total_overtime_minutes", 0), "attendance_pct": pct,
    }


def department_summary_pipeline(month: str, department: Optional[str] = None):
    start, end = parse_month(month)
    match = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]},
                ]}}},
                {"$group": {"_id": None,
                    "present_days": {"$sum": {"$cond": [
                        {"$and": [
                            {"$in": ["$status", list(PRESENCE)]},
                            {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2,3,4,5,6]]},
                        ]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]}, 0,
                    ]}},
                    "avg_work_hours_sum": {"$sum": {"$cond": [
                        {"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$ne": ["$work_hours", None]}]},
                        "$work_hours", 0,
                    ]}},
                    "avg_work_hours_count": {"$sum": {"$cond": [
                        {"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$ne": ["$work_hours", None]}]}, 1, 0
                    ]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ], "as": "metrics"
        }},
        {"$unwind": {"path": "$metrics", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department", "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$metrics.present_days", 0]}},
            "work_sum": {"$sum": {"$ifNull": ["$metrics.avg_work_hours_sum", 0]}},
            "work_count": {"$sum": {"$ifNull": ["$metrics.avg_work_hours_count", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$metrics.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$metrics.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$metrics.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$metrics.on_duty_count", 0]}},
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$work_count", 0]}, {"$round": [{"$divide": ["$work_sum", "$work_count"]}, 2]}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    parse_month(month)
    items = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = parse_month(month)
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$set": {"emp_code": "$_id", "name": "$employee.name", "department": "$employee.department"}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {"_id": 0, "emp_code": 1, "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1, "rank": 1}},
    ]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    """Generate every calendar date in MongoDB, including dates with no logs."""
    try:
        start, end = parse_date(from_date), parse_date(to_date)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if end < start:
        raise HTTPException(422, "to must be on or after from")
    day_count = (end - start).days + 1
    if day_count > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "Unknown department")

    start_dt = datetime(start.year, start.month, start.day)
    # The aggregation starts from a real department employee, then generates the
    # calendar using $range. This works even when attendance_logs has zero rows.
    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {
            "_id": 0,
            "dates": {"$map": {
                "input": {"$range": [0, day_count]},
                "as": "offset",
                "in": {"$dateAdd": {"startDate": {"$literal": start_dt}, "unit": "day", "amount": "$$offset"}},
            }},
        }},
        {"$unwind": "$dates"},
        {"$set": {"date_string": {"$dateToString": {"date": "$dates", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
        {"$lookup": {"from": "attendance_logs", "let": {"day": "$date_string"}, "pipeline": [
            {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"},
            {"$match": {"$expr": {"$eq": ["$employee.department", department]}}},
            {"$group": {"_id": None,
                "present_count": {"$sum": {"$cond": [
                    {"$in": ["$status", list(PRESENCE)]},
                    {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]}, 0,
                ]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            }},
        ], "as": "daily"}},
        {"$set": {
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]},
            "is_working_day": {"$in": [{"$dayOfWeek": "$dates"}, [2, 3, 4, 5, 6]]},
        }},
        {"$lookup": {"from": "employees", "let": {"day": "$date_string"}, "pipeline": [
            {"$match": {"$expr": {"$and": [
                {"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$day"]}
            ]}}},
            {"$count": "count"},
        ], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]}, None,
        ]}}},
        {"$setWindowFields": {"sortBy": {"dates": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}
        }}},
        {"$project": {
            "_id": 0, "date": "$date_string", "is_working_day": 1, "headcount": 1,
            "present_count": 1, "late_count": 1, "attendance_rate": 1,
            "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]},
        }},
        {"$sort": {"date": 1}},
    ]
    items = list(db.employees.aggregate(pipeline, allowDiskUse=True))
    return {"department": department, "from": from_date, "to": to_date, "items": items}


# ------------------------------- Explain -----------------------------------
def explain_find(collection, query, sort, page, page_size):
    skip = (page - 1) * page_size
    return collection.database.command({
        "explain": {
            "find": collection.name, "filter": query, "sort": dict(sort),
            "skip": skip, "limit": page_size,
        },
        "verbosity": "executionStats",
    })


def explain_aggregate(collection, pipeline):
    return collection.database.command({
        "explain": {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}},
        "verbosity": "executionStats",
    })


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
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code is not None:
            q["emp_code"] = emp_code
        q.update(ensure_date_query(date_from, date_to))
        if status is not None:
            if status not in STATUSES:
                raise HTTPException(422, "Invalid status")
            q["status"] = status
        result = explain_find(db.attendance_logs, q, [("date", -1), ("emp_code", 1)], page, page_size)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json_safe(result)}
    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        start, end = parse_month(month)
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}}]
        result = explain_aggregate(db.attendance_logs, pipeline)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json_safe(result)}
    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        result = explain_aggregate(db.employees, department_summary_pipeline(month, department))
        return {"endpoint": endpoint, "collection": "employees", "explain": json_safe(result)}
    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        start, end = parse_month(month)
        pipeline = [
            {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
        ]
        result = explain_aggregate(db.attendance_logs, pipeline)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json_safe(result)}
    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required")
        # Explain the same indexed range query used to seed the trend pipeline.
        try:
            parse_date(from_date); parse_date(to_date)
        except ValueError as e:
            raise HTTPException(422, str(e))
        if from_date > to_date or (parse_date(to_date) - parse_date(from_date)).days + 1 > 92:
            raise HTTPException(422, "Invalid trend date range")
        pipeline = [{"$match": {"date": {"$gte": from_date, "$lte": to_date}}}]
        result = explain_aggregate(db.attendance_logs, pipeline)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json_safe(result)}
    raise HTTPException(422, "Unknown endpoint")

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
