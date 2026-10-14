"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
import re
import calendar
from datetime import datetime, timedelta, timezone, date as date_type
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Startup: create indexes idempotently
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_indexes():
    # employees: unique on emp_code (enforces concurrency for duplicate creates)
    db.employees.create_index("emp_code", unique=True, name="idx_emp_code_unique")

    # attendance_logs: unique on (emp_code, date) for concurrency-safe punch-in
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="idx_empcode_date_unique",
    )

    # attendance_logs: for list sorted by date desc, emp_code asc + date range queries
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_date_desc_empcode_asc",
    )

    # attendance_logs: for analytics date-range filtering (ascending for aggregation match)
    db.attendance_logs.create_index(
        [("date", ASCENDING)],
        name="idx_date_asc",
    )

    # attendance_logs: for punch-out lookup (find open records for an employee)
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="idx_empcode_punchin",
    )

    # employees: for department-based queries
    db.employees.create_index("department", name="idx_department")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def round_half_up(value, decimals=2):
    """Round to given decimal places using half-up rounding (not banker's rounding)."""
    if value is None:
        return None
    d = Decimal(str(value))
    if decimals == 0:
        return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    quant = Decimal("0." + "0" * decimals)
    return float(d.quantize(quant, rounding=ROUND_HALF_UP))


def truncate_to_seconds(dt: datetime) -> datetime:
    """Truncate a datetime to whole seconds (R1)."""
    return dt.replace(microsecond=0)


def epoch_ms_to_dt(ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC datetime, truncated to seconds."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return truncate_to_seconds(dt)


def dt_to_epoch_ms(dt: datetime) -> Optional[int]:
    """Convert a datetime to epoch milliseconds. Returns None for None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def validate_epoch_ms(value, field_name="punched_at"):
    """Validate an epoch milliseconds value per the contract."""
    if value is None:
        return None
    if not isinstance(value, int):
        raise HTTPException(422, detail=f"{field_name} must be an integer")
    if value < 100000000000:
        raise HTTPException(
            422, detail=f"{field_name} looks like seconds, must be milliseconds"
        )
    if value > 4102444800000:
        raise HTTPException(422, detail=f"{field_name} is out of range")
    return value


def get_ist_date(dt: datetime) -> str:
    """Get the IST calendar date string from a UTC datetime."""
    ist_dt = dt.astimezone(IST)
    return ist_dt.strftime("%Y-%m-%d")


def determine_attendance_date(punch_in_utc: datetime, emp: dict) -> str:
    """
    Determine the attendance date per R1.
    For overnight shifts (shift_end <= shift_start): if punch-in IST time
    is earlier than shift_end, it belongs to the previous day's shift.
    """
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)
    ist_dt = punch_in_utc.astimezone(IST)
    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]

    is_overnight = shift_end <= shift_start

    if is_overnight:
        # Parse shift_end time
        eh, em = map(int, shift_end.split(":"))
        punch_time_minutes = ist_dt.hour * 60 + ist_dt.minute + (1 if ist_dt.second > 0 else 0)
        end_minutes = eh * 60 + em

        if punch_time_minutes < end_minutes:
            # Punch-in is before shift_end -> belongs to previous day
            prev_day = ist_dt.date() - timedelta(days=1)
            return prev_day.isoformat()

    return ist_dt.date().isoformat()


def compute_late_minutes_for_date(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    """
    Compute late_minutes per R2.
    Grace period: 10 minutes. Late only if STRICTLY more than 10:00 after shift_start.
    When late: whole minutes (floor) elapsed since shift_start (not since grace end).
    Example (shift 09:30): 09:40:00 -> 0; 09:40:01 -> 10; 10:15:59 -> 45.
    """
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)
    ist_dt = punch_in_utc.astimezone(IST)
    h, m = map(int, shift_start.split(":"))

    # Build shift_start datetime on the attendance date
    att_d = date_type.fromisoformat(attendance_date)
    start_dt = datetime(att_d.year, att_d.month, att_d.day, h, m, 0, 0, tzinfo=IST)

    diff_seconds = (ist_dt - start_dt).total_seconds()

    # Not late if before or at shift start
    if diff_seconds <= 0:
        return 0

    # Grace period: strictly more than 10 minutes (600 seconds)
    if diff_seconds <= 600:
        return 0

    # Late: floor of minutes since shift_start
    return int(diff_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """
    Compute work_hours per R4.
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals HALF-UP.
    """
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)
    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=timezone.utc)
    seconds = (punch_out - punch_in).total_seconds()
    return round_half_up(seconds / 3600, 2)


def compute_overtime_minutes(
    punch_out_utc: datetime, shift_end: str, shift_start: str, attendance_date: str
) -> int:
    """
    Compute overtime_minutes per R3.
    Floor of minutes between shift_end and punch_out, counted only when >= 30, otherwise 0.
    """
    if punch_out_utc.tzinfo is None:
        punch_out_utc = punch_out_utc.replace(tzinfo=timezone.utc)
    h, m = map(int, shift_end.split(":"))
    att_date = date_type.fromisoformat(attendance_date)
    end_dt = datetime(att_date.year, att_date.month, att_date.day, h, m, 0, 0, tzinfo=IST)

    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_dt += timedelta(days=1)

    punch_out_ist = punch_out_utc.astimezone(IST)
    diff_seconds = (punch_out_ist - end_dt).total_seconds()

    if diff_seconds <= 0:
        return 0

    minutes = int(diff_seconds // 60)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    """Compute half_day per R5. True when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def format_attendance_record(doc: dict) -> dict:
    """Format a MongoDB attendance_logs document for the API response."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": format_history(doc.get("history", [])),
    }


def format_history(history: list) -> list:
    """Format history entries: convert BSON dates to epoch ms."""
    formatted = []
    for entry in history:
        h = {
            "at": dt_to_epoch_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": {},
        }
        for field, change in entry.get("changes", {}).items():
            from_val = change.get("from")
            to_val = change.get("to")
            # punch_in and punch_out values are datetimes in MongoDB, epoch ms in API
            if field in ("punch_in", "punch_out"):
                from_val = dt_to_epoch_ms(from_val) if isinstance(from_val, datetime) else from_val
                to_val = dt_to_epoch_ms(to_val) if isinstance(to_val, datetime) else to_val
            h["changes"][field] = {"from": from_val, "to": to_val}
        formatted.append(h)
    return formatted


def format_employee(doc: dict) -> dict:
    """Format an employee document for the API response."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc["created_at"]),
    }


def get_month_date_range(month_str: str):
    """Parse 'YYYY-MM' and return (first_day, last_day) as 'YYYY-MM-DD' strings."""
    match = re.match(r"^(\d{4})-(0[1-9]|1[0-2])$", month_str)
    if not match:
        raise HTTPException(422, detail="month must be YYYY-MM format")
    year, month = int(match.group(1)), int(match.group(2))
    first_day = f"{year:04d}-{month:02d}-01"
    last_day_num = calendar.monthrange(year, month)[1]
    last_day = f"{year:04d}-{month:02d}-{last_day_num:02d}"
    return first_day, last_day


def count_working_days(start_date_str: str, end_date_str: str) -> int:
    """Count Mon-Fri days between two dates inclusive."""
    start = date_type.fromisoformat(start_date_str)
    end = date_type.fromisoformat(end_date_str)
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:  # Mon=0 to Fri=4
            count += 1
        current += timedelta(days=1)
    return count


def validate_date_str(d: str, field_name: str = "date") -> str:
    """Validate a YYYY-MM-DD date string."""
    try:
        date_type.fromisoformat(d)
    except (ValueError, TypeError):
        raise HTTPException(422, detail=f"Invalid {field_name}: {d}")
    return d


# --------------------------------------------------------------------------- #
# Pydantic Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v):
        if not re.match(r"^EMP\d{4,6}$", v):
            raise ValueError("emp_code must be EMP followed by 4-6 digits")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v):
        if not v or len(v) > 100:
            raise ValueError("name must be 1-100 characters")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v):
        if len(v) > 120 or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("invalid email")
        return v

    @field_validator("department")
    @classmethod
    def validate_department(cls, v):
        if not v or len(v) > 50:
            raise ValueError("department must be 1-50 characters")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, v):
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v):
            raise ValueError("shift time must be HH:MM (24-hour)")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v):
        try:
            date_type.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v):
        if len(v) < 5 or len(v) > 200:
            raise ValueError("reason must be 5-200 characters")
        return v

    @field_validator("regularized_by")
    @classmethod
    def validate_regularized_by(cls, v):
        if len(v) < 1 or len(v) > 50:
            raise ValueError("regularized_by must be 1-50 characters")
        return v


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, detail="MongoDB unavailable")


# ---- Employees ---- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, detail="shift_start must differ from shift_end")

    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")

    doc.pop("_id", None)
    return format_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [format_employee(e) for e in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---- Attendance CRUD ---- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    # Validate status
    if body.status not in PRESENCE_STATUSES:
        raise HTTPException(
            422, detail=f"status must be one of {sorted(PRESENCE_STATUSES)}"
        )

    # Validate punched_at if provided
    if body.punched_at is not None:
        validate_epoch_ms(body.punched_at, "punched_at")
        ts = epoch_ms_to_dt(body.punched_at)
    else:
        ts = truncate_to_seconds(datetime.now(timezone.utc))

    # Lookup employee
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    # Determine attendance date (R1)
    att_date = determine_attendance_date(ts, emp)

    # Compute late_minutes (R2)
    late_minutes = compute_late_minutes_for_date(ts, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
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
        raise HTTPException(409, detail="Already punched in for this date")

    doc.pop("_id", None)
    return format_attendance_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    # Validate punched_at
    if body.punched_at is not None:
        validate_epoch_ms(body.punched_at, "punched_at")
        ts = epoch_ms_to_dt(body.punched_at)
    else:
        ts = truncate_to_seconds(datetime.now(timezone.utc))

    # Lookup employee
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    # Find the most recent record with punch_in <= punched_at and no punch_out
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts},
            "punch_out": None,
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(404, detail="No open punch-in found")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # Validate: punch_out must be after punch_in
    if ts <= punch_in_dt:
        raise HTTPException(422, detail="punch_out must be after punch_in")

    # Validate: within 24 hours of punch_in
    if (ts - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(422, detail="punch_out must be within 24 hours of punch_in")

    # Compute derived fields
    work_hours = compute_work_hours(punch_in_dt, ts)
    overtime = compute_overtime_minutes(
        ts, emp["shift_end"], emp["shift_start"], record["date"]
    )
    half_day = compute_half_day(work_hours)

    # Atomic update with concurrency check (punch_out must still be None)
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
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

    if result is None:
        raise HTTPException(409, detail="Already punched out (concurrent request)")

    return format_attendance_record(result)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # Validate date range
    if date_from:
        validate_date_str(date_from, "date_from")
    if date_to:
        validate_date_str(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must not be after date_to")

    if status and status not in ALL_STATUSES:
        raise HTTPException(422, detail=f"Invalid status: {status}")

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
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": [format_attendance_record(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---- Regularize (PATCH) ---- #
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    validate_date_str(date, "date")

    # Validate status if provided
    if body.status is not None and body.status not in ALL_STATUSES:
        raise HTTPException(422, detail=f"Invalid status: {body.status}")

    # Validate epoch ms if provided
    if body.punch_in is not None:
        validate_epoch_ms(body.punch_in, "punch_in")
    if body.punch_out is not None:
        validate_epoch_ms(body.punch_out, "punch_out")

    # Check employee exists
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    # Check record exists
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, detail="No attendance record for this date")

    # Determine new status
    new_status = body.status if body.status is not None else record["status"]

    # Rule: ABSENT/LEAVE clears both punch times; supplying punch_in or punch_out with it is 422
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                422,
                detail="Cannot supply punch_in or punch_out with ABSENT or LEAVE status",
            )
        new_punch_in = None
        new_punch_out = None
    else:
        # Presence status requires punch_in (supplied or already stored)
        if body.punch_in is not None:
            new_punch_in = epoch_ms_to_dt(body.punch_in)
        else:
            new_punch_in = record.get("punch_in")

        if new_punch_in is None:
            raise HTTPException(
                422, detail="A presence status requires a punch_in"
            )

        # Ensure punch_in timezone
        if new_punch_in.tzinfo is None:
            new_punch_in = new_punch_in.replace(tzinfo=timezone.utc)

        # Validate punch_in stays on the record's attendance date (R1)
        att_date_check = determine_attendance_date(new_punch_in, emp)
        if att_date_check != date:
            raise HTTPException(
                422, detail="punch_in must stay on the record's attendance date"
            )

        if body.punch_out is not None:
            new_punch_out = epoch_ms_to_dt(body.punch_out)
        else:
            new_punch_out = record.get("punch_out")

        if new_punch_out is not None:
            if new_punch_out.tzinfo is None:
                new_punch_out = new_punch_out.replace(tzinfo=timezone.utc)
            # punch_out must be after punch_in
            if new_punch_out <= new_punch_in:
                raise HTTPException(422, detail="punch_out must be after punch_in")
            # Within 24 hours
            if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(
                    422, detail="punch_out must be within 24 hours of punch_in"
                )

    # Compute derived fields for new state
    if new_status in ("ABSENT", "LEAVE"):
        new_late = 0
        new_work_hours = None
        new_overtime = 0
        new_half_day = False
    else:
        # Compute late_minutes from new punch_in
        new_late = compute_late_minutes_for_date(new_punch_in, emp["shift_start"], date)

        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime = compute_overtime_minutes(
                new_punch_out, emp["shift_end"], emp["shift_start"], date
            )
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime = 0
            new_half_day = False

    # Check if anything actually changed
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    old_status = record["status"]
    old_work_hours = record.get("work_hours")
    old_late = record.get("late_minutes", 0)
    old_overtime = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)

    # Normalize old values timezone
    if old_punch_in is not None and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
    if old_punch_out is not None and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)

    changes = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_punch_in != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if new_punch_out != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if new_work_hours != old_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}
    if new_overtime != old_overtime:
        changes["overtime_minutes"] = {"from": old_overtime, "to": new_overtime}
    if new_half_day != old_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(422, detail="No changes detected")

    # Build history entry
    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Atomic update with version check using the history length
    # This prevents concurrent edits from silently losing each other's history
    current_history_len = len(record.get("history", []))

    update = {
        "$set": {
            "status": new_status,
            "punch_in": new_punch_in,
            "punch_out": new_punch_out,
            "work_hours": new_work_hours,
            "late_minutes": new_late,
            "overtime_minutes": new_overtime,
            "half_day": new_half_day,
        },
        "$push": {"history": history_entry},
    }

    # Use the _id and history size as a concurrency guard
    filter_q = {
        "_id": record["_id"],
        "$expr": {
            "$eq": [
                {"$size": {"$ifNull": ["$history", []]}},
                current_history_len,
            ]
        },
    }

    result = db.attendance_logs.find_one_and_update(
        filter_q,
        update,
        return_document=ReturnDocument.AFTER,
    )

    if result is None:
        raise HTTPException(409, detail="Concurrent modification detected")

    return format_attendance_record(result)


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    # Check employee exists
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    first_day, last_day = get_month_date_range(month)

    # Determine working days (R7): from max(joined_on, first_day) to last_day
    joined = emp["joined_on"]
    effective_start = max(joined, first_day)
    if effective_start > last_day:
        working_days = 0
    else:
        working_days = count_working_days(effective_start, last_day)

    # Aggregation pipeline
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
        {
            "$addFields": {
                "date_obj": {
                    "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                },
                "_half_day": {"$ifNull": ["$half_day", False]},
                "_late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "_overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
            }
        },
        {
            "$addFields": {
                "_dow": {"$dayOfWeek": "$date_obj"},  # 1=Sun, 7=Sat
                "_is_weekday": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$date_obj"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$date_obj"}, 6]},
                    ]
                },
                "_is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$_is_present", "$_is_weekday"]},
                            {"$cond": ["$_half_day", 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$_late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$_late_minutes"},
                "total_overtime_minutes": {"$sum": "$_overtime_minutes"},
            }
        },
    ]

    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        r = results[0]
        present_days = r["present_days"]
        leave_days = r["leave_days"]
        late_count = r["late_count"]
        total_late_minutes = r["total_late_minutes"]
        total_overtime_minutes = r["total_overtime_minutes"]
    else:
        present_days = 0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    attendance_pct = (
        round_half_up(present_days / working_days * 100, 2)
        if working_days > 0
        else None
    )

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
    first_day, last_day = get_month_date_range(month)

    # Build employee filter for headcount (R9: joined_on <= last day of month)
    emp_filter = {"joined_on": {"$lte": last_day}}
    if department:
        emp_filter["department"] = department

    # Get all relevant employees
    employees = list(db.employees.find(emp_filter))

    # Group employees by department for headcount
    dept_employees = {}
    for emp in employees:
        dept = emp["department"]
        if dept not in dept_employees:
            dept_employees[dept] = []
        dept_employees[dept].append(emp)

    if not dept_employees:
        return {"month": month, "items": []}

    # Get all emp_codes for the relevant employees
    all_emp_codes = [e["emp_code"] for e in employees]

    # Aggregation pipeline for attendance stats per department
    att_pipeline = [
        {
            "$match": {
                "date": {"$gte": first_day, "$lte": last_day},
                "emp_code": {"$in": all_emp_codes},
            }
        },
        # Lookup employee to get department
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp_info",
            }
        },
        {"$unwind": "$emp_info"},
        # Compute helper fields
        {
            "$addFields": {
                "date_obj": {
                    "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                },
                "_half_day": {"$ifNull": ["$half_day", False]},
                "_late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "_is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            }
        },
        {
            "$addFields": {
                "_is_weekday": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$date_obj"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$date_obj"}, 6]},
                    ]
                },
                # work_hours is valid if present-status and work_hours field exists & is not null
                "_has_work_hours": {
                    "$and": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$ne": [{"$type": "$work_hours"}, "null"]},
                        {"$ne": [{"$type": "$work_hours"}, "missing"]},
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": "$emp_info.department",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$_is_present", "$_is_weekday"]},
                            {"$cond": ["$_half_day", 0.5, 1]},
                            0,
                        ]
                    }
                },
                "avg_work_hours_sum": {
                    "$sum": {
                        "$cond": ["$_has_work_hours", "$work_hours", 0]
                    }
                },
                "avg_work_hours_count": {
                    "$sum": {"$cond": ["$_has_work_hours", 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$_late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$_late_minutes"},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]

    att_results = {r["_id"]: r for r in db.attendance_logs.aggregate(att_pipeline)}

    items = []
    for dept in sorted(dept_employees.keys()):
        headcount = len(dept_employees[dept])
        if headcount == 0:
            continue

        att = att_results.get(dept, {})
        avg_count = att.get("avg_work_hours_count", 0)
        avg_sum = att.get("avg_work_hours_sum", 0)
        avg_work_hours = round_half_up(avg_sum / avg_count, 2) if avg_count > 0 else None

        items.append(
            {
                "department": dept,
                "headcount": headcount,
                "present_days": att.get("present_days", 0),
                "avg_work_hours": avg_work_hours,
                "late_count": att.get("late_count", 0),
                "total_late_minutes": att.get("total_late_minutes", 0),
                "leave_count": att.get("leave_count", 0),
                "on_duty_count": att.get("on_duty_count", 0),
            }
        )

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first_day, last_day = get_month_date_range(month)

    pipeline = [
        {"$match": {"date": {"$gte": first_day, "$lte": last_day}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        # Only employees with total > 0
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        # Lookup employee info
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp_info",
            }
        },
        # Logs whose employee does not exist are ignored
        {"$unwind": "$emp_info"},
    ]

    # Filter by department if specified
    if department:
        pipeline.append({"$match": {"emp_info.department": department}})

    pipeline.extend([
        # Sort by total_late_minutes desc, then emp_code asc for tie-breaking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        # Standard competition ranking using $setWindowFields
        # $rank ranks by the sortBy key; ties share a rank, next rank is skipped
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}},
                },
            }
        },
        # Filter by rank <= limit (includes all ties at the cutoff)
        {"$match": {"rank": {"$lte": limit}}},
        # Project final shape
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp_info.name",
                "department": "$emp_info.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        # Final sort: total_late_minutes desc, emp_code asc
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    results = list(db.attendance_logs.aggregate(pipeline))

    return {"month": month, "items": results}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    validate_date_str(date_from, "from")
    validate_date_str(date_to, "to")

    if date_to < date_from:
        raise HTTPException(422, detail="'to' must not be before 'from'")

    # Check range doesn't exceed 92 days
    from_d = date_type.fromisoformat(date_from)
    to_d = date_type.fromisoformat(date_to)
    if (to_d - from_d).days > 91:  # 0..91 inclusive = 92 days
        raise HTTPException(422, detail="Range must not exceed 92 days")

    # Check department exists (has employees)
    dept_count = db.employees.count_documents({"department": department})
    if dept_count == 0:
        raise HTTPException(404, detail="Department not found (no employees)")

    # Get all employees in this department to compute headcount per day
    dept_employees = list(
        db.employees.find({"department": department}, {"joined_on": 1, "_id": 0})
    )

    # Build aggregation pipeline using $densify for gap filling
    pipeline = [
        # Match attendance logs in the date range
        {
            "$match": {
                "date": {"$gte": date_from, "$lte": date_to},
            }
        },
        # Lookup employee to filter by department
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp_info",
            }
        },
        {"$unwind": "$emp_info"},
        {"$match": {"emp_info.department": department}},
        # Convert date string to actual date for grouping and densify
        {
            "$addFields": {
                "date_obj": {
                    "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                }
            }
        },
        # Group by date
        {
            "$group": {
                "_id": "$date_obj",
                "date_str": {"$first": "$date"},
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        # Densify: fill gaps so we have one row per calendar day
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc),
                        # Upper bound is exclusive, so add 1 day to include to_d
                        datetime(to_d.year, to_d.month, to_d.day, tzinfo=timezone.utc)
                        + timedelta(days=1),
                    ],
                },
            }
        },
        # Fill nulls for densified rows
        {
            "$addFields": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "date_str": {
                    "$ifNull": [
                        "$date_str",
                        {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                    ]
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]

    trend_data = list(db.attendance_logs.aggregate(pipeline))

    items = []
    rates = []  # Collect attendance_rate values for moving average

    for row in trend_data:
        d = row["_id"]
        if isinstance(d, datetime):
            day_date = d.date()
            date_str = row.get("date_str", d.strftime("%Y-%m-%d"))
        else:
            date_str = row.get("date_str", str(d))
            day_date = date_type.fromisoformat(date_str)

        # is_working_day: Mon-Fri
        is_working_day = day_date.weekday() < 5

        # headcount: employees with joined_on <= this day (R9)
        date_str_for_cmp = day_date.isoformat()
        headcount = sum(1 for e in dept_employees if e["joined_on"] <= date_str_for_cmp)

        present_count = row.get("present_count", 0)
        late_count = row.get("late_count", 0)

        # attendance_rate: only on working days with headcount > 0, otherwise null
        if is_working_day and headcount > 0:
            attendance_rate = round_half_up(present_count / headcount, 4)
        else:
            attendance_rate = None

        rates.append(attendance_rate)

        # moving_avg_7d: mean of non-null attendance_rate in window of current + up to 6 preceding
        # rows inside the requested range (rows before 'from' not considered, first rows use shorter window)
        window_start = max(0, len(rates) - 7)
        window = rates[window_start:]
        non_null_rates = [r for r in window if r is not None]

        if non_null_rates:
            moving_avg = round_half_up(sum(non_null_rates) / len(non_null_rates), 4)
        else:
            moving_avg = None

        items.append(
            {
                "date": day_date.isoformat(),
                "is_working_day": is_working_day,
                "headcount": headcount,
                "present_count": present_count,
                "late_count": late_count,
                "attendance_rate": attendance_rate,
                "moving_avg_7d": moving_avg,
            }
        )

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: Explain
# --------------------------------------------------------------------------- #
EXPLAIN_ENDPOINTS = {
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, alias="date_from"),
    date_to: Optional[str] = Query(None, alias="date_to"),
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint not in EXPLAIN_ENDPOINTS:
        raise HTTPException(422, detail=f"Unknown endpoint: {endpoint}")

    if endpoint == "attendance_list":
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
            q["status"] = status

        cmd = {
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        try:
            explain = db.command(cmd)
        except Exception:
            explain = (
                db.attendance_logs.find(q)
                .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
                .skip((page - 1) * page_size)
                .limit(page_size)
                .explain()
            )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, detail="emp_code is required")
        if not month:
            raise HTTPException(422, detail="month is required")

        first_day, last_day = get_month_date_range(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
            {
                "$addFields": {
                    "date_obj": {
                        "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                    },
                    "_half_day": {"$ifNull": ["$half_day", False]},
                    "_late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "_overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                }
            },
            {
                "$addFields": {
                    "_dow": {"$dayOfWeek": "$date_obj"},
                    "_is_weekday": {
                        "$and": [
                            {"$gte": [{"$dayOfWeek": "$date_obj"}, 2]},
                            {"$lte": [{"$dayOfWeek": "$date_obj"}, 6]},
                        ]
                    },
                    "_is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {"$and": ["$_is_present", "$_is_weekday"]},
                                {"$cond": ["$_half_day", 0.5, 1]},
                                0,
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                    },
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": ["$_late_minutes", 0]}, 1, 0]}
                    },
                    "total_late_minutes": {"$sum": "$_late_minutes"},
                    "total_overtime_minutes": {"$sum": "$_overtime_minutes"},
                }
            },
        ]
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        try:
            explain = db.command(cmd)
        except Exception:
            explain = db.command(
                "aggregate", "attendance_logs", pipeline=pipeline, explain=True,
            )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required")

        first_day, last_day = get_month_date_range(month)
        pipeline = [
            {"$match": {"date": {"$gte": first_day, "$lte": last_day}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "emp_info",
                }
            },
            {"$unwind": "$emp_info"},
        ]
        if department:
            pipeline.append({"$match": {"emp_info.department": department}})
        pipeline.extend([
            {
                "$addFields": {
                    "date_obj": {
                        "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                    },
                    "_half_day": {"$ifNull": ["$half_day", False]},
                    "_late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "_is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                }
            },
            {
                "$addFields": {
                    "_is_weekday": {
                        "$and": [
                            {"$gte": [{"$dayOfWeek": "$date_obj"}, 2]},
                            {"$lte": [{"$dayOfWeek": "$date_obj"}, 6]},
                        ]
                    },
                    "_has_work_hours": {
                        "$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$ne": [{"$type": "$work_hours"}, "null"]},
                            {"$ne": [{"$type": "$work_hours"}, "missing"]},
                        ]
                    },
                }
            },
            {
                "$group": {
                    "_id": "$emp_info.department",
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {"$and": ["$_is_present", "$_is_weekday"]},
                                {"$cond": ["$_half_day", 0.5, 1]},
                                0,
                            ]
                        }
                    },
                    "avg_work_hours_sum": {
                        "$sum": {"$cond": ["$_has_work_hours", "$work_hours", 0]}
                    },
                    "avg_work_hours_count": {
                        "$sum": {"$cond": ["$_has_work_hours", 1, 0]}
                    },
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": ["$_late_minutes", 0]}, 1, 0]}
                    },
                    "total_late_minutes": {"$sum": "$_late_minutes"},
                    "leave_count": {
                        "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                    },
                    "on_duty_count": {
                        "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                    },
                }
            },
            {"$sort": {"_id": 1}},
        ])
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        try:
            explain = db.command(cmd)
        except Exception:
            explain = db.command(
                "aggregate", "attendance_logs", pipeline=pipeline, explain=True,
            )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required")

        first_day, last_day = get_month_date_range(month)
        pipeline = [
            {"$match": {"date": {"$gte": first_day, "$lte": last_day}}},
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp_info",
                }
            },
            {"$unwind": "$emp_info"},
        ]
        if department:
            pipeline.append({"$match": {"emp_info.department": department}})

        pipeline.extend([
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {"$rank": {}},
                    },
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp_info.name",
                    "department": "$emp_info.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ])

        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        try:
            explain = db.command(cmd)
        except Exception:
            explain = db.command(
                "aggregate", "attendance_logs", pipeline=pipeline, explain=True,
            )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(422, detail="department is required")
        if not from_date or not to_date:
            raise HTTPException(422, detail="from and to are required")

        from_d = date_type.fromisoformat(from_date)
        to_d = date_type.fromisoformat(to_date)

        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "emp_info",
                }
            },
            {"$unwind": "$emp_info"},
            {"$match": {"emp_info.department": department}},
            {
                "$group": {
                    "_id": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$densify": {
                    "field": "_id",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [
                            datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc),
                            datetime(to_d.year, to_d.month, to_d.day, tzinfo=timezone.utc) + timedelta(days=1),
                        ],
                    },
                }
            },
        ]

        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        try:
            explain = db.command(cmd)
        except Exception:
            explain = db.command(
                "aggregate", "attendance_logs", pipeline=pipeline, explain=True,
            )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }
