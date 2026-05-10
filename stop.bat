@echo off
REM AutoMoney bot stopper.
REM Mata o processo Python rodando uvicorn na :8000.

echo Procurando uvicorn na :8000...
set FOUND=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":8000"') do (
    echo Matando PID %%a...
    taskkill /F /PID %%a
    set FOUND=1
)

if %FOUND%==0 (
    echo Nada rodando em :8000.
) else (
    echo AutoMoney parado.
)

pause
