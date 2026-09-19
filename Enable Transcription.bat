@echo off
title Zerko - enable transcription
color 0B
echo.
echo   ENABLE TRANSCRIPTION
echo   ====================
echo.
echo   This makes everything you say on camera searchable, and tags clips
echo   automatically from what is said.
echo.
echo   It downloads roughly 2-3 GB (PyTorch + the Whisper model) and works
echo   far better with an NVIDIA graphics card. Without one it still runs,
echo   just slowly.
echo.
echo   You only do this once.
echo.
pause

set "HEREWIN=%~dp0"
set "HEREWIN=%HEREWIN:~0,-1%"
for /f "usebackq delims=" %%i in (`wsl wslpath -a "%HEREWIN%" 2^>nul`) do set "HERE=%%i"
if "%HERE%"=="" ( echo   [!] Could not talk to WSL. & pause & exit /b 1 )

wsl -- bash -lc "cd '%HERE%' && ./enable-transcription.sh"
echo.
pause
