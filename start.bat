@echo off
REM AutoMoney bot launcher.
REM
REM Mata uvicorn anterior na :8000 (se houver), inicia novo em background,
REM redireciona logs pra uvicorn.log, abre browser. Pode fechar o cmd
REM depois — bot continua rodando.
REM
REM Pra parar: stop.bat
REM Pra ver logs: type uvicorn.log (ou abrir no editor)

cd /d "%~dp0"

echo [1/4] Checando uvicorn anterior em :8000...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":8000"') do (
    echo     Matando PID %%a...
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 1 /nobreak >nul

echo [2/4] Iniciando uvicorn em background...
start "" /B "C:\Users\Wallace\Python313\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8000 > uvicorn.log 2>&1

echo [3/4] Aguardando boot (8s)...
timeout /t 8 /nobreak >nul

echo [4/4] Abrindo dashboard no browser...
start "" "http://admin:Wallace1@127.0.0.1:8000/"

echo.
echo ========================================================
echo  AutoMoney rodando em http://127.0.0.1:8000
echo  Logs:  type uvicorn.log
echo  Parar: stop.bat
echo ========================================================
echo.
echo Pode fechar essa janela. Bot continua rodando.
pause
