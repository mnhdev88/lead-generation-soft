@echo off
title LeadGen - Backend (API)
cd /d "%~dp0backend"
echo Starting backend at http://localhost:8000  (API docs: http://localhost:8000/docs)
.venv\Scripts\python -m uvicorn app.main:app --reload --port 8000
pause
