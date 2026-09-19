@echo off
title Zerko File Manager
color 0B
cd /d "%~dp0"

echo.
echo   ZERKO FILE MANAGER
echo   ==================
echo.

REM ---- WSL present AND has a Linux installed? ---------------------------
REM `wsl --status` succeeds even when NO distribution is installed, which is a
REM very common state - the feature is switched on but Ubuntu was never added.
REM So test for an actual distribution instead.
where wsl >nul 2>&1
if errorlevel 1 goto :no_wsl

REM The definitive test: can we actually run a command inside Linux?
REM (Parsing `wsl -l -q` is unreliable - it returns UTF-16, which batch
REM  misreads as garbage or as empty depending on the Windows build.)
wsl -- true >nul 2>&1
if errorlevel 1 goto :no_distro
goto :wsl_ok

:no_wsl
echo   Windows Subsystem for Linux is not installed.
echo.
echo   1. Right-click the Start button, choose "Terminal (Admin)"
echo   2. Run this:
echo.
echo        wsl --install -d Ubuntu
echo.
echo   3. RESTART your PC
echo   4. Ubuntu opens and asks you to make a username and password.
echo      WRITE THAT PASSWORD DOWN - you will need it here.
echo   5. Run this file again.
echo.
pause
exit /b 1

:no_distro
echo   WSL is switched on, but no Linux is installed yet.
echo   ^(This is the usual state - the feature exists, Ubuntu was never added.^)
echo.
echo   1. Right-click the Start button, choose "Terminal (Admin)"
echo   2. Run this:
echo.
echo        wsl --install -d Ubuntu
echo.
echo   3. RESTART your PC if it asks you to
echo   4. Ubuntu opens and asks you to make a username and password.
echo      WRITE THAT PASSWORD DOWN - you will need it here.
echo   5. Run this file again.
echo.
echo   To see what else you could install:  wsl --list --online
echo.
pause
exit /b 1

:wsl_ok

REM Work out where this folder is, in WSL terms
set "HEREWIN=%~dp0"
set "HEREWIN=%HEREWIN:~0,-1%"
for /f "usebackq delims=" %%i in (`wsl wslpath -a "%HEREWIN%" 2^>nul`) do set "HERE=%%i"
if "%HERE%"=="" (
    echo   [!] Could not read this folder from WSL.
    echo       Make sure the folder is on a normal drive ^(C:, D: ...^).
    pause
    exit /b 1
)

REM ---- first run: dependencies ------------------------------------------
REM Self-heal a stale marker. An earlier version wrote .setup-done even when
REM setup had failed, which made every later start skip setup and fail in a
REM more confusing way. If the marker is there but the environment is not,
REM ignore the marker and set up properly.
if exist "%~dp0.setup-done" (
    wsl -- bash -lc "cd '%HERE%' && test -x venv/bin/python" >nul 2>&1
    if errorlevel 1 (
        echo   Previous setup did not finish - starting it again.
        echo.
        del "%~dp0.setup-done" >nul 2>&1
    )
)

if not exist "%~dp0.setup-done" (
    echo   First run - installing what Zerko needs inside WSL.
    echo   This takes a few minutes. You only wait once.
    echo.
    wsl -- bash -lc "cd '%HERE%' && chmod +x *.sh deploy/*.sh 2>/dev/null; ./first-run.sh"

    REM Trust the result, not the exit code: check the thing setup was
    REM supposed to create. A marker file written after a failed run would
    REM make every future start skip setup and fail in a more confusing way.
    wsl -- bash -lc "cd '%HERE%' && test -x venv/bin/python" >nul 2>&1
    if errorlevel 1 (
        echo.
        echo   [!] Setup did not finish - the Python environment was not created.
        echo       Scroll up for the reason. Common causes:
        echo         - no internet connection
        echo         - wrong WSL password when it asked
        echo.
        pause
        exit /b 1
    )
    echo done > "%~dp0.setup-done"
    echo.
)

echo   Starting...
echo   Keep this window open while you use Zerko.
echo.

start "" /b cmd /c "timeout /t 8 >nul & start http://localhost:9600"
wsl -- bash -lc "cd '%HERE%' && ./start.sh"

echo.
echo   Zerko has stopped.
pause
