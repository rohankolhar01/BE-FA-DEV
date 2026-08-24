@echo off
REM Starts the API. Must run from backend\ (not backend\app\) so that "app" is
REM importable as a package - main.py uses relative imports (from . import ...).
cd /d "%~dp0"
"..\.venv\Scripts\python.exe" -m uvicorn app.main:app --reload --port 8000
