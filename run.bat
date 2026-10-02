@echo off
setlocal
cd /d "%~dp0"
if exist "venv\Scripts\python.exe" (
  "venv\Scripts\python.exe" "app.py"
) else if exist "..\venv\Scripts\python.exe" (
  "..\venv\Scripts\python.exe" "app.py"
) else (
  py "app.py"
)
endlocal
