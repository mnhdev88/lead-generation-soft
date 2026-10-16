@echo off
rem One-time install (only needed on a new computer or after deleting .venv / node_modules).
cd /d "%~dp0backend"
if not exist .venv python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\pip install -r requirements.txt
cd /d "%~dp0frontend"
call npm install
echo.
echo Setup complete. First time: run create-user.bat to make your login.
echo Then run start-all.bat to launch LeadGen.
pause
