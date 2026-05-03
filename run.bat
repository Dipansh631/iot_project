@echo off
echo ============================================================
echo Starting TrafficMonitor AI System
echo ============================================================

REM Kill any process running on port 8000
echo Checking for existing server on port 8000...
powershell -Command "Get-Process -Id (Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue).OwningProcess -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue"


IF EXIST "venv311\Scripts\activate.bat" (
    echo Activating virtual environment...
    call venv311\Scripts\activate.bat
) ELSE (
    echo Warning: No venv311 found. Continuing with global Python environment...
)

REM Install requirements if they are not installed
echo Installing dependencies from requirements.txt...
pip install -r requirements.txt

REM Start the FastAPI application
echo.
echo Starting FastAPI server on http://127.0.0.1:8000
echo.
uvicorn main:app --host 127.0.0.1 --port 8000 --reload

