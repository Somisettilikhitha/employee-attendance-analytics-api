"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional


from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    # Unique employee code for each employee
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uniq_employee_emp_code",
    )
    # Prevent duplicate attendance records for an employee on the same date
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uniq_emp_code_date",
    )
        # Employee query indexes
    db.employees.create_index(
        [("department", 1), ("joined_on", 1)],
        name="department_joined_on_idx",
    )
    db.employees.create_index(
        [("joined_on", 1)],
        name="joined_on_idx",
    )

    # Attendance query indexes
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", -1)],
        name="emp_code_date_idx",
    )
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="date_emp_code_idx",
    )
    db.attendance_logs.create_index(
        [("date", 1), ("late_minutes", -1)],
        name="date_late_minutes_idx",
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _as_ist(value: datetime) -> datetime:
    """Convert a datetime value to IST and truncate microseconds. """
    if value.tzinfo is None:
        # MongoDB naive datetimes are treated as UTC.
        value = value.replace(tzinfo=timezone.utc)

    return value.astimezone(IST).replace(microsecond=0)


def _shift_start_datetime(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> datetime:
    """Find the correct shift-start instant, including overnight shifts."""
    punch_in = _as_ist(punch_in)

    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    shift_date = punch_in.date()

    # For an overnight shift, a punch-in before shift_end
    # belongs to the shift that started on the previous day.
    is_overnight = (end_h, end_m) <= (start_h, start_m)
    punch_time = (punch_in.hour, punch_in.minute)
    shift_end_time = (end_h, end_m)

    if is_overnight and punch_time < shift_end_time:
        shift_date -= timedelta(days=1)

    return datetime(
        shift_date.year,
        shift_date.month,
        shift_date.day,
        start_h,
        start_m,
        tzinfo=IST,
    )

def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> int:
    """Apply the 10-minute grace period and return whole minutes late."""
    punch_in = _as_ist(punch_in)
    shift_start_dt = _shift_start_datetime(
        punch_in,
        shift_start,
        shift_end,
    )

    elapsed_seconds = (punch_in - shift_start_dt).total_seconds()

    # Late only when strictly more than 10 minutes after shift start.
    if elapsed_seconds <= 10 * 60:
        return 0

    return int(elapsed_seconds // 60)


def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime,
) -> float:
    """Calculate duration in hours, rounded half-up to two decimals."""
    punch_in = _as_ist(punch_in)
    punch_out = _as_ist(punch_out)

    seconds = Decimal(
        str((punch_out - punch_in).total_seconds())
    )
    hours = seconds / Decimal("3600")

    return float(
        hours.quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )


def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    date_str: str,
    shift_start: str,
) -> int:
    """Return whole overtime minutes if excess is at least 30 minutes."""
    punch_out = _as_ist(punch_out)

    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    shift_date = datetime.fromisoformat(date_str).date()

    shift_end_dt = datetime(
        shift_date.year,
        shift_date.month,
        shift_date.day,
        end_h,
        end_m,
        tzinfo=IST,
    )

    # Shift ends the next day when shift_end <= shift_start.
    if (end_h, end_m) <= (start_h, start_m):
        shift_end_dt += timedelta(days=1)

    excess_seconds = (punch_out - shift_end_dt).total_seconds()
    excess_minutes = int(excess_seconds // 60)

    return excess_minutes if excess_minutes >= 30 else 0

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, value):
        if value is None:
            return value

        if type(value) is not int:
            raise ValueError("punched_at must be an integer epoch timestamp in milliseconds")

        if not 100000000000 <= value <= 4102444800000:
            raise ValueError("punched_at is outside the allowed epoch-millisecond range")

        return value



class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, value):
        if value is None:
            return value

        if type(value) is not int:
            raise ValueError("punched_at must be an integer epoch timestamp in milliseconds")

        if not 100000000000 <= value <= 4102444800000:
            raise ValueError("punched_at must be a valid epoch timestamp in milliseconds")

        return value


class RegularizeRequest(BaseModel):
    status: Optional[Literal[
        "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
    ]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: Optional[str] = None
    regularized_by: Optional[str] = None

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_timestamps(cls, value):
        if value is None:
            return value
        if type(value) is not int:
            raise ValueError("Timestamp must be integer epoch milliseconds")
        if not 100000000000 <= value <= 4102444800000:
            raise ValueError("Timestamp is outside the allowed range")
        return value

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value):
        if value is not None:
            value = value.strip()
            if not 5 <= len(value) <= 200:
                raise ValueError("reason must contain 5 to 200 characters")
        return value

    @field_validator("regularized_by")
    @classmethod
    def validate_regularized_by(cls, value):
        if value is not None:
            value = value.strip()
            if not 1 <= len(value) <= 50:
                raise ValueError("regularized_by must contain 1 to 50 characters")
        return value


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="MongoDB unavailable",
        )




@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(status_code=409, detail="emp_code already exists")

    doc = body.model_dump()
    doc["created_at"] = datetime.now()

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):

    # Validate pagination
    if page < 1:
        raise HTTPException(422, "page must be at least 1")

    if page_size < 1 or page_size > 100:
        raise HTTPException(422, "page_size must be between 1 and 100")

    # Build the filter
    q = {}
    if department:
        q["department"] = department

    # Calculate how many records to skip
    skip = (page - 1) * page_size

    # Count only employees matching the filter
    total = db.employees.count_documents(q)

    # Fetch the requested page, sorted by employee code
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size))

    # Convert created_at to epoch milliseconds for the API
    for item in items:
        created_at = item.get("created_at")
        if created_at is not None:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            item["created_at"] = int(created_at.timestamp() * 1000)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    # 1. Verify that the employee exists.
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(404, "employee not found")

    # 2. Get the punch-in timestamp in IST.
    if body.punched_at is not None:
        ts = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc).astimezone(IST)
    else:
        ts = datetime.now(IST)

    # 3. Truncate microseconds to whole seconds.
    ts = ts.replace(microsecond=0)

    # 4. Determine the attendance date in IST.
    d = ts.date().isoformat()

    # 5. Reject duplicate punch-in for the same employee and date.
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")

    # 6. Build the attendance record.
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts.astimezone(timezone.utc).replace(tzinfo=None),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    # 7. Save the attendance record safely.
    try:
        res = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date",
        )

    # 8. Prepare the API response.
    doc["id"] = str(res.inserted_id)
    doc.pop("_id", None)

    # Return timestamps as epoch milliseconds.
    punch_in_value = doc.get("punch_in")
    if punch_in_value is not None:
        if punch_in_value.tzinfo is None:
            punch_in_value = punch_in_value.replace(
                tzinfo=timezone.utc
            )
        doc["punch_in"] = int(punch_in_value.timestamp() * 1000)

    return doc


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    # 1. Verify that the employee exists.
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    # 2. Get the requested punch-out time in IST, truncated to seconds.
    if body.punched_at is not None:
        punched_at = datetime.fromtimestamp(
            body.punched_at / 1000, tz=timezone.utc
        ).astimezone(IST)
    else:
        punched_at = datetime.now(IST)

    punched_at = punched_at.replace(microsecond=0)

    # MongoDB stores aware datetimes as UTC and normally reads them back
    # as naive UTC datetimes.
    punched_at_utc = punched_at.astimezone(timezone.utc).replace(tzinfo=None)

    # 3. Find the most recent attendance record whose punch-in
    # occurred at or before this punch-out time.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
        },
        sort=[("punch_in", -1)],
    )

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="no eligible punch-in found",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="attendance record already punched out",
        )

    # 4. Convert punch-in to IST for calculations.
    punch_in_ist = _as_ist(record["punch_in"])

    if punched_at <= punch_in_ist:
        raise HTTPException(
            status_code=422,
            detail="punch-out must be after punch-in",
        )

    if punched_at > punch_in_ist + timedelta(hours=24):
        raise HTTPException(
            status_code=422,
            detail="punch-out cannot be more than 24 hours after punch-in",
        )

    # 5. Calculate derived attendance fields.
    work_hours = compute_work_hours(punch_in_ist, punched_at)
    overtime_minutes = compute_overtime(
        punched_at,
        emp["shift_end"],
        record["date"],
        emp["shift_start"],
    )
    half_day = work_hours < 4.50

    # 6. Atomically close the record. Only one simultaneous request
    # can update a record that still has no punch-out.
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punched_at.astimezone(timezone.utc).replace(tzinfo=None),
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="attendance record already punched out",
        )

    # 7. Return the record without MongoDB's internal _id or a custom id.
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    updated.pop("_id", None)

    # API timestamps must be epoch milliseconds.
    for field in ("punch_in", "punch_out"):
        value = updated.get(field)
        if value is not None:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            updated[field] = int(value.timestamp() * 1000)

    return updated



@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    # Validate pagination
    if page < 1:
        raise HTTPException(422, "page must be at least 1")

    if page_size < 1 or page_size > 100:
        raise HTTPException(422, "page_size must be between 1 and 100")

    # Validate dates
    for date_value in (date_from, date_to):
        if date_value is not None:
            try:
                parsed = datetime.strptime(date_value, "%Y-%m-%d")
                if parsed.strftime("%Y-%m-%d") != date_value:
                    raise ValueError
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail="Dates must use YYYY-MM-DD format",
                )

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must not be after date_to",
        )

    # Validate status
    allowed_statuses = {
        "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
    }
    if status is not None and status not in allowed_statuses:
        raise HTTPException(422, "Invalid attendance status")

    # Build MongoDB filter
    query = {}

    if emp_code:
        query["emp_code"] = emp_code

    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to

    if status:
        query["status"] = status

    # Count matching records before pagination
    total = db.attendance_logs.count_documents(query)

    # Sort and paginate in MongoDB
    skip = (page - 1) * page_size
    items = list(
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    # Convert API timestamps to epoch milliseconds
    timestamp_fields = ("punch_in", "punch_out", "created_at")

    for item in items:
        for field in timestamp_fields:
            value = item.get(field)

            if value is not None:
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)

                item[field] = int(value.timestamp() * 1000)

        # Convert timestamps inside regularization history
        for entry in item.get("history", []):
            if entry.get("at") is not None:
                value = entry["at"]
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                entry["at"] = int(value.timestamp() * 1000)

            for change in entry.get("changes", {}).values():
                for field in ("from", "to"):
                    value = change.get(field)
                    if isinstance(value, datetime):
                        if value.tzinfo is None:
                            value = value.replace(tzinfo=timezone.utc)
                        change[field] = int(value.timestamp() * 1000)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
):
    # 1. Validate the attendance date.
    try:
        parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
        if parsed_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "date must use YYYY-MM-DD format")

    # 2. Validate required audit fields.
    if not body.reason or not body.regularized_by:
        raise HTTPException(
            422,
            "reason and regularized_by are required",
        )

    # 3. Find the employee and attendance record.
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date}
    )
    if record is None:
        raise HTTPException(404, "attendance record not found")

    old_record = record.copy()
    old_history = record.get("history", [])
    original_history_length = len(old_history)

    # 4. Build the proposed final state.
    new_status = (
        body.status if body.status is not None
        else record["status"]
    )

    new_punch_in = record.get("punch_in")
    new_punch_out = record.get("punch_out")

    if "punch_in" in body.model_fields_set:
        new_punch_in = (
            datetime.fromtimestamp(
                body.punch_in / 1000, tz=timezone.utc
            ).replace(microsecond=0)
            if body.punch_in is not None else None
        )

    if "punch_out" in body.model_fields_set:
        new_punch_out = (
            datetime.fromtimestamp(
                body.punch_out / 1000, tz=timezone.utc
            ).replace(microsecond=0)
            if body.punch_out is not None else None
        )

    # 5. Validate status and punch-time combinations.
    present_statuses = {"PRESENT", "WFH", "ON_DUTY"}

    if new_status in {"ABSENT", "LEAVE"}:
        if (
            "punch_in" in body.model_fields_set
            and body.punch_in is not None
        ) or (
            "punch_out" in body.model_fields_set
            and body.punch_out is not None
        ):
            raise HTTPException(
                422,
                "ABSENT or LEAVE cannot include punch times",
            )
        new_punch_in = None
        new_punch_out = None

    elif new_status in present_statuses:
        if new_punch_in is None:
            raise HTTPException(
                422,
                "presence status requires punch_in",
            )

        punch_in_ist = _as_ist(new_punch_in)

        # R1: punch-in must belong to the record's attendance date.
        start_h, start_m = map(
            int, emp["shift_start"].split(":")
        )
        end_h, end_m = map(
            int, emp["shift_end"].split(":")
        )

        expected_date = punch_in_ist.date()
        if (
            (end_h, end_m) <= (start_h, start_m)
            and (punch_in_ist.hour, punch_in_ist.minute)
                < (end_h, end_m)
        ):
            expected_date -= timedelta(days=1)

        if expected_date.isoformat() != date:
            # Validate the attendance date when punch_in is supplied
            # or when the status is explicitly changed.
            if (
                "punch_in" in body.model_fields_set
                or "status" in body.model_fields_set
            ):
                raise HTTPException(
                    422,
                    "punch_in must belong to the attendance date",
                )

        if new_punch_out is not None:
            punch_out_ist = _as_ist(new_punch_out)

            if punch_out_ist <= punch_in_ist:
                raise HTTPException(
                    422,
                    "punch_out must be after punch_in",
                )

            if punch_out_ist > punch_in_ist + timedelta(hours=24):
                raise HTTPException(
                    422,
                    "punch_out cannot be more than 24 hours after punch_in",
                )

    # 6. Calculate changes to the fields supplied or affected.
    changes = {}

    def add_change(field, old_value, new_value):
        def normalize(value):
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                return int(value.timestamp() * 1000)
            return value
        old_compare = normalize(old_value)
        new_compare = normalize(new_value)

        if old_compare != new_compare:
                changes[field] = {
                    "from": old_value,
                    "to": new_value,
                }

    add_change("status", record.get("status"), new_status)
    add_change("punch_in", record.get("punch_in"), new_punch_in)
    add_change("punch_out", record.get("punch_out"), new_punch_out)

    # Derived values are recalculated from the proposed final times.
    if new_punch_in is not None:
        new_late_minutes = compute_late_minutes(
            _as_ist(new_punch_in),
            emp["shift_start"],
            emp["shift_end"],
        )
    else:
        new_late_minutes = 0

    if new_punch_in is not None and new_punch_out is not None:
        new_work_hours = compute_work_hours(
            _as_ist(new_punch_in),
            _as_ist(new_punch_out),
        )
        new_overtime_minutes = compute_overtime(
            _as_ist(new_punch_out),
            emp["shift_end"],
            date,
            emp["shift_start"],
        )
        new_half_day = new_work_hours < 4.50
    else:
        new_work_hours = None
        new_overtime_minutes = 0
        new_half_day = False

    add_change(
        "late_minutes", record.get("late_minutes", 0),
        new_late_minutes,
    )
    add_change(
        "work_hours", record.get("work_hours"),
        new_work_hours,
    )
    add_change(
        "overtime_minutes", record.get("overtime_minutes", 0),
        new_overtime_minutes,
    )
    add_change(
        "half_day", record.get("half_day", False),
        new_half_day,
    )

    # A request that changes nothing is invalid.
    if not changes:
        raise HTTPException(422, "request makes no changes")

    # 7. Record exactly one audit entry.
    now = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {
            field: {
                "from": (
                    int(value["from"].timestamp() * 1000)
                    if field in {"punch_in", "punch_out"}
                    and isinstance(value["from"], datetime)
                    else value["from"]
                ),
                "to": (
                    int(value["to"].timestamp() * 1000)
                    if field in {"punch_in", "punch_out"}
                    and isinstance(value["to"], datetime)
                    else value["to"]
                ),
            }
            for field, value in changes.items()
        },
    }

    # 8. Atomically update the record while preserving history.
    update_filter = {
        "_id": record["_id"],
        "history": old_history,
    }

    for field in ("status", "punch_in", "punch_out"):
        if field in record:
            update_filter[field] = record[field]
        else:
            update_filter[field] = {"$exists": False}

    update_values = {
        "status": new_status,
        "late_minutes": new_late_minutes,
        "work_hours": new_work_hours,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day,
    }
    # Preserve the existing punch times unless they were changed
    # by the request or cleared by an ABSENT/LEAVE status.
    if (
        "punch_in" in body.model_fields_set
        or new_status in {"ABSENT", "LEAVE"}
    ):
        update_values["punch_in"] = (
            new_punch_in.astimezone(timezone.utc).replace(tzinfo=None)
            if new_punch_in is not None else None
        )

    if (
        "punch_out" in body.model_fields_set
        or new_status in {"ABSENT", "LEAVE"}
    ):
        update_values["punch_out"] = (
            new_punch_out.astimezone(timezone.utc).replace(tzinfo=None)
            if new_punch_out is not None else None
        )

    result = db.attendance_logs.update_one(
        update_filter,
        {
            "$set": update_values,
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            409,
            "attendance record changed concurrently; retry the request",
        )

    # 9. Return the updated record using the API timestamp format.
    updated = db.attendance_logs.find_one(
        {"_id": record["_id"]},
        {"_id": 0},
    )

    for field in ("punch_in", "punch_out"):
        value = updated.get(field)
        if value is not None:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            updated[field] = int(value.timestamp() * 1000)

    for entry in updated.get("history", []):
        value = entry.get("at")
        if value is not None:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            entry["at"] = int(value.timestamp() * 1000)

        changes = entry.get("changes", {})

        for change in changes.values():
            for field in ("from", "to"):
                value = change.get(field)
                if isinstance(value, datetime):
                    if value.tzinfo is None:
                        value = value.replace(tzinfo=timezone.utc)
                    change[field] = int(value.timestamp() * 1000)

    return updated

# ---------------------------------------------------------------------------- #
#   GET   /analytics/employees/{emp_code}/monthly
# ---------------------------------------------------------------------------- #

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    # 1. Validate the month format: YYYY-MM.
    import re
    import calendar
    from datetime import date
    from decimal import Decimal, ROUND_HALF_UP

    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    year, month_number = map(int, month.split("-"))
    month_start = date(year, month_number, 1)
    last_day = calendar.monthrange(year, month_number)[1]
    month_end = date(year, month_number, last_day)

    if month_number == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month_number + 1, 1)

    # 2. Verify that the employee exists.
    employee = db.employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0, "emp_code": 1, "joined_on": 1},
    )

    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    # 3. Aggregate the employee's attendance records in MongoDB.
    pipeline = [
        {"$match": {"emp_code": emp_code}},
        {
            "$addFields": {
                "_joined_date": {
                    "$dateFromString": {"dateString": "$joined_on"}
                }
            }
        },
        {
            "$addFields": {
                "_period_start": {
                    "$max": [
                        "$_joined_date",
                        {"$dateFromString": {
                            "dateString": month_start.isoformat()
                        }},
                    ]
                },
                "_period_end": {
                    "$dateFromString": {
                        "dateString": next_month.isoformat()
                    }
                },
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", month_start.isoformat()]},
                                    {"$lt": ["$date", next_month.isoformat()]},
                                ]
                            }
                        }
                    }
                ],
                "as": "_logs",
            }
        },
        {
            "$addFields": {
                "_working_days": {
                    "$size": {
                        "$filter": {
                            "input": {
                                "$map": {
                                    "input": {
                                        "$range": [
                                            0,
                                            {
                                                "$max": [
                                                    0,
                                                    {
                                                        "$dateDiff": {
                                                            "startDate": "$_period_start",
                                                            "endDate": "$_period_end",
                                                            "unit": "day",
                                                        }
                                                    },
                                                ]
                                            },
                                        ]
                                    },
                                    "as": "offset",
                                    "in": {
                                        "$dateAdd": {
                                            "startDate": "$_period_start",
                                            "unit": "day",
                                            "amount": "$$offset",
                                        }
                                    },
                                }
                            },
                            "as": "day",
                            "cond": {
                                "$and": [
                                    {"$gte": [{"$dayOfWeek": "$$day"}, 2]},
                                    {"$lte": [{"$dayOfWeek": "$$day"}, 6]},
                                ]
                            },
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "working_days": "$_working_days",
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$_logs",
                                    "as": "log",
                                    "cond": {
                                        "$and": [
                                            {
                                                "$in": [
                                                    "$$log.status",
                                                    ["PRESENT", "WFH", "ON_DUTY"],
                                                ]
                                            },
                                            {
                                                "$gte": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$$log.date"
                                                            }
                                                        }
                                                    },
                                                    2,
                                                ]
                                            },
                                            {
                                                "$lte": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$$log.date"
                                                            }
                                                        }
                                                    },
                                                    6,
                                                ]
                                            },
                                        ]
                                    },
                                }
                            },
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$eq": ["$$log.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                        }
                    }
                },
                "leave_days": {
                    "$size": {
                        "$filter": {
                            "input": "$_logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]},
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$_logs",
                            "as": "log",
                            "cond": {
                                "$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]
                            },
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$_logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.late_minutes", 0]},
                        }
                    }
                },
                "total_overtime_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$_logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.overtime_minutes", 0]},
                        }
                    }
                },
            }
        },
    ]

    results = list(db.employees.aggregate(pipeline))

    # 4. Round reported numbers using half-up rounding.
    result = results[0]

    def round_half_up(value, places=2):
        quantum = Decimal("1").scaleb(-places)
        return float(
            Decimal(str(value)).quantize(
                quantum, rounding=ROUND_HALF_UP
            )
        )

    working_days = result["working_days"]
    present_days = round_half_up(result["present_days"])

    attendance_pct = (
        round_half_up(present_days / working_days * 100)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": result["leave_days"],
        "late_count": result["late_count"],
        "total_late_minutes": result["total_late_minutes"],
        "total_overtime_minutes": result["total_overtime_minutes"],
        "attendance_pct": attendance_pct,
    }

# --------------------------------------------------------------------------- #
#   GET   /analytics/departments/summary
# --------------------------------------------------------------------------- #

@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    import re
    import calendar
    from datetime import date
    from decimal import Decimal, ROUND_HALF_UP

    # 1. Validate YYYY-MM.
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    year, month_number = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_number)[1]
    month_end = f"{year:04d}-{month_number:02d}-{last_day:02d}"
    month_start = f"{year:04d}-{month_number:02d}-01"

    # 2. Select employees who joined by the end of the month.
    employee_filter = {"joined_on": {"$lte": month_end}}

    if department is not None:
        employee_filter["department"] = department

    # 3. Join attendance records and calculate metrics in MongoDB.
    pipeline = [
        {"$match": employee_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "_weekday": {
                                "$dayOfWeek": {
                                    "$dateFromString": {
                                        "dateString": "$date"
                                    }
                                }
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "_present_value": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {
                                                "$in": [
                                                    "$status",
                                                    ["PRESENT", "WFH", "ON_DUTY"],
                                                ]
                                            },
                                            {"$gte": ["$_weekday", 2]},
                                            {"$lte": ["$_weekday", 6]},
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {"$eq": ["$half_day", True]},
                                            0.5,
                                            1,
                                        ]
                                    },
                                    0,
                                ]
                            }
                        }
                    },
                ],
                "as": "_logs",
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "headcount": {"$literal": 1},
                "present_days": {"$sum": "$_logs._present_value"},
                "work_hours_sum": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$_logs",
                                    "as": "log",
                                    "cond": {
                                        "$and": [
                                            {
                                                "$in": [
                                                    "$$log.status",
                                                    ["PRESENT", "WFH", "ON_DUTY"],
                                                ]
                                            },
                                            {
                                                "$ne": ["$$log.work_hours", None]},
                                        ]
                                    },
                                }
                            },
                            "as": "log",
                            "in": "$$log.work_hours",
                        }
                    }
                },
                "work_hours_count": {
    "$size": {
        "$filter": {
            "input": "$_logs",
            "as": "log",
            "cond": {
                "$and": [
                    {
                        "$in": [
                            "$$log.status",
                            ["PRESENT", "WFH", "ON_DUTY"],
                        ]
                    },
                    {"$ne": ["$$log.work_hours", None]},
                ]
            },
        }
    }
},

                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$_logs",
                            "as": "log",
                            "cond": {
                                "$gt": [
                                    {"$ifNull": ["$$log.late_minutes", 0]},
                                    0,
                                ]
                            },
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$_logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$gt": [
                                            {"$ifNull": ["$$log.late_minutes", 0]},
                                            0,
                                        ]
                                    },
                                    "$$log.late_minutes",
                                    0,
                                ]
                            },
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$_logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$_logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": "$headcount"},
                "present_days": {"$sum": "$present_days"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {
            "$addFields": {
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {
                            "$divide": [
                                "$work_hours_sum", 
                                "$work_hours_count",
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {"$sort": {"_id": 1}},
    ]

    results = list(db.employees.aggregate(pipeline))

    # 4. Apply the contract's half-up rounding.
    def round_half_up(value, places=2):
        quantum = Decimal("1").scaleb(-places)
        return float(
            Decimal(str(value)).quantize(
                quantum, rounding=ROUND_HALF_UP
            )
        )

    items = []

    for result in results:
        avg_hours = result.get("avg_work_hours")

        items.append({
            "department": result["_id"],
            "headcount": result["headcount"],
            "present_days": round_half_up(result["present_days"]),
            "avg_work_hours": (
                round_half_up(avg_hours) if avg_hours is not None else None
            ),
            "late_count": result["late_count"],
            "total_late_minutes": result["total_late_minutes"],
            "leave_count": result["leave_count"],
            "on_duty_count": result["on_duty_count"],
        })

    return {
        "month": month,
        "items": items,
    }
# --------------------------------------------------------------------------- #
#   GET   /analytics/leaderboard/late
# --------------------------------------------------------------------------- #


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = 10,
    department: Optional[str] = None,
):
    import re
    import calendar

    # 1. Validate month and limit.
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    if limit < 1 or limit > 50:
        raise HTTPException(
            status_code=422,
            detail="limit must be between 1 and 50",
        )

    year, month_number = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_number)[1]
    month_start = f"{year:04d}-{month_number:02d}-01"
    month_end = f"{year:04d}-{month_number:02d}-{last_day:02d}"

    # 2. Match attendance records for the selected month.
    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": month_start,
                    "$lte": month_end,
                },
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
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "_employee",
            }
        },
        {"$unwind": "$_employee"},
    ]

    # Filter by the employee's department, if provided.
    if department is not None:
        pipeline.append(
            {"$match": {"_employee.department": department}}
        )

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$_employee.name",
                "department": "$_employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
        # Rank using standard competition ranking.
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        # Apply the limit after ranking, including ties.
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
    ])

    items = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": items,
    }


def build_department_trend_pipeline(department, start_dt, end_dt):
    """Build the MongoDB aggregation pipeline for department trends."""
    pipeline = [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": "$department",
                "employees": {
                    "$push": {
                        "emp_code": "$emp_code",
                        "joined_on": "$joined_on",
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "employees": 1,
                "emp_codes": "$employees.emp_code",
                "dates": {"$literal": [start_dt, end_dt]},
            }
        },
        {"$unwind": "$dates"},
        {"$set": {"day": "$dates"}},
        {"$unset": "dates"},
        {"$sort": {"day": 1}},
        {
            "$densify": {
                "field": "day",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": "full",
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {
                        "$project": {
                            "_id": 0,
                            "emp_code": 1,
                            "joined_on": 1,
                        }
                    },
                ],
                "as": "day_employees",
            }
        },
        {
            "$set": {
                "date_string": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$day",
                        "timezone": "UTC",
                    }
                },
                "weekday": {"$isoDayOfWeek": "$day"},
            }
        },
        {
            "$set": {
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$day_employees",
                            "as": "employee",
                            "cond": {
                                "$lte": [
                                    "$$employee.joined_on",
                                    "$date_string",
                                ]
                            },
                        }
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "target_date": "$date_string",
                    "employee_codes": "$day_employees.emp_code",
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$target_date"]},
                                    {
                                        "$in": [
                                            "$emp_code",
                                            "$$employee_codes",
                                        ]
                                    },
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$set": {
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$in": [
                                            "$$log.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {"$eq": ["$$log.half_day", True]},
                                            0.5,
                                            1,
                                        ]
                                    },
                                    0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {
                                "$gt": [
                                    {"$ifNull": ["$$log.late_minutes", 0]},
                                    0,
                                ]
                            },
                        }
                    }
                },
                "is_working_day": {"$lte": ["$weekday", 5]},
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$date_string",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]
    return pipeline


# --------------------------------------------------------------------------- #
#  GET /analytics/departments/{department}/trend
# --------------------------------------------------------------------------- #


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    from datetime import date, datetime, timedelta, timezone
    from decimal import Decimal, ROUND_HALF_UP

    # 1. Validate dates.
    try:
        start = date.fromisoformat(date_from)
        end = date.fromisoformat(date_to)

        if start.isoformat() != date_from:
            raise ValueError
        if end.isoformat() != date_to:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD format",
        )

    if end < start:
        raise HTTPException(
            status_code=422,
            detail="'to' must not be before 'from'",
        )

    if (end - start).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )

    # 2. Verify that the department exists.
    if db.employees.find_one({"department": department}) is None:
        raise HTTPException(
            status_code=404,
            detail="department not found",
        )

    start_dt = datetime.combine(
        start, datetime.min.time(), tzinfo=timezone.utc
    )
    end_dt = datetime.combine(
        end, datetime.min.time(), tzinfo=timezone.utc
    )

    # 3. Build daily rows using MongoDB aggregation.
    pipeline = build_department_trend_pipeline(
        department, start_dt, end_dt
    )

    rows = list(db.employees.aggregate(pipeline))

    # 4. Apply half-up rounding to the API values.
    def round_half_up(value, places=4):
        if value is None:
            return None
        quantum = Decimal("1").scaleb(-places)
        return float(
            Decimal(str(value)).quantize(
                quantum,
                rounding=ROUND_HALF_UP,
            )
        )

    for row in rows:
        row["present_count"] = round_half_up(
            row["present_count"], 2
        )
        row["attendance_rate"] = round_half_up(
            row["attendance_rate"], 4
        )
        row["moving_avg_7d"] = round_half_up(
            row["moving_avg_7d"], 4
        )

    return {
        "department": department,
        "items": rows,
    }
# --------------------------------------------------------------------------- #
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    date_from_trend: Optional[str] = Query(None, alias="from"),
    date_to_trend: Optional[str] = Query(None, alias="to"),
    page: int = 1,
    page_size: int = 20,
):
    import re
    import calendar
    from datetime import date

    supported = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in supported:
        raise HTTPException(
            status_code=422,
            detail="Unsupported endpoint",
        )

    if page < 1 or not 1 <= page_size <= 100:
        raise HTTPException(
            status_code=422,
            detail="Invalid pagination parameters",
        )

    if not 1 <= limit <= 50:
        raise HTTPException(
            status_code=422,
            detail="limit must be between 1 and 50",
        )

    def validate_month(value):
        if value is None or not re.fullmatch(
            r"\d{4}-(0[1-9]|1[0-2])", value
        ):
            raise HTTPException(
                status_code=422,
                detail="A valid month in YYYY-MM format is required",
            )

        year, month_number = map(int, value.split("-"))
        last_day = calendar.monthrange(year, month_number)[1]

        return (
            f"{year:04d}-{month_number:02d}-01",
            f"{year:04d}-{month_number:02d}-{last_day:02d}",
        )

    def validate_date(value, parameter_name):
        if value is None:
            raise HTTPException(
                status_code=422,
                detail=f"{parameter_name} is required",
            )

        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"{parameter_name} must use YYYY-MM-DD format",
            )

        return value

    # 1. Explain the attendance list query.
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        query = {}

        if emp_code:
            query["emp_code"] = emp_code

        if date_from or date_to:
            query["date"] = {}

            if date_from:
                query["date"]["$gte"] = validate_date(
                    date_from, "date_from"
                )

            if date_to:
                query["date"]["$lte"] = validate_date(
                    date_to, "date_to"
                )

            if date_from and date_to and date_from > date_to:
                raise HTTPException(
                    status_code=422,
                    detail="date_from must not be after date_to",
                )

        if status is not None:
            if status not in {
                "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
            }:
                raise HTTPException(
                    status_code=422,
                    detail="Invalid attendance status",
                )
            query["status"] = status

        explain = db.command(
            "explain",
            {
                "find": collection,
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )

    # 2. Explain employee monthly aggregation.
    
    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(
                status_code=422,
                detail="emp_code is required",
            )

        month_start, month_end = validate_month(month)
        year, month_number = map(int, month.split("-"))

        import calendar
        from datetime import date

        if month_number == 12:
            next_month = date(year + 1, 1, 1)
        else:
            next_month = date(year, month_number + 1, 1)

        month_start = date(year, month_number, 1).isoformat()
        next_month = next_month.isoformat()

        collection = "employees"

        pipeline = [
            {"$match": {"emp_code": emp_code}},
            {
                "$addFields": {
                    "_joined_date": {
                        "$dateFromString": {
                            "dateString": "$joined_on"
                        }
                    }
                }
            },
            {
                "$addFields": {
                    "_period_start": {
                        "$max": [
                            "$_joined_date",
                            {
                                "$dateFromString": {
                                    "dateString": month_start
                                }
                            },
                        ]
                    },
                    "_period_end": {
                        "$dateFromString": {
                            "dateString": next_month
                        }
                    },
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"employee_code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {
                                            "$eq": [
                                                "$emp_code",
                                                "$$employee_code",
                                            ]
                                        },
                                        {
                                            "$gte": [
                                                "$date",
                                                month_start,
                                            ]
                                        },
                                        {
                                            "$lt": [
                                                "$date",
                                                next_month,
                                            ]
                                        },
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "_logs",
                }
            },
            {
                "$addFields": {
                    "_working_days": {
                        "$size": {
                            "$filter": {
                                "input": {
                                    "$map": {
                                        "input": {
                                            "$range": [
                                                0,
                                                {
                                                    "$max": [
                                                        0,
                                                        {
                                                            "$dateDiff": {
                                                                "startDate": "$_period_start",
                                                                "endDate": "$_period_end",
                                                                "unit": "day",
                                                            }
                                                        },
                                                    ]
                                                },
                                            ]
                                        },
                                        "as": "offset",
                                        "in": {
                                            "$dateAdd": {
                                                "startDate": "$_period_start",
                                                "unit": "day",
                                                "amount": "$$offset",
                                            }
                                        },
                                    }
                                },
                                "as": "day",
                                "cond": {
                                    "$and": [
                                        {
                                            "$gte": [
                                                {"$dayOfWeek": "$$day"},
                                                2,
                                            ]
                                        },
                                        {
                                            "$lte": [
                                                {"$dayOfWeek": "$$day"},
                                                6,
                                            ]
                                        },
                                    ]
                                },
                            }
                        }
                    }
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "emp_code": 1,
                    "working_days": "$_working_days",
                    "present_days": {
                        "$sum": {
                            "$map": {
                                "input": {
                                    "$filter": {
                                        "input": "$_logs",
                                        "as": "log",
                                        "cond": {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$$log.status",
                                                        [
                                                            "PRESENT",
                                                            "WFH",
                                                            "ON_DUTY",
                                                        ],
                                                    ]
                                                },
                                                {
                                                    "$gte": [
                                                        {
                                                            "$dayOfWeek": {
                                                                "$dateFromString": {
                                                                    "dateString": "$$log.date"
                                                                }
                                                            }
                                                        },
                                                        2,
                                                    ]
                                                },
                                                {
                                                    "$lte": [
                                                        {
                                                            "$dayOfWeek": {
                                                                "$dateFromString": {
                                                                    "dateString": "$$log.date"
                                                                }
                                                            }
                                                        },
                                                        6,
                                                    ]
                                                },
                                            ]
                                        },
                                    }
                                },
                                "as": "log",
                                "in": {
                                    "$cond": [
                                        {
                                            "$eq": [
                                                "$$log.half_day",
                                                True,
                                            ]
                                        },
                                        0.5,
                                        1,
                                    ]
                                },
                            }
                        }
                    },
                    "leave_days": {
                        "$size": {
                            "$filter": {
                                "input": "$_logs",
                                "as": "log",
                                "cond": {
                                    "$eq": ["$$log.status", "LEAVE"]
                                },
                            }
                        }
                    },
                    "late_count": {
                        "$size": {
                            "$filter": {
                                "input": "$_logs",
                                "as": "log",
                                "cond": {
                                    "$gt": [
                                        {
                                            "$ifNull": [
                                                "$$log.late_minutes",
                                                0,
                                            ]
                                        },
                                        0,
                                    ]
                                },
                            }
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {
                            "$map": {
                                "input": "$_logs",
                                "as": "log",
                                "in": {
                                    "$ifNull": [
                                        "$$log.late_minutes",
                                        0,
                                    ]
                                },
                            }
                        }
                    },
                    "total_overtime_minutes": {
                        "$sum": {
                            "$map": {
                                "input": "$_logs",
                                "as": "log",
                                "in": {
                                    "$ifNull": [
                                        "$$log.overtime_minutes",
                                        0,
                                    ]
                                },
                            }
                        }
                    },
                }
            },
        ]

        explain = db.command(
            "explain",
            {
                "aggregate": collection,
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    # 3. Explain department summary aggregation.
    elif endpoint == "department_summary":
        month_start, month_end = validate_month(month)
        collection = "employees"

        employee_filter = {"joined_on": {"$lte": month_end}}

        if department is not None:
            employee_filter["department"] = department

        pipeline = [
            {"$match": employee_filter},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$code"]},
                                        {"$gte": ["$date", month_start]},
                                        {"$lte": ["$date", month_end]},
                                    ]
                                }
                            }
                        },
                        {
                            "$addFields": {
                                "_weekday": {
                                    "$dayOfWeek": {
                                        "$dateFromString": {
                                            "dateString": "$date"
                                        }
                                    }
                                }
                            }
                        },
                        {
                            "$addFields": {
                                "_present_value": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                                {"$gte": ["$_weekday", 2]},
                                                {"$lte": ["$_weekday", 6]},
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            }
                        },
                    ],
                    "as": "_logs",
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": 1,
                    "headcount": {"$literal": 1},
                    "present_days": {"$sum": "$_logs._present_value"},
                    
                    "work_hours_sum": {
                        "$sum": {
                            "$map": {
                                "input": {
                                    "$filter": {
                                        "input": "$_logs",
                                        "as": "log",
                                        "cond": {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$$log.status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                                {
                                                    "$ne": [
                                                        "$$log.work_hours",
                                                        None,
                                                    ]
                                                },
                                            ]
                                        },
                                    }
                                },
                                "as": "log",
                                "in": "$$log.work_hours",
                            }
                        }
                    },
                    "work_hours_count": {
                        "$size": {
                            "$filter": {
                                "input": "$_logs",
                                "as": "log",
                                "cond": {
                                    "$and": [
                                        {
                                            "$in": [
                                                "$$log.status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$ne": [
                                                "$$log.work_hours",
                                                None,
                                            ]
                                        },
                                    ]
                                },
                            }
                        }
                    },
                    "late_count": {
                        "$size": {
                            "$filter": {
                                "input": "$_logs",
                                "as": "log",
                                "cond": {
                                    "$gt": [
                                        {"$ifNull": ["$$log.late_minutes", 0]},
                                        0,
                                    ]
                                },
                            }
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {
                            "$map": {
                                "input": "$_logs",
                                "as": "log",
                                "in": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                {"$ifNull": ["$$log.late_minutes", 0]},
                                                0,
                                            ]
                                        },
                                        "$$log.late_minutes",
                                        0,
                                    ]
                                },
                            }
                        }
                    },
                    "leave_count": {
                        "$size": {
                            "$filter": {
                                "input": "$_logs",
                                "as": "log",
                                "cond": {"$eq": ["$$log.status", "LEAVE"]},
                            }
                        }
                    },
                    "on_duty_count": {
                        "$size": {
                            "$filter": {
                                "input": "$_logs",
                                "as": "log",
                                "cond": {"$eq": ["$$log.status", "ON_DUTY"]},
                            }
                        }
                    },
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": "$headcount"},
                    "present_days": {"$sum": "$present_days"},
                    "work_hours_sum": {"$sum": "$work_hours_sum"},
                    "work_hours_count": {"$sum": "$work_hours_count"},
                    "late_count": {"$sum": "$late_count"},
                    "total_late_minutes": {"$sum": "$total_late_minutes"},
                    "leave_count": {"$sum": "$leave_count"},
                    "on_duty_count": {"$sum": "$on_duty_count"},
                }
            },
            {
                "$addFields": {
                    "avg_work_hours": {
                        "$cond": [
                            {"$gt": ["$work_hours_count", 0]},
                            {
                                "$round": [
                                    {
                                        "$divide": [
                                            "$work_hours_sum",
                                            "$work_hours_count",
                                        ]
                                    },
                                    2,
                                ]
                            },
                            0,
                        ]
                    }
                }
            },
            {"$sort": {"_id": 1}},
        ]

        explain = db.command(
            "explain",
            {
                "aggregate": collection,
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    # 4. Explain late leaderboard aggregation.
    elif endpoint == "late_leaderboard":
        month_start, month_end = validate_month(month)
        collection = "attendance_logs"

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": month_start, "$lte": month_end},
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
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
        ]

        if department is not None:
            pipeline.append(
                {"$match": {"employee.department": department}}
            )

        pipeline.extend([
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "emp_code": 1,
                }
            },
            {"$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "emp_code": 1,
                }
            },
        ])

        explain = db.command(
            "explain",
            {
                "aggregate": collection,
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    # 5. Explain department trend aggregation.
    else:
        if not department:
            raise HTTPException(
                status_code=422,
                detail="department is required",
            )

        start_value = validate_date(date_from_trend, "from")
        end_value = validate_date(date_to_trend, "to")

        start_date = date.fromisoformat(start_value)
        end_date = date.fromisoformat(end_value)

        if end_date < start_date:
            raise HTTPException(
                status_code=422,
                detail="'to' must not be before 'from'",
            )

        if (end_date - start_date).days + 1 > 92:
            raise HTTPException(
                status_code=422,
                detail="Date range cannot exceed 92 days",
            )

        collection = "employees"

        
        from datetime import datetime, timezone

        start_dt = datetime.combine(
            start_date, datetime.min.time(), tzinfo=timezone.utc
        )
        end_dt = datetime.combine(
            end_date, datetime.min.time(), tzinfo=timezone.utc
        )

        pipeline = build_department_trend_pipeline(
            department, start_dt, end_dt
        )

        explain = db.command(
            "explain",
            {
                "aggregate": collection,
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explain,
    }

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
