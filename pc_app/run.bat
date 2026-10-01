@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw companion.py
) else (
  python companion.py
)
