# Employee Attendance & Analytics API

## Project Overview

This project implements an Employee Attendance & Analytics API using FastAPI and MongoDB. It supports employee management, attendance tracking, attendance regularization, and department-level analytics.

## Features

- Employee creation and listing with pagination.
- Attendance punch-in and punch-out.
- Attendance regularization with change history.
- Attendance filtering by employee code, date range, and status.
- Employee monthly attendance summaries.
- Department attendance summaries.
- Late-arrival leaderboard with ranking.
- Department daily trends with a seven-day moving average.
- MongoDB explain endpoint for inspecting query execution plans.
- MongoDB indexes for uniqueness and query performance.
- Input validation and appropriate HTTP error responses.

## Prerequisites

- Python 3.10 or later (tested with Python 3.11.9)
- MongoDB 6.0 or later (tested with MongoDB Server 9.0.2).
- pip (Python package installer).

## Local Setup

Run these commands below from the candidate_kit project directory.

### 1. Activate the virtual environment

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

### 2. Install dependencies

```powershell
pip install -r requirements.txt
```

### 3. Configure environment variables

Create a `.env` file based on `.env.example` and configure the following values:

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Ensure the MongoDB server is running before starting the API.

### 4. Load sample data

```powershell
python sample_seed.py
```

Run this command when you need to load the supplied sample dataset. Avoid rerunning it unnecessarily if you want to preserve your existing test data.

### 5. Start the API

```powershell
uvicorn app.main:app --port 8000
```

For development with automatic reload:

```powershell
uvicorn app.main:app --port 8000 --reload
```

## API Documentation

Once the server is running, open:

- Health check: http://127.0.0.1:8000/health
- Swagger UI: http://127.0.0.1:8000/docs
- OpenAPI JSON: http://127.0.0.1:8000/openapi.json

## Testing

API endpoints have been tested through Swagger UI for employee creation, duplicate handling, attendance punch-in and punch-out conflicts, regularization history, pagination, filtering, analytics endpoints, and MongoDB explain plans.



