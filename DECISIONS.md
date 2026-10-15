# DECISIONS.md

1. **Indexes:**  At startup, my code creates a unique index on employees.emp_code to prevent duplicate employee codes and a unique compound index on attendance_logs.emp_code and date to prevent duplicate daily attendance records. I also created supporting indexes for department and joining-date queries, employee/date attendance lookups, and late-minute sorting. I avoided creating a redundant index when an existing index already supports the required query.

2. **Punch-in race:** Two requests may both pass the initial duplicate check. The unique index allows only one insert for the same employee and date. The other request raises `DuplicateKeyError`, which the API converts to HTTP 409 Conflict.

3. **Ties:** The leaderboard ranks employees by late minutes and filters using rank <= limit. Employees tied at the cutoff are included, receive the same rank, and cause the next rank to be skipped.

4. **Headcount:** The department summary starts with employees who joined on or before the period end and looks up their attendance logs. Employees with no logs remain in the result and are included in headcount.

5. **Scaling:** For 100 times more data, I would consider pre-aggregating daily attendance statistics to reduce repeated aggregation work.
