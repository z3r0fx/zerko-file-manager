@echo off
title Zerko - fix localhost access
color 0B
echo.
echo   FIX LOCALHOST ACCESS
echo   ====================
echo.
echo   Zerko runs inside WSL. On some PCs Windows does not forward
echo   localhost into WSL, so http://localhost:9600 refuses to connect
echo   even though the server is running perfectly.
echo.
echo   This switches WSL to "mirrored" networking, which makes it share
echo   the Windows network directly. localhost then just works, and the
echo   address stops changing every reboot.
echo.
echo   WSL will be shut down and restarted. Save anything open in Linux.
echo.
pause

set "CFG=%USERPROFILE%\.wslconfig"

if exist "%CFG%" (
    echo.
    echo   You already have a .wslconfig file. Backing it up...
    copy /y "%CFG%" "%CFG%.backup" >nul
    findstr /i /c:"networkingMode" "%CFG%" >nul 2>&1
    if not errorlevel 1 (
        echo   It already sets networkingMode. Not changing it.
        echo   Open it yourself if you need to:  notepad "%CFG%"
        echo.
        pause
        exit /b 0
    )
)

echo.>> "%CFG%"
echo [wsl2]>> "%CFG%"
echo networkingMode=mirrored>> "%CFG%"
echo firewall=true>> "%CFG%"
echo dnsTunneling=true>> "%CFG%"

echo.
echo   Written to %CFG%
echo.
echo   Restarting WSL...
wsl --shutdown
timeout /t 5 >nul

echo.
echo   Done. Start Zerko again and try http://localhost:9600
echo.
echo   If it still will not connect, use the address the server window
echo   prints next to "On your network" instead - that always works.
echo.
pause
