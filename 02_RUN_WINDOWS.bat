@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First launch: setting up dependencies...
  call "01_SETUP_WINDOWS.bat"
  if errorlevel 1 exit /b 1
)
echo Starting Green Control. Use the URL printed below if the browser does not open automatically.
echo Keep this window open while you use the application.
".venv\Scripts\python.exe" -m streamlit run app.py --server.address=127.0.0.1 --server.headless=false --browser.serverAddress=127.0.0.1
if errorlevel 1 (
  echo App failed to launch. If another Green Control window is running, close it and retry.
  echo If the issue persists, run 03_TEST_WINDOWS.bat.
  pause
  exit /b 1
)
endlocal
