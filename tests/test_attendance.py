
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

import pytest
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from app.main import (
    PRESENCE,
    STATUSES,
    IST,
    PunchInIn,
    attendance_date_for_punch,
    compute_late_minutes,
    compute_work_hours,
    half_up,
    overtime_for_employee,
    present_fraction_expr,
    punch_in,
)


def test_work_hours_4_hours_29_minutes():
    start = datetime(2026, 10, 15, 3, 30, tzinfo=timezone.utc)
    end = datetime(2026, 10, 15, 7, 59, tzinfo=timezone.utc)

    assert compute_work_hours(start, end) == 4.48


def test_work_hours_exactly_4_hours_30_minutes():
    start = datetime(2026, 10, 15, 3, 30, tzinfo=timezone.utc)
    end = datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)

    assert compute_work_hours(start, end) == 4.5


def test_half_up_rounding():
    assert half_up(4.485, 2) == 4.49


def test_late_grace_boundary():
    # 09:00 IST is 03:30 UTC.
    shift_start_utc = datetime(2026, 10, 15, 3, 30, tzinfo=timezone.utc)

    # Exactly 10 minutes late: not counted as late.
    assert compute_late_minutes(
        shift_start_utc + timedelta(minutes=10),
        "09:00",
        "18:00",
    ) == 0

    # 11 minutes late: report 11 minutes.
    assert compute_late_minutes(
        shift_start_utc + timedelta(minutes=11),
        "09:00",
        "18:00",
    ) == 11


def test_overnight_shift_late_arrival_after_midnight():
    # 06:05 IST is 00:35 UTC and is before the shift's 06:00 end
    # only if interpreted on the prior shift's timeline.
    punch_in = datetime(2026, 10, 15, 0, 35, tzinfo=timezone.utc)

    assert compute_late_minutes(punch_in, "22:00", "06:00") == 0


def test_overnight_overtime_after_shift_end():
    employee = {
        "shift_start": "22:00",
        "shift_end": "06:00",
    }

    # Attendance date is Oct 14; shift ends Oct 15 at 06:00 IST.
    punch_out = datetime(2026, 10, 15, 6, 45, tzinfo=IST)

    assert overtime_for_employee(
        punch_out, employee, "2026-10-14"
    ) == 45


def test_overnight_punch_in_uses_previous_attendance_date():
    # 01:00 IST on October 15 = 19:30 UTC on October 14.
    punch_in = datetime(2026, 10, 14, 19, 30, tzinfo=timezone.utc)

    assert attendance_date_for_punch(
        punch_in, "22:00", "06:00"
    ) == "2026-10-14"


def test_present_statuses():
    assert PRESENCE == {"PRESENT", "WFH", "ON_DUTY"}


def test_all_allowed_statuses():
    assert STATUSES == {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}


def test_present_fraction_expression():
    expression = present_fraction_expr()
    condition = expression["$cond"][0]

    assert condition["$in"][0] == "$status"
    assert set(condition["$in"][1]) == {"PRESENT", "WFH", "ON_DUTY"}

    half_day_condition = expression["$cond"][1]["$cond"]
    assert half_day_condition == [
        {"$eq": ["$half_day", True]},
        0.5,
        1.0,
    ]


def test_duplicate_punch_in_returns_409():
    employee = {
        "emp_code": "EMP9999",
        "shift_start": "09:00",
        "shift_end": "18:00",
    }

    # Mock the database object itself so this test never contacts MongoDB.
    fake_db = MagicMock()
    fake_db.employees.find_one.return_value = employee
    fake_db.attendance_logs.insert_one.side_effect = DuplicateKeyError(
        "duplicate attendance record"
    )

    body = PunchInIn(
        emp_code="EMP9999",
        punched_at=1792035000000,
        status="PRESENT",
    )

    with patch("app.main.db", fake_db):
        with pytest.raises(HTTPException) as exc:
            punch_in(body)

    assert exc.value.status_code == 409
    assert exc.value.detail == "Already punched in for this date"
    fake_db.attendance_logs.insert_one.assert_called_once()


def test_concurrent_punch_out_only_one_request_succeeds():
    employee = {
        "emp_code": "EMP9001",
        "shift_start": "09:00",
        "shift_end": "18:00",
    }

    punch_in_time = datetime(2026, 10, 15, 3, 30)
    punch_out_time = datetime(2026, 10, 15, 12, 0)

    attendance = {
        "emp_code": "EMP9001",
        "date": "2026-10-15",
        "status": "PRESENT",
        "punch_in": punch_in_time,
        "punch_out": None,
        "work_hours": None,
        "overtime_minutes": 0,
        "half_day": False,
    }

    completed_attendance = {
        **attendance,
        "punch_out": punch_out_time,
        "work_hours": 8.5,
    }

    fake_db = MagicMock()
    fake_db.employees.find_one.return_value = employee

    # First request: no completed record, then find the open record.
    # Second request: the record is already completed.
    fake_db.attendance_logs.find_one.side_effect = [
        None,
        attendance,
        completed_attendance,
    ]

    fake_db.attendance_logs.find_one_and_update.return_value = (
        completed_attendance
    )

    from app.main import PunchOutIn, punch_out

    body = PunchOutIn(
        emp_code="EMP9001",
        punched_at=1792065600000,
    )

    def request():
        try:
            with patch("app.main.db", fake_db):
                return punch_out(body)
        except HTTPException as exc:
            return exc.status_code

    first_result = request()
    second_result = request()

    assert isinstance(first_result, dict)
    assert second_result == 409
    assert fake_db.attendance_logs.find_one_and_update.call_count == 1