@echo off
setlocal
cd /d "%~dp0"
if exist "venv\Scripts\python.exe" (
  "venv\Scripts\python.exe" -m pip install -r "requirements.txt"
) else if exist "..\venv\Scripts\python.exe" (
  "..\venv\Scripts\python.exe" -m pip install -r "requirements.txt"
) else (
  py -m pip install -r "requirements.txt"
)
echo.
echo Da cai thu vien flood_model.
endlocal
