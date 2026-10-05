@echo off
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 start_finblocks.py %*
    if errorlevel 1 pause
    exit /b
)
if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" (
    "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" start_finblocks.py %*
    if errorlevel 1 pause
    exit /b
)
python start_finblocks.py %*
if errorlevel 1 pause
