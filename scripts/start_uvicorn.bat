@echo off
REM Launch the AutoMoney bot dashboard.
REM
REM Uses the embeddable Python at C:\Users\Wallace\Python313\ (per
REM CLAUDE.md — installed via the embeddable zip with pip bootstrapped).
REM .env is read by python-dotenv at startup; auth is admin/Wallace1.
REM
REM Logs go to uvicorn.log so the bot survives terminal close.
REM Stop with: kill the python.exe pid listening on :8000
REM   netstat -ano ^| findstr :8000
REM   taskkill /F /PID ^<pid^>

cd /d "%~dp0\.."
"C:\Users\Wallace\Python313\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8000
