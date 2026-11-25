@echo off
REM autostart.bat — Windows entry point for Stash Scheduler auto-start.
REM
REM Delegates to ensure-running.ps1 which:
REM   - Defaults to http://127.0.0.1:9998 (reads port from Stash config if present)
REM   - Loads ApiKey from stash-scheduler-config.json or .stash\config.yml
REM   - Uses systemStatus (not the invalid {health} query)
REM   - Is idempotent if the daemon is already running
REM
REM Usage:
REM   autostart.bat
REM   autostart.bat http://127.0.0.1:9998
REM   autostart.bat http://127.0.0.1:9998 your-api-key-here
REM
REM Prefer the Scheduled Task "Stash Schedule" (logon + periodic) over shell:startup.

setlocal
SET SCRIPT_DIR=%~dp0
SET STASH_URL=%~1
SET API_KEY=%~2

SET PS_ARGS=-NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%ensure-running.ps1"
IF NOT "%STASH_URL%"=="" SET PS_ARGS=%PS_ARGS% -StashUrl "%STASH_URL%"
IF NOT "%API_KEY%"=="" SET PS_ARGS=%PS_ARGS% -ApiKey "%API_KEY%"

powershell.exe %PS_ARGS%
SET EXITCODE=%ERRORLEVEL%
endlocal & exit /b %EXITCODE%
