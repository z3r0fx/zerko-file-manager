@echo off
title Zerko - firewall
net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo   [!] Right-click this file and choose "Run as administrator".
    echo.
    pause
    exit /b 1
)

echo.
echo   Opening inbound ports for Zerko File Manager
echo   ============================================
echo.

netsh advfirewall firewall delete rule name="Zerko File Manager" >nul 2>&1
netsh advfirewall firewall add rule name="Zerko File Manager" dir=in action=allow protocol=TCP localport=80,443,9443,9600
echo.
echo   Allowed inbound TCP 80, 443, 9443 and 9600.
echo.
echo   Next, on your router, forward this to YOUR-PC-IP:
echo      9443 -^> 9443    (this is the HTTPS port people actually use)
echo.
echo   Ports 80 and 443 are blocked by the ISP on this line, which is why
echo   HTTPS runs on 9443 and the certificate is proved via DNS instead.
echo.
echo   Once 9443 works, DELETE the 9600 forward - it is the unencrypted door.
echo.
pause
