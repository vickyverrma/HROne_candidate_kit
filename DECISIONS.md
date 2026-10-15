# DECISIONS.md

1. **Indexes.**
I created unique indexes on `employees.emp_code` and `attendance_logs.(emp_code, date)` to enforce data integrity directly in the database. For queries, `attendance_logs.(date, -1, emp_code, 1)` satisfies paginated list sorts without in-memory sorting, `(date, late_minutes)` backs the monthly leaderboard, and `employees.(department, joined_on)` speeds up department headcount lookups.
*Rejected:* A wide covering index on `(emp_code, date, status, work_hours)`. Punch events are write-heavy; the extra index maintenance on every punch wasn't worth the marginal read savings.

2. **Punch-in race.**
When two simultaneous punch requests arrive for the same employee and day, both compute the same attendance date and attempt `insert_one()`. Because `(emp_code, date)` has a unique index, WiredTiger handles this atomically: one write commits (201 Created), while the concurrent write collides and raises a `DuplicateKeyError`. I catch that error and return HTTP 409 Conflict. No flaky check-then-insert logic.

3. **Ties.**
If employees tie on late minutes at the cutoff rank, the leaderboard returns all of them (even if total rows exceed `limit`). I used MongoDB's `$rank` window function for standard competition ranking (1, 2, 2, 4). The `limit` check runs after ranking per contract rules, and tied rows are broken alphabetically by `emp_code`.

4. **Headcount.**
The department aggregation starts from the `employees` collection (filtering `joined_on <= month_end`), grouping to get the baseline headcount before joining records with `$lookup`. If an employee has zero logs in that month, `$lookup` yields an empty list, but their employee record still counts toward department headcount.

5. **One thing you would change for 100x data.**
At 100x scale (~10 million logs), running dynamic multi-stage aggregations across raw collections will choke database CPU. I would introduce an asynchronous daily rollup collection (fed via MongoDB change streams or a background worker) that stores pre-aggregated daily department tallies and monthly employee summaries, turning analytics queries into simple O(1) point lookups.
