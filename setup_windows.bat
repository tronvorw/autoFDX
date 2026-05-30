@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist "tools\setup_windows.ps1" (
    echo tools\setup_windows.ps1 not found.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\setup_windows.ps1"
set "EXIT_CODE=%errorlevel%"
if not "%EXIT_CODE%"=="0" (
    pause
    exit /b %EXIT_CODE%
)

pause
exit /b 0
