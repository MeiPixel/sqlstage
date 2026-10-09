@echo off
setlocal
cd /d "%~dp0"
if /i "%~1"=="frontend" goto frontend
if not exist ".venv\Scripts\python.exe" (
  echo Create .venv and install requirements.txt first. See README.md.
  exit /b 1
)
if /i "%~1"=="check" goto check
if "%~1"=="" goto backend
if /i "%~1"=="backend" goto backend
echo Usage: run.bat [backend^|frontend^|check]
exit /b 1
:check
".venv\Scripts\python.exe" -c "import sqlstage, fastapi, uvicorn, yaml, socksio; from sqlstage import ModelConfig; c = ModelConfig(); print('SQLStage OK; provider=' + c.provider + '; thinking=' + c.thinking)"
exit /b %ERRORLEVEL%
:backend
echo Backend: http://127.0.0.1:8020 - Ctrl+C to stop
".venv\Scripts\python.exe" -m uvicorn server.main:app --host 127.0.0.1 --port 8020
exit /b %ERRORLEVEL%
:frontend
if not exist "frontend\node_modules" (
  echo Run npm ci in frontend first.
  exit /b 1
)
cd frontend
call npm run dev -- --host 127.0.0.1 --port 5173 --strictPort
exit /b %ERRORLEVEL%
