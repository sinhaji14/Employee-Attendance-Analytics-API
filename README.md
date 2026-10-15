# Employee Attendance & Analytics API

A RESTful backend for employee attendance tracking, shift management, attendance regularization, and workforce analytics, built with **FastAPI** and **MongoDB**.

## Overview

The Employee Attendance & Analytics API provides employee management, punch-in and punch-out workflows, attendance calculations, and department-level reporting. It uses MongoDB indexes and aggregation pipelines to support data integrity and analytics queries.

## Tech Stack

- **Language:** Python
- **Backend:** FastAPI
- **Database:** MongoDB Atlas
- **Data Validation:** Pydantic
- **API Documentation:** OpenAPI / Swagger UI
- **Testing:** pytest
- **Server:** Uvicorn

## Key Features

- **Employee Management** — Create and retrieve employee records with department, joining date, and shift details.
- **Attendance Tracking** — Record punch-in and punch-out timestamps and calculate work hours.
- **Shift-Aware Calculations** — Support overnight shifts, late arrivals, overtime, and half-day classification.
- **Attendance Regularization** — Correct attendance records with a history of changes, reasons, and timestamps.
- **Monthly Analytics** — Retrieve employee-level monthly attendance summaries.
- **Department Analytics** — Generate department summaries and daily attendance trends.
- **Late-Arrival Leaderboard** — Rank employees by accumulated late minutes, with tie-aware ranking.
- **Data Integrity** — Use unique indexes and conditional database updates to help prevent duplicate attendance records and conflicting punch operations.
- **Query Analysis** — Inspect MongoDB execution statistics through the explain endpoint.

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Health check |
| POST | `/employees` | Create an employee |
| GET | `/employees` | List employees |
| POST | `/attendance/punch-in` | Record punch-in |
| POST | `/attendance/punch-out` | Record punch-out |
| GET | `/attendance` | Retrieve attendance records |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize an attendance record |
| GET | `/analytics/employees/{emp_code}/monthly` | Employee monthly analytics |
| GET | `/analytics/departments/summary` | Department summary |
| GET | `/analytics/leaderboard/late` | Late-arrival leaderboard |
| GET | `/analytics/departments/{department}/trend` | Department attendance trend |
| GET | `/admin/explain/{endpoint}` | Inspect query execution statistics |

Interactive API documentation is available at `/docs` while the server is running.

## Business Rules

- All API timestamps are represented as Unix epoch milliseconds.
- Calendar dates use `YYYY-MM-DD`; shift times use `HH:MM` in Indian Standard Time (IST).
- Overnight shifts are supported, including attendance-date assignment.
- Late arrival is recorded only when the employee is more than 10 minutes late.
- Overtime is recorded when the time worked beyond the shift end reaches at least 30 minutes.
- Work hours are rounded to two decimal places, using half-up rounding.
- A record is classified as a half-day when rounded work hours are below 4.50.
- Weekends are excluded from working-day attendance calculations, while recorded weekend attendance can still contribute to late-arrival and overtime metrics.

## Project Structure

```text
candidate_kit/
├── app/
│   ├── __init__.py
│   └── main.py
├── sample_data/
│   ├── attendance_logs.json
│   └── employees.json
├── tests/
│   └── test_attendance.py
├── DATA_MODEL.md
├── DECISIONS.md
├── REVIEW.md
├── openapi.yaml
├── requirements.txt
├── sample_seed.py
└── README.md
```

## Getting Started

### Prerequisites

- Python 3.10 or newer
- MongoDB 6.0 or newer, or a MongoDB Atlas cluster
- Git

### 1. Clone the repository

```bash
git clone https://github.com/sinhaji14/Employee-Attendance-Analytics-API.git
cd Employee-Attendance-Analytics-API
```

### 2. Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

Create a `.env` file in the project root:

```env
MONGO_URI=your_mongodb_connection_string
MONGO_DB=employee_attendance
```

Replace the connection string with your MongoDB connection details. For MongoDB Atlas, ensure your IP address is allowed in the cluster's network access settings.

**Security:** Never commit `.env` or database credentials to version control.

### 5. Load sample data (optional)

```bash
python sample_seed.py
```

Run this only when you intend to load the supplied sample dataset into the configured database.

### 6. Start the API

```bash
uvicorn app.main:app --port 8000
```

The API will be available at:

- Base URL: `http://127.0.0.1:8000`
- Swagger UI: `http://127.0.0.1:8000/docs`
- Health check: `http://127.0.0.1:8000/health`

## Running Tests

Run the test suite from the project root:

```bash
python -m pytest -q
```

The tests cover attendance calculations, rounding boundaries, shift handling, status validation, and selected API/database behaviors.

## Design and Documentation

- **`DATA_MODEL.md`** — MongoDB collections, fields, and sample documents.
- **`DECISIONS.md`** — Key implementation decisions and trade-offs.
- **`REVIEW.md`** — Starter-code defect review and corrective actions.
- **`openapi.yaml`** — API contract and business-rule definitions.

## Engineering Considerations

- Unique indexes enforce employee-code and employee/date attendance uniqueness.
- Conditional database updates help handle competing punch operations safely.
- MongoDB aggregation pipelines support analytics without fetching entire datasets into application memory.
- Attendance regularization preserves an audit trail of corrections.
- Query execution statistics help assess database query plans and index usage.

## Submission Notes

This repository contains the API implementation, dependency list, tests, sample data, API contract, and supporting design documentation.

The application expects MongoDB configuration through environment variables. Keep credentials and local environment files out of the public repository.

---

**Built with FastAPI and MongoDB.**
