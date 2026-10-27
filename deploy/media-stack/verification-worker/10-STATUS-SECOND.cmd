@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0worker.ps1" -Action Status -Slot 2
set "WORKER_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %WORKER_EXIT%
