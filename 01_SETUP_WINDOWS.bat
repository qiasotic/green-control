@echo off
setlocal
cd /d "%~dp0"
echo [Green Control] Checking Python installation...
if not exist ".venv\Scripts\python.exe" (
  call :create_environment
  if errorlevel 1 goto setup_failed
)
echo Installing project packages (internet required unless packages are cached)...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto setup_failed
echo Running checks...
".venv\Scripts\python.exe" -m pytest -q
if errorlevel 1 goto setup_failed
echo [OK] Installation and tests passed.
exit /b 0

:create_environment
set "PY_LAUNCH_VERSION="
where py.exe >nul 2>&1
if not errorlevel 1 (
  for %%V in (3.12 3.13 3.14) do if not defined PY_LAUNCH_VERSION (
    py -%%V -V >nul 2>&1 && set "PY_LAUNCH_VERSION=%%V"
  )
)
if defined PY_LAUNCH_VERSION (
  py -%PY_LAUNCH_VERSION% -m venv ".venv"
  goto environment_created
)
set "PYTHON_EXE="
for %%V in (312 313 314) do if not defined PYTHON_EXE (
  if exist "%USERPROFILE%\AppData\Local\Programs\Python\Python%%V\python.exe" (
    set "PYTHON_EXE=%USERPROFILE%\AppData\Local\Programs\Python\Python%%V\python.exe"
  )
)
if defined PYTHON_EXE (
  "%PYTHON_EXE%" -m venv ".venv"
  goto environment_created
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1
if errorlevel 1 (
  echo Python 3.12 or later not found. Install Python x64 and enable Add python.exe to PATH.
  exit /b 1
)
python -m venv ".venv"

:environment_created
exit /b %ERRORLEVEL%

:setup_failed
echo [ERROR] Setup failed. Check the message above.
if not defined CI pause
exit /b 1
