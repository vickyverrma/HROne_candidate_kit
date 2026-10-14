# Employee Attendance & Analytics API (HROne Engineering Assignment)

Production-grade FastAPI & MongoDB backend for employee attendance tracking, regularization, and multi-dimensional analytics.

## Project Structure
- `app/main.py`: Single-file FastAPI application containing all models, business logic (R1–R10), MongoDB aggregation pipelines, and startup index configurations.
- `requirements.txt`: Python dependencies (`fastapi`, `uvicorn`, `pymongo`, `pydantic`, `python-dotenv`).
- `REVIEW.md`: Detailed audit of 28 defects found in the starter code, symptoms/tests, and corresponding fixes.
- `DECISIONS.md`: Architecture and design decisions covering indexes, race-condition handling, competition ranking ties, zero-log headcounts, and scaling strategies.
- `sample_data/` & `sample_seed.py`: Sample datasets and Extended JSON database loader.
- `tests/`: Automated unit and integration test suites covering all business rules, edge cases, overnight shifts, aggregations, and query plans.

## Setup & Running

### 1. Environment & Dependencies
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configuration
Configure `MONGO_URI` and `MONGO_DB` via environment variables (or `.env` file):
```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```

### 3. Seed Sample Data (Optional)
```bash
python sample_seed.py
```

### 4. Start the Application
All database indexes are automatically created at startup via FastAPI's lifespan handler.
```bash
uvicorn app.main:app --port 8000
```

Interactive OpenAPI docs are available at `http://localhost:8000/docs`.

## Automated Testing
```bash
PYTHONPATH=. pytest tests/ -v
```
All 8 test suites pass, verifying CRUD operations, edge cases, window rankings, gap-filled trend generation, and index scan (`IXSCAN`) execution plans.
