@echo off
title Zerko - reset a password
color 0B
cd /d "%~dp0"

echo.
echo   RESET A PASSWORD
echo   ================
echo.
echo   Use this if you cannot sign in to Zerko. It lists the accounts on this
echo   PC and lets you choose a new password for one of them.
echo.
echo   Passwords are stored scrambled and can never be shown - a reset is the
echo   only way back in. Nothing you type is displayed or saved anywhere else.
echo.

where wsl >nul 2>&1
if errorlevel 1 (
    echo   Windows Subsystem for Linux is not installed, so Zerko has not run here.
    pause
    exit /b 1
)

set "HEREWIN=%~dp0"
set "HEREWIN=%HEREWIN:~0,-1%"
for /f "usebackq delims=" %%i in (`wsl wslpath -a "%HEREWIN%" 2^>nul`) do set "HERE=%%i"
if "%HERE%"=="" (
    echo   [!] Could not read this folder from WSL.
    pause
    exit /b 1
)

wsl -- bash -lc "cd '%HERE%' && if [ -x venv/bin/python ]; then ./venv/bin/python reset_password.py; else echo '  Zerko has not been set up here yet - run Start Zerko.bat first.'; fi"

echo.
pause
