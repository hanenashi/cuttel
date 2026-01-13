@echo off
setlocal

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [FAIL] Python not found in PATH.
  echo Install Python, then re-open terminal, or run: py cuttel.py
  pause
  exit /b 1
)

echo [OK] Starting cuttel.py ...
python "%~dp0cuttel.py"

echo.
echo [INFO] Program ended (closed window or error).
pause
