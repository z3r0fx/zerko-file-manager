@echo off
title Zerko - install auto-start
net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo   [!] Right-click this file and choose "Run as administrator".
    echo.
    pause
    exit /b 1
)

set "HEREWIN=%~dp0"
set "HEREWIN=%HEREWIN:~0,-1%"
for /f "usebackq delims=" %%i in (`wsl wslpath -a "%HEREWIN%" 2^>nul`) do set "D=%%i"
if "%D%"=="" ( echo   [!] Could not talk to WSL. & pause & exit /b 1 )

echo.
echo   Installing Zerko auto-start
echo   ===========================
echo.
echo   Two tasks get created:
echo     - one at logon, to bring Zerko back after a restart
echo     - one every 10 minutes, to bring it back if it crashes
echo.
echo   BOTH respect the off switch. If you run STOP Zerko.bat,
echo   neither task will start anything until you start it yourself.
echo.

schtasks /Create /TN "Zerko\Watchdog" /SC MINUTE /MO 10 /F /RL HIGHEST ^
  /TR "wsl -- bash -lc \"cd '%D%' && ./zerko-service.sh watchdog\"" >nul 2>&1
if errorlevel 1 (echo   [!] watchdog task failed) else (echo   [OK] watchdog every 10 minutes)

schtasks /Create /TN "Zerko\At Logon" /SC ONLOGON /F /RL HIGHEST /DELAY 0001:00 ^
  /TR "wsl -- bash -lc \"cd '%D%' && ./zerko-service.sh watchdog\"" >nul 2>&1
if errorlevel 1 (echo   [!] logon task failed) else (echo   [OK] starts 1 min after you log in)

echo.
echo   Done.
echo.
echo   To remove them later:
echo      schtasks /Delete /TN "Zerko\Watchdog" /F
echo      schtasks /Delete /TN "Zerko\At Logon" /F
echo.
pause
