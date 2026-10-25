@echo off
REM PakData residential egress tunnel.
REM Opens a reverse dynamic SOCKS proxy on the VPS (127.0.0.1:1080) that egresses
REM through THIS machine's residential IP, so the VPS scheduler can refresh the
REM Cloudflare-IP-blocked sources (SBP EasyData, sbp.org.pk) that block datacenter
REM and WARP IPs. MUFAP stays on WARP (always-on) and does not need this.
REM
REM Auto-reconnects. Register to run at logon:
REM   schtasks /create /tn PakDataTunnel /tr "%~f0" /sc onlogon /rl highest /f
setlocal
set KEY=%USERPROFILE%\.ssh\pakdata_deploy
set VPS=root@200.234.34.97

:loop
echo [%date% %time%] connecting reverse SOCKS tunnel...
ssh -i "%KEY%" -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o StrictHostKeyChecking=accept-new -N -R 1080 %VPS%
echo [%date% %time%] tunnel dropped; reconnecting in 10s...
timeout /t 10 /nobreak >nul
goto loop
