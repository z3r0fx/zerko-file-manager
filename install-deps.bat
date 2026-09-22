@echo off
REM Downloads a portable ffmpeg into this folder. Nothing is installed
REM system-wide and nothing outside this folder is touched.
setlocal
cd /d "%~dp0"

if exist "%~dp0ffmpeg\bin\ffmpeg.exe" (
    echo   ffmpeg is already here.
    exit /b 0
)

echo   Downloading ffmpeg (about 80 MB)...
powershell -NoProfile -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$u='https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip';" ^
  "Invoke-WebRequest -Uri $u -OutFile \"$env:TEMP\ffmpeg.zip\";" ^
  "Expand-Archive -Path \"$env:TEMP\ffmpeg.zip\" -DestinationPath \"$env:TEMP\ffmpeg_x\" -Force;" ^
  "$d=Get-ChildItem \"$env:TEMP\ffmpeg_x\" -Directory | Select-Object -First 1;" ^
  "Move-Item $d.FullName '%~dp0ffmpeg' -Force;" ^
  "Remove-Item \"$env:TEMP\ffmpeg.zip\",\"$env:TEMP\ffmpeg_x\" -Recurse -Force -ErrorAction SilentlyContinue"

if exist "%~dp0ffmpeg\bin\ffmpeg.exe" (
    echo   ffmpeg ready.
) else (
    echo   [!] ffmpeg download failed. Install it yourself from https://ffmpeg.org
)
exit /b 0
