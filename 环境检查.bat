@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Douyin Downloader - Environment Check
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    echo.
    echo   Python not found.
    echo   Please install Python 3.9 or newer: https://www.python.org/downloads/
    echo   During install, tick "Add Python to PATH".
    echo.
    pause
    exit /b 1
)
%PY% app.py --checkenv %*
echo.
pause
