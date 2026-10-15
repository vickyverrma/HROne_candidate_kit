# Employee Attendance & Analytics API

FastAPI backend backed by MongoDB implementing attendance tracking, regularization workflows, and analytics aggregations per the HROne Engineering contract.

## Architecture & Code Layout
- `app/main.py`: Complete application code containing Pydantic schemas, business logic (R1–R10), MongoDB aggregation pipelines, and startup index configurations.
- `requirements.txt`: Python package requirements (`fastapi`, `uvicorn`, `pymongo`, `pydantic`, `python-dotenv`).
- `REVIEW.md`: Audit of defects found in the starter code, symptoms/reproductions, and their fixes.
- `DECISIONS.md`: Design rationale for database indexing, race-condition handling, competition ranking ties, zero-log headcounts, and scaling.
- `sample_data/` & `sample_seed.py`: Seed documents and loader script.
- `tests/`: Test suite covering business rules, edge cases, overnight shifts, aggregations, and execution plans.

## Local Setup

### 1. Virtual Environment & Dependencies
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Database & Environment Configuration
Set `MONGO_URI` and `MONGO_DB` via environment variables (or `.env`):
```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```

### 3. Load Sample Data
```bash
python sample_seed.py
```

### 4. Run the API Server
All database indexes are created automatically at startup.
```bash
uvicorn app.main:app --port 8000 --reload
```

Interactive documentation is available at `http://localhost:8000/docs`.

## Running Tests
```bash
PYTHONPATH=. pytest tests/ -v
```
All tests verify endpoint status codes, edge cases, race condition safety, gap-filled trend generation, and index scan (`IXSCAN`) execution plans with zero collection scans.
