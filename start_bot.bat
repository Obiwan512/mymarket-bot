@echo off
rem Starts the bot in loop mode (checks every 5 minutes).
rem Autostart: Win+R, type shell:startup, put a shortcut to this file there.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
".venv\Scripts\python.exe" bot.py --loop
pause
