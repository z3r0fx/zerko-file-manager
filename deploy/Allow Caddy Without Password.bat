@echo off
title Zerko - allow Caddy to start on its own
color 0B
echo.
echo   Allowing Caddy to start without a password
echo   ==========================================
echo.
echo   Ports 80 and 443 need administrator rights on the Linux side,
echo   which is why Caddy asks for your WSL password every time.
echo.
echo   A scheduled task cannot type a password, so auto-start cannot
echo   bring HTTPS back up until this is done.
echo.
echo   This grants passwordless rights to the Caddy program ONLY.
echo   Every other command still asks, as normal.
echo.
echo   You will be asked for your WSL password once, now.
echo.

set "HEREWIN=%~dp0"
set "HEREWIN=%HEREWIN:~0,-1%"
for /f "usebackq delims=" %%i in (`wsl wslpath -a "%HEREWIN%" 2^>nul`) do set "D=%%i"

if "%D%"=="" (
    echo   [!] Could not talk to WSL.
    pause
    exit /b 1
)

wsl -- bash -lc "cd '%D%' && ./allow-caddy-nopasswd.sh"

echo.
echo   Checking it worked...
wsl -- bash -lc "sudo -n /usr/local/bin/caddy-duckdns version >/dev/null 2>&1 && echo '   [OK] Caddy can now start without a password.' || echo '   [!] Still asking for a password - tell Claude.'"
echo.
pause
