# DECISIONS.md

1. **Indexes.**
I created unique indexes on `employees.emp_code` (enforces ID uniqueness; O(1) lookups) and `attendance_logs.(emp_code, date)` (guarantees at most one record per employee-day and race-safety; serves monthly employee queries). For listing, `attendance_logs.(date DESC, emp_code ASC)` backs `GET /attendance` pagination and sort order via an `IXSCAN`, avoiding in-memory sorts. `attendance_logs.date` supports date-range `$match` stages across analytics aggregations, while `attendance_logs.(emp_code, punch_in DESC)` rapidly finds open punch-in records during punch-out. I considered and rejected a compound `(status, date)` index on logs because `status` has low cardinality (5 values), offering negligible selectivity gain while adding write overhead.

2. **Punch-in race.**
When two identical punch-in requests arrive simultaneously, both calculate the IST attendance date and execute `db.attendance_logs.insert_one(...)`. MongoDB's unique index on `(emp_code, date)` atomically inserts the first document (returning HTTP 201). The second concurrent insert violates the unique constraint and raises `DuplicateKeyError`, which is caught and returned as HTTP 409. No check-then-act race window exists.

3. **Ties.**
The pipeline applies standard competition ranking via `$setWindowFields` using `$rank` ordered by `total_late_minutes` descending (e.g., ranks 1, 2, 2, 4). The subsequent filter matches `rank <= limit`. If employees tie at the cutoff rank, all tied employees are returned (the response may exceed `limit`), ordered deterministically by `emp_code` ascending.

4. **Headcount.**
Department summary anchors headcount on the `employees` collection filtered by `joined_on <= month_end` (R9), establishing the true roster independent of attendance records. Attendance statistics from `attendance_logs` are joined by department. Employees with zero logs are fully counted in department headcount while contributing zero present days.

5. **One thing you would change for 100x data.**
At 100x scale (~10M+ records), running ad-hoc cross-collection lookups and `$densify` on every request would degrade database performance. I would introduce incremental pre-aggregation pipelines (via MongoDB `$merge` or change streams) to maintain daily department rollups and monthly employee summaries, turning analytics endpoints into O(1) point reads.
