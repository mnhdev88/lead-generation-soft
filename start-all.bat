@echo off
rem Opens the backend and the dashboard in two windows, then the browser.
start "" "%~dp0start-backend.bat"
start "" "%~dp0start-frontend.bat"
timeout /t 8 /nobreak >nul
start http://localhost:3000
