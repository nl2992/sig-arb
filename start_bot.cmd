@echo off
rem Restart the SIG bot live with all strategies under the supervisor (double-click to run).
cd /d "%~dp0"
.venv\Scripts\python go_live.py stop
set RELEASE=
if exist logs\KILL_SWITCH (
  echo Kill switch is engaged:
  type logs\KILL_SWITCH
  choice /M "Release it and start trading"
  if errorlevel 2 goto :status
  set RELEASE=--release-kill-switch
)
.venv\Scripts\python go_live.py start %RELEASE% --wait
:status
.venv\Scripts\python go_live.py status
pause
