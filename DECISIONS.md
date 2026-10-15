# DECISIONS.md

## 1. Indexes

I created a unique index on `employees.emp_code` to prevent duplicate employee codes. A unique compound index on `attendance_logs.emp_code` and `date` prevents duplicate attendance records. Other indexes support common queries involving dates, employees, status, departments, and joining dates. Indexes improve read performance but require additional storage and can slow down writes.

## 2. Punch-in Race Conditions

Two simultaneous punch-in requests may both find no existing attendance record. The unique compound index ensures that only one insert succeeds. The application catches `DuplicateKeyError` and returns HTTP 409 for the conflicting request.

## 3. Leaderboard Ties

The leaderboard uses MongoDB ranking so employees with equal late-minute totals share the same rank, and the next rank is skipped. Employee code provides deterministic ordering. Ranking is applied before limiting results to preserve correct rank values.

## 4. Department Headcount

The department summary starts from employees and joins attendance records. This includes eligible employees who have no attendance logs. Employees who joined after the reporting period ends are excluded.

## 5. Scaling

For 100 times more data, I would inspect execution statistics and query plans, review index selectivity, and benchmark aggregation pipelines. I would also monitor database performance and enforce sensible pagination limits to keep response times predictable.
