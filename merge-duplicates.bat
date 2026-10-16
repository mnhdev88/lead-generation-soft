@echo off
title LeadGen - Merge duplicate businesses
rem Finds businesses saved twice (e.g. from OpenStreetMap and Google) and shows them.
rem Then asks before merging. A backup copy of the database is made first.
cd /d "%~dp0backend"
.venv\Scripts\python -m app.dedupe
echo.
set /p ANSWER=Merge the duplicates listed above? Type Y and press Enter:
if /i not "%ANSWER%"=="Y" goto :end
copy /y data\leads.db data\leads-before-merge.db >nul
echo Backup saved as backend\data\leads-before-merge.db
.venv\Scripts\python -m app.dedupe --apply
:end
pause
