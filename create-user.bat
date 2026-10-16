@echo off
title LeadGen - Create a login
rem Creates a login for the dashboard (an admin, unless you add --member), or resets its password.
cd /d "%~dp0backend"
.venv\Scripts\python -m app.create_user %*
pause
