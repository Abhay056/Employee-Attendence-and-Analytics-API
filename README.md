# Employee Attendance & Analytics API

FastAPI and MongoDB backend for employee attendance tracking (punch-in/out, business rules R1–R10, manual regularizations with audit trail) and analytics pipelines (department summaries, monthly summaries, competition-ranked leaderboards, and gap-filled daily trends with 7-day moving averages).

## Requirements
- Python 3.11+
- MongoDB 6.0+ (Atlas or local instance)

## Setup & Running
```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment variables (optional, defaults to localhost:27017 and attendance_db)
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"

# 3. Seed sample data (optional)
python sample_seed.py

# 4. Start API server
uvicorn app.main:app --port 8000
```

## Key Deliverables
- [app/main.py](file:///c:/Users/Asus/Documents/Projects/Employee-Attendence-API/app/main.py): Complete API implementation with all endpoints, startup indexing, and aggregations.
- [REVIEW.md](file:///c:/Users/Asus/Documents/Projects/Employee-Attendence-API/REVIEW.md): Detailed code review of the 14 defects identified in the starter code.
- [DECISIONS.md](file:///c:/Users/Asus/Documents/Projects/Employee-Attendence-API/DECISIONS.md): Concise answers to the five architectural and design decisions.
