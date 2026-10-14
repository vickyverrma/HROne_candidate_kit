import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from app.main import (
    app,
    db,
    client,
    IST,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    get_attendance_date,
    dt_to_epoch_ms,
    epoch_ms_to_dt,
)


def test_compute_late_minutes():
    # Shift start: 09:30 IST on 2026-07-06
    # 09:30:00 IST -> 04:00:00 UTC
    # 09:40:00 IST -> 04:10:00 UTC (exactly 10 min, grace period -> 0)
    # 09:40:01 IST -> 04:10:01 UTC (strictly > 10 min -> 10)
    # 10:15:59 IST -> 04:45:59 UTC (strictly > 10 min -> 45)
    
    t_0930 = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(t_0930, "09:30", "2026-07-06") == 0

    t_0940_00 = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(t_0940_00, "09:30", "2026-07-06") == 0

    t_0940_01 = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
    assert compute_late_minutes(t_0940_01, "09:30", "2026-07-06") == 10

    t_0940_30 = datetime(2026, 7, 6, 4, 10, 30, tzinfo=timezone.utc)
    assert compute_late_minutes(t_0940_30, "09:30", "2026-07-06") == 10

    t_1015_59 = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)
    assert compute_late_minutes(t_1015_59, "09:30", "2026-07-06") == 45

    # Overnight shift: 22:00 start on 2026-07-06
    # Punch in 00:15:30 IST on 2026-07-07 (18:45:30 UTC on July 6)
    # Elapsed from 22:00 IST: 2 hours 15 min 30 sec = 135 minutes floor
    t_overnight = datetime(2026, 7, 6, 18, 45, 30, tzinfo=timezone.utc)
    assert compute_late_minutes(t_overnight, "22:00", "2026-07-06") == 135


def test_compute_work_hours_half_up():
    t1 = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    # 9630 seconds = 2.675 hours. Banker's rounding gives 2.67; Half-up gives 2.68
    t2 = t1 + timedelta(seconds=9630)
    assert compute_work_hours(t1, t2) == 2.68

    # 32820 seconds = 9.11666... hours -> 9.12
    t3 = t1 + timedelta(seconds=32820)
    assert compute_work_hours(t1, t3) == 9.12


def test_compute_overtime():
    # Shift end: 18:30 IST
    # Punch out at 18:35 IST (5 min -> < 30 -> 0)
    t_out_5min = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    assert compute_overtime(t_out_5min, "18:30", "2026-07-06", "09:30") == 0

    # Punch out at 18:59 IST (29 min -> < 30 -> 0)
    t_out_29min = datetime(2026, 7, 6, 13, 29, 0, tzinfo=timezone.utc)
    assert compute_overtime(t_out_29min, "18:30", "2026-07-06", "09:30") == 0

    # Punch out at 19:00 IST (30 min -> >= 30 -> 30)
    t_out_30min = datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc)
    assert compute_overtime(t_out_30min, "18:30", "2026-07-06", "09:30") == 30

    # Punch out at 19:10 IST (40 min -> >= 30 -> 40)
    t_out_40min = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
    assert compute_overtime(t_out_40min, "18:30", "2026-07-06", "09:30") == 40

    # Overnight shift: 22:00 - 06:00
    # Punch out 06:40 IST next day (2026-07-07 01:10 UTC)
    t_out_overnight = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)
    assert compute_overtime(t_out_overnight, "06:00", "2026-07-06", "22:00") == 40


def test_get_attendance_date():
    # Normal shift: 09:30 - 18:30
    t_normal = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc) # 09:28 IST
    assert get_attendance_date(t_normal, "09:30", "18:30") == "2026-07-06"

    # Overnight shift: 22:00 - 06:00
    # Punch in 21:55 IST on July 6 (16:25 UTC July 6)
    t_overnight_1 = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
    assert get_attendance_date(t_overnight_1, "22:00", "06:00") == "2026-07-06"

    # Punch in 00:30 IST on July 7 (19:00 UTC July 6) -> before shift_end 06:00 -> belongs to July 6
    t_overnight_2 = datetime(2026, 7, 6, 19, 0, 0, tzinfo=timezone.utc)
    assert get_attendance_date(t_overnight_2, "22:00", "06:00") == "2026-07-06"

    # Punch in 06:05 IST on July 7 (00:35 UTC July 7) -> after shift_end 06:00 -> belongs to July 7
    t_overnight_3 = datetime(2026, 7, 7, 0, 35, 0, tzinfo=timezone.utc)
    assert get_attendance_date(t_overnight_3, "22:00", "06:00") == "2026-07-07"


def test_endpoints():
    with TestClient(app) as test_client:
        # Test health
        res = test_client.get("/health")
        assert res.status_code == 200
        assert res.json() == {"status": "ok"}

        # Clean test database
        db.employees.delete_many({"emp_code": {"$regex": "^EMP99"}})
        db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP99"}})

        # Test employee validation
        res_invalid_code = test_client.post("/employees", json={
            "emp_code": "INVALID",
            "name": "Test",
            "email": "test@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })
        assert res_invalid_code.status_code == 422

        res_same_shifts = test_client.post("/employees", json={
            "emp_code": "EMP9901",
            "name": "Test",
            "email": "test@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "09:30",
            "joined_on": "2026-01-01"
        })
        assert res_same_shifts.status_code == 422

        # Create employee EMP9901
        res_create = test_client.post("/employees", json={
            "emp_code": "EMP9901",
            "name": "Test User",
            "email": "testuser@example.com",
            "department": "QA",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })
        assert res_create.status_code == 201
        data = res_create.json()
        assert "_id" not in data
        assert isinstance(data["created_at"], int)

        # Duplicate create EMP9901 -> 409
        res_dup = test_client.post("/employees", json={
            "emp_code": "EMP9901",
            "name": "Test User",
            "email": "testuser@example.com",
            "department": "QA",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })
        assert res_dup.status_code == 409

        # List employees pagination & total filter
        res_list = test_client.get("/employees?department=QA&page=1&page_size=10")
        assert res_list.status_code == 200
        list_data = res_list.json()
        assert list_data["total"] >= 1
        assert any(e["emp_code"] == "EMP9901" for e in list_data["items"])
        assert isinstance(list_data["items"][0]["created_at"], int)

        # Pagination validation
        assert test_client.get("/employees?page=0").status_code == 422
        assert test_client.get("/employees?page_size=200").status_code == 422

        # Punch in unknown employee -> 404
        assert test_client.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404

        # Punch in invalid status -> 422
        assert test_client.post("/attendance/punch-in", json={"emp_code": "EMP9901", "status": "ABSENT"}).status_code == 422

        # Punch in seconds-looking timestamp -> 422
        assert test_client.post("/attendance/punch-in", json={"emp_code": "EMP9901", "punched_at": 1783310280}).status_code == 422

        # Punch in valid (2026-07-06 09:28 IST = 1783310280000 ms)
        res_punch = test_client.post("/attendance/punch-in", json={
            "emp_code": "EMP9901",
            "punched_at": 1783310280000,
            "status": "PRESENT"
        })
        assert res_punch.status_code == 201
        p_data = res_punch.json()
        assert p_data["date"] == "2026-07-06"
        assert p_data["punch_in"] == 1783310280000
        assert p_data["late_minutes"] == 0
        assert "id" not in p_data
        assert "_id" not in p_data

        # Duplicate punch in for same day -> 409
        res_punch_dup = test_client.post("/attendance/punch-in", json={
            "emp_code": "EMP9901",
            "punched_at": 1783310280000
        })
        assert res_punch_dup.status_code == 409

        # List attendance date_from > date_to -> 422
        res_att_bad_date = test_client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
        assert res_att_bad_date.status_code == 422

        # List attendance
        res_att = test_client.get("/attendance?emp_code=EMP9901")
        assert res_att.status_code == 200
        att_data = res_att.json()
        assert att_data["total"] == 1
        assert att_data["items"][0]["emp_code"] == "EMP9901"
        assert att_data["items"][0]["punch_in"] == 1783310280000
        assert "id" not in att_data["items"][0]

        # Clean up
        db.employees.delete_many({"emp_code": {"$regex": "^EMP99"}})
        db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP99"}})
