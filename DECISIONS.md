# DECISIONS.md

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
- `employees`: Unique index on `emp_code` guarantees identity uniqueness. Indexes on `(department, joined_on)` and `joined_on` back department summary and trend headcount lookups.
- `attendance_logs`: Unique compound index on `(emp_code, date)` prevents duplicate punches. Compound index `(date, -1, emp_code, 1)` backs attendance pagination. Index `(date, late_minutes)` powers leaderboard ranking. Index `(emp_code, punch_in, -1)` backs punch-out lookups.
- *Rejected*: A wide covering index on `(emp_code, date, status, work_hours)`—write overhead on high-frequency punch events outweighed minor read gains.

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
Both requests validate inputs, compute identical attendance dates, and issue `insert_one()`. MongoDB's unique index on `(emp_code, date)` enforces atomicity: one insert acquires the write lock and succeeds (201 Created); the concurrent insert raises a `DuplicateKeyError`. Our code catches this exception and returns HTTP 409 Conflict.

3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
The leaderboard returns both tied employees, even if total rows exceed `limit`. Standard competition ranking (`$rank` in `$setWindowFields`) gives tied employees identical ranks (e.g. 1, 2, 2, 4). Filtering by `rank <= limit` occurs after ranking per contract rules, sorting tied rows by `emp_code` ascending.

4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
The aggregation pipeline originates on `employees` (matching `joined_on <= period_end`), grouping by department to establish baseline headcount before joining `attendance_logs` via `$lookup`. Employees with zero records produce an empty log array but still count towards `headcount`.

5. **One thing you would change** if this had to serve 100x the data.
At 100x data (~10M logs), dynamic aggregation over raw collections will saturate database CPU. I would introduce an asynchronous incremental rollup collection (updated via change streams or message queue workers) storing precomputed daily department counters and monthly employee summaries for instant O(1) reads.
