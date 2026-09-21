@echo off
chcp 65001 > nul 2>&1
setlocal

title KYROSHIX BOT
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   [KYROSHIX BOT] Virtual environment not found.
    echo   Execute setup.bat first.
    echo.
    pause
    exit /b 1
)

REM AMD ROCm / MIOpen
set "MIOPEN_FIND_MODE=FAST"
set "MIOPEN_LOG_LEVEL=3"

echo.
echo   ==============================================
echo                  KYROSHIX BOT
echo              Real-time VRChat AI
echo   ==============================================
echo.
echo   Starting KYROSHIX BOT...
echo   AMD ROCm / MIOpen: FAST
echo   Press Ctrl+C to stop.
echo.

".venv\Scripts\python.exe" "supervisor.py"

set "EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%EXIT_CODE%"=="0" (
    echo   [KYROSHIX BOT] Process exited with code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%