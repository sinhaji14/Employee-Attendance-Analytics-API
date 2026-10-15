# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

# DECISIONS.md

## 1. Indexes

I created a unique index on `employees.emp_code` to prevent duplicate employee codes. A unique compound index on `attendance_logs.emp_code` and `date` prevents duplicate attendance records. Additional indexes support filtering by date, employee, status, department, and joining date. Indexes improve read performance but add storage and write overhead.

## 2. Punch-in race

Two identical punch-in requests can both initially find no attendance record. The unique compound index is the final safeguard: MongoDB accepts one insert and rejects the conflicting insert. The application catches `DuplicateKeyError` and returns HTTP 409 for the losing request.

## 3. Ties

The leaderboard uses MongoDB ranking so employees with equal late-minute totals share a rank, and the next rank is skipped. Employees are ordered by employee code to make tied results deterministic. The limit must be applied after ranking to preserve the correct rank values.

## 4. Headcount

The department summary starts from employees and joins attendance data, allowing employees with no attendance records to remain in the results. It filters out employees whose joining date is after the reporting period ends.

## 5. Scaling

For 100 times more data, I would inspect execution statistics and query plans, review index selectivity, and benchmark the aggregation pipelines. I would also consider workload monitoring and pagination limits to keep response times predictable.
