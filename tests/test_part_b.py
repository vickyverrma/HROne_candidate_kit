import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from app.main import app, db, client, dt_to_epoch_ms, epoch_ms_to_dt


def reset_test_data():
    db.employees.delete_many({"emp_code": {"$regex": "^EMP88"}})
    db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP88"}})


def test_punch_out_and_concurrency():
    with TestClient(app) as test_client:
        reset_test_data()

        # Create test employee
        res_emp = test_client.post("/employees", json={
            "emp_code": "EMP8801",
            "name": "PunchOut User",
            "email": "punchout@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01"
        })
        assert res_emp.status_code == 201

        # Punch in at 09:28 IST on 2026-07-06 (03:58 UTC = 1783310280000 ms)
        t_in_ms = 1783310280000
        res_in = test_client.post("/attendance/punch-in", json={
            "emp_code": "EMP8801",
            "punched_at": t_in_ms,
            "status": "PRESENT"
        })
        assert res_in.status_code == 201

        # Unknown employee -> 404
        assert test_client.post("/attendance/punch-out", json={"emp_code": "EMP9999"}).status_code == 404

        # Punched at <= punch in -> 422
        res_le = test_client.post("/attendance/punch-out", json={
            "emp_code": "EMP8801",
            "punched_at": t_in_ms
        })
        assert res_le.status_code == 422

        # Punched at > 24 hours -> 422
        res_gt24 = test_client.post("/attendance/punch-out", json={
            "emp_code": "EMP8801",
            "punched_at": t_in_ms + (25 * 3600 * 1000)
        })
        assert res_gt24.status_code == 422

        # Valid punch out: 18:35 IST on 2026-07-06 (13:05 UTC = 1783343100000 ms)
        # 9 hours 7 minutes = 9.12 work hours, overtime 5 min (< 30 -> 0), half_day False
        t_out_ms = 1783343100000
        res_out = test_client.post("/attendance/punch-out", json={
            "emp_code": "EMP8801",
            "punched_at": t_out_ms
        })
        assert res_out.status_code == 200
        out_data = res_out.json()
        assert out_data["work_hours"] == 9.12
        assert out_data["overtime_minutes"] == 0
        assert out_data["half_day"] is False
        assert out_data["punch_out"] == t_out_ms

        # Second punch out for already closed record -> 409
        res_out_dup = test_client.post("/attendance/punch-out", json={
            "emp_code": "EMP8801",
            "punched_at": t_out_ms + 1000
        })
        assert res_out_dup.status_code == 409

        reset_test_data()


def test_regularization():
    with TestClient(app) as test_client:
        reset_test_data()

        # Create test employee
        test_client.post("/employees", json={
            "emp_code": "EMP8802",
            "name": "Reg User",
            "email": "reg@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01"
        })

        # Punch in late at 10:05 IST on 2026-07-06 (04:35 UTC = 1783312500000 ms)
        t_in_ms = 1783312500000
        test_client.post("/attendance/punch-in", json={
            "emp_code": "EMP8802",
            "punched_at": t_in_ms
        })
        # Punch out at 19:10 IST (13:40 UTC = 1783345200000 ms)
        t_out_ms = 1783345200000
        test_client.post("/attendance/punch-out", json={
            "emp_code": "EMP8802",
            "punched_at": t_out_ms
        })

        # Test request that changes nothing -> 422
        res_no_change = test_client.patch("/attendance/EMP8802/2026-07-06", json={
            "reason": "Accidental submit without change",
            "regularized_by": "manager"
        })
        assert res_no_change.status_code == 422

        # Test regularization: change punch-in from 10:05 IST to 09:28 IST (1783310280000 ms)
        t_new_in_ms = 1783310280000
        res_reg = test_client.patch("/attendance/EMP8802/2026-07-06", json={
            "punch_in": t_new_in_ms,
            "reason": "Fingerprint scanner malfunction",
            "regularized_by": "hr.admin"
        })
        assert res_reg.status_code == 200
        reg_data = res_reg.json()
        assert reg_data["punch_in"] == t_new_in_ms
        assert reg_data["late_minutes"] == 0
        assert len(reg_data["history"]) == 1
        h1 = reg_data["history"][0]
        assert h1["by"] == "hr.admin"
        assert h1["reason"] == "Fingerprint scanner malfunction"
        assert "punch_in" in h1["changes"]
        assert h1["changes"]["punch_in"]["from"] == t_in_ms
        assert h1["changes"]["punch_in"]["to"] == t_new_in_ms
        assert "late_minutes" in h1["changes"]
        assert h1["changes"]["late_minutes"]["from"] == 35
        assert h1["changes"]["late_minutes"]["to"] == 0

        # Change to LEAVE with punch times -> 422
        res_leave_bad = test_client.patch("/attendance/EMP8802/2026-07-06", json={
            "status": "LEAVE",
            "punch_in": t_new_in_ms,
            "reason": "Tried to leave with punches",
            "regularized_by": "hr.admin"
        })
        assert res_leave_bad.status_code == 422

        # Change to LEAVE properly -> 200, clears punches, derived fields null/0
        res_leave = test_client.patch("/attendance/EMP8802/2026-07-06", json={
            "status": "LEAVE",
            "reason": "Approved retroactive leave",
            "regularized_by": "hr.admin"
        })
        assert res_leave.status_code == 200
        leave_data = res_leave.json()
        assert leave_data["status"] == "LEAVE"
        assert leave_data["punch_in"] is None
        assert leave_data["punch_out"] is None
        assert leave_data["work_hours"] is None
        assert leave_data["late_minutes"] == 0
        assert len(leave_data["history"]) == 2

        reset_test_data()


def test_analytics_and_explain():
    with TestClient(app) as test_client:
        reset_test_data()

        # Seed sample data using sample_seed logic
        from sample_seed import here, opts
        from bson import json_util
        for name in ("employees", "attendance_logs"):
            docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
            db[name].delete_many({})
            db[name].insert_many(docs)

        # 1. Test employee monthly analytics
        res_m = test_client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        assert res_m.status_code == 200
        m_data = res_m.json()
        assert m_data["emp_code"] == "EMP0001"
        assert m_data["month"] == "2026-07"
        assert m_data["working_days"] == 23 # July 2026 has 23 working days
        assert m_data["present_days"] >= 1.0

        # Unknown employee monthly -> 404
        assert test_client.get("/analytics/employees/EMP9999/monthly?month=2026-07").status_code == 404

        # 2. Test department summary
        res_d = test_client.get("/analytics/departments/summary?month=2026-07")
        assert res_d.status_code == 200
        d_data = res_d.json()
        assert d_data["month"] == "2026-07"
        depts = {item["department"]: item for item in d_data["items"]}
        assert "Engineering" in depts
        assert "Sales" in depts
        assert "Support" in depts
        assert depts["Engineering"]["headcount"] == 2
        assert depts["Engineering"]["avg_work_hours"] == 9.04

        # Filter by department
        res_d_eng = test_client.get("/analytics/departments/summary?month=2026-07&department=Engineering")
        assert res_d_eng.status_code == 200
        d_eng_data = res_d_eng.json()
        assert len(d_eng_data["items"]) == 1
        assert d_eng_data["items"][0]["department"] == "Engineering"

        # 3. Test late leaderboard
        res_lb = test_client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
        assert res_lb.status_code == 200
        lb_data = res_lb.json()
        assert lb_data["month"] == "2026-07"
        assert len(lb_data["items"]) >= 1
        assert lb_data["items"][0]["rank"] == 1
        assert lb_data["items"][0]["emp_code"] == "EMP0003"
        assert lb_data["items"][0]["total_late_minutes"] == 35

        # 4. Test department trend
        res_t = test_client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-12")
        assert res_t.status_code == 200
        t_data = res_t.json()
        assert t_data["department"] == "Engineering"
        # 7 days exactly (from July 6 to July 12)
        assert len(t_data["items"]) == 7
        assert t_data["items"][0]["date"] == "2026-07-06"
        assert t_data["items"][6]["date"] == "2026-07-12"
        assert t_data["items"][0]["moving_avg_7d"] is not None

        # Unknown department trend -> 404
        assert test_client.get("/analytics/departments/NonExistent/trend?from=2026-07-06&to=2026-07-12").status_code == 404

        # 5. Test Explain Endpoints
        for ep, params in [
            ("attendance_list", {"emp_code": "EMP0001"}),
            ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
            ("department_summary", {"month": "2026-07", "department": "Engineering"}),
            ("late_leaderboard", {"month": "2026-07"}),
            ("department_trend", {"department": "Engineering", "from": "2026-07-06", "to": "2026-07-12"}),
        ]:
            res_exp = test_client.get(f"/admin/explain/{ep}", params=params)
            assert res_exp.status_code == 200, f"Explain failed for {ep}: {res_exp.text}"
            exp_data = res_exp.json()
            assert exp_data["endpoint"] == ep
            assert "collection" in exp_data
            assert "explain" in exp_data

            # Check that explain output proves index usage (IXSCAN)
            explain_str = str(exp_data["explain"])
            assert "IXSCAN" in explain_str or "IndexScan" in explain_str, f"No index scan found in explain for {ep}"
            assert "COLLSCAN" not in explain_str, f"Unexpected COLLSCAN in explain for {ep}"
