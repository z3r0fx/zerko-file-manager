@echo off
title Zerko - stop HTTPS
set "HEREWIN=%~dp0"
set "HEREWIN=%HEREWIN:~0,-1%"
for /f "usebackq delims=" %%i in (`wsl wslpath -a "%HEREWIN%" 2^>nul`) do set "HERE=%%i"
echo.
wsl -- bash -lc "cd '%HERE%' && ./caddy-bg.sh stop"
echo.
pause
