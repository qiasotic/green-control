@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run 01_SETUP_WINDOWS.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pytest -q
if errorlevel 1 (
  echo [ERROR] Some tests failed.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" cli.py validate "examples\TECHNICAL_SMOKE_3_CASES.csv"
set "CHECK_EXIT=%ERRORLEVEL%"
if not defined CI pause
exit /b %CHECK_EXIT%
