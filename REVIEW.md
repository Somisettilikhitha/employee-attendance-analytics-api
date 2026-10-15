# Starter Code Review — `app/main.py`

## 1. Defects Found and Fixed

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `list_employees()` | The pagination offset was incorrect. | Request pages 1 and 2 with `page_size=5` and check for missing or repeated records. | Changed the offset to `skip = (page - 1) * page_size`. |
| 2 | `list_employees()` | Pagination inputs were not validated. | Request `page=0`, `page_size=0`, or `page_size=101`. | Added validation requiring `page >= 1` and `page_size` between 1 and 100. Invalid values return HTTP 422. |
| 3 | `list_employees()` | The total count did not apply the active filters. | Filter by department and compare the total with matching records. | Used `count_documents(q)` with the same query filter. |
| 4 | `create_employee()` | A duplicate check alone cannot prevent simultaneous requests from creating the same employee code. | Send concurrent requests using the same `emp_code`. | Added a unique MongoDB index on `emp_code` and handled `DuplicateKeyError` with HTTP 409. |
| 5 | `punch_in()` | Checking for an existing attendance record does not prevent duplicates during concurrent requests. | Send simultaneous punch-in requests for the same employee and date. | Added a unique compound index on `emp_code` and `date` and handled duplicate-key errors with HTTP 409. |
| 6 | `punch_out()` | Repeated punch-out requests must not overwrite an existing punch-out timestamp. | Submit multiple punch-out requests for the same attendance record. | Used an atomic conditional update and returned HTTP 409 if the record was already closed. |
| 7 | `regularize_attendance()` | Concurrent regularizations could cause history entries to be lost. | Regularize the same record multiple times and inspect its history. | Used MongoDB `$push` to append history entries and conditional updates to detect conflicts. |
| 8 | `create_indexes()` | Redundant or conflicting index definitions can cause startup errors or unnecessary overhead. | Restart the application and inspect startup logs and existing indexes. | Kept the required indexes with consistent definitions and removed redundant indexes. |



## 2. Items Reviewed but Not Considered Defects

| Item reviewed | Why it was not considered a defect |
|---|---|
| Half-day boundary | The rule sets `half_day = true` when rounded work hours are below 4.50. The tested results were true at 4.48 hours and false at 4.50 hours. |
| Employee pagination | After the fix, tests returned the expected records across pages and the correct filtered total. |
| Attendance pagination | With 13 matching records and a page size of 5, the API returned 5, 5, and 3 records across three pages. |
| Regularization history | Two successive regularizations preserved both history entries in order. |


## 3. Missing Required Functionality

The original starter code did not include all the functionality required by the assignment. The following features were implemented to meet the requirements.

1. Punch-Out Functionality: Implemented the punch-out endpoint to record an employee's punch-out time and calculate work hours, overtime, and half-day status. Added validation to prevent repeated punch-out requests from overwriting an existing punch-out timestamp.


2. Attendance Regularization: Implemented the PATCH regularization endpoint to update attendance status. Added history tracking to preserve previous and updated attendance statuses and prevent concurrent updates from silently losing history entries.


3. Monthly Employee Summary: Implemented the monthly employee summary endpoint using MongoDB aggregation. It calculates working days, attendance, late arrivals, overtime, and other required employee-level statistics.


4. Department Summary: Implemented the department summary endpoint to calculate department-level headcount and attendance statistics using employee and attendance collections.


5. Late Leaderboard: Implemented the late leaderboard endpoint to rank employees according to their total late-arrival minutes. Competition ranking is used so that tied employees receive the same rank and the next rank is skipped accordingly.


6. Department Trend: Implemented the department trend endpoint to provide daily attendance statistics, fill missing dates, and calculate the seven-day moving average using MongoDB aggregation.


7. MongoDB Explain Endpoint: Implemented the /admin/explain/{endpoint} endpoint to inspect MongoDB query execution statistics for the required query types: attendance_list, employee_monthly, department_summary, late_leaderboard, and department_trend.



## 4. Verification Notes

The following behaviors were tested through Swagger UI:

- Duplicate employee creation returned HTTP 409.
- Duplicate punch-in returned HTTP 409.
- Repeated punch-out returned HTTP 409.
- Punch-in for a nonexistent employee returned HTTP 404.
- An invalid attendance status returned HTTP 422.
- Invalid pagination values returned HTTP 422.
- Attendance regularization preserved the change history.
- All five MongoDB explain endpoint keys were tested: attendance_list, employee_monthly, department_summary, late_leaderboard, and department_trend.
- The observed explain plans used indexes and reported no collection scans.



