@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0worker.ps1" -Action RestartRunning
pause
