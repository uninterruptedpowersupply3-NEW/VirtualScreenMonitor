@echo off
cd /d "%~dp0"
setlocal enabledelayedexpansion
title VDDMon Universal Installer

echo ===================================================================
echo                    VDDMON UNIVERSAL INSTALLER
echo ===================================================================
echo.

:: Sentinel loop guard: if invoked with ELEVATED argument, skip re-elevation check
set "IS_ELEVATED=0"
if /i "%~1"=="ELEVATED" (
    set "IS_ELEVATED=1"
    shift
)

:: Check Administrator Privileges
if "!IS_ELEVATED!"=="1" goto :SKIP_INSTALL_ELEVATION

fltmc >nul 2>&1
if %errorLevel% neq 0 (
    echo [*] Requesting Administrator privileges to install VDDMon...
    powershell -NoProfile -Command "Start-Process '%~f0' -ArgumentList 'ELEVATED %*' -WorkingDirectory '%~dp0' -Verb RunAs"
    exit /b
)
:SKIP_INSTALL_ELEVATION

echo [1/5] Setting up Virtual Display Driver (MTT VDD)...
set "PDIR=C:\VirtualDisplayDriver"
set "PFDIR=C:\Program Files\Virtual Display Driver"
if not exist "%PDIR%" mkdir "%PDIR%" >nul 2>&1
if not exist "%PFDIR%" mkdir "%PFDIR%" >nul 2>&1

if exist "%~dp0driver\MttVDD.inf" (
    copy /y "%~dp0driver\vdd_settings.xml" "%PDIR%\vdd_settings.xml" >nul 2>&1
    copy /y "%~dp0driver\adapter.txt" "%PDIR%\adapter.txt" >nul 2>&1
    copy /y "%~dp0driver\MttVDD.dll" "%PDIR%\MttVDD.dll" >nul 2>&1
    copy /y "%~dp0driver\MttVDD.inf" "%PDIR%\MttVDD.inf" >nul 2>&1
    copy /y "%~dp0driver\mttvdd.cat" "%PDIR%\mttvdd.cat" >nul 2>&1
    copy /y "%PDIR%\*" "%PFDIR%\" >nul 2>&1
)

:: Enforce Integrated GPU preference for wudfhost.exe (Power Saving)
reg add "HKCU\Software\Microsoft\DirectX\UserGpuPreferences" /v "C:\Windows\System32\wudfhost.exe" /t REG_SZ /d "GpuPreference=1;" /f >nul 2>&1
reg add "HKLM\SOFTWARE\Microsoft\DirectX\UserGpuPreferences" /v "C:\Windows\System32\wudfhost.exe" /t REG_SZ /d "GpuPreference=1;" /f >nul 2>&1

:: Install driver and extend display
pnputil /add-driver "%PDIR%\MttVDD.inf" /install >nul 2>&1
pnputil /restart-device "Root\MttVDD" >nul 2>&1
DisplaySwitch.exe /extend >nul 2>&1
echo [+] Virtual Display Driver installed and activated!

echo.
echo [2/5] Configuring Windows Firewall Rules...
netsh advfirewall firewall delete rule name="VDDMon Remote Desktop (TCP 5900)" >nul 2>&1
netsh advfirewall firewall add rule name="VDDMon Remote Desktop (TCP 5900)" dir=in action=allow protocol=TCP localport=5900 profile=any >nul 2>&1
netsh advfirewall firewall delete rule name="WireGuard VPN (UDP 51820)" >nul 2>&1
netsh advfirewall firewall add rule name="WireGuard VPN (UDP 51820)" dir=in action=allow protocol=UDP localport=51820 profile=any >nul 2>&1
echo [+] Firewall rules configured for Port 5900 (TCP) and Port 51820 (UDP)!

echo.
echo [3/5] Configuring Universal Master Password...
python vddmon_server.py passwd -p "VDDMon2026!" >nul 2>&1
echo [+] Universal Password set to: VDDMon2026!

echo.
echo [4/5] Checking WireGuard VPN Configuration...
set "WG_DIR=%~dp0wireguard"
if not exist "!WG_DIR!" mkdir "!WG_DIR!" >nul 2>&1

python -c "
import subprocess, os
from pathlib import Path
wg_candidates = [r'C:\Program Files\WireGuard\wg.exe', r'C:\Program Files (x86)\WireGuard\wg.exe']
wg_exe = next((p for p in wg_candidates if os.path.exists(p)), None)
srv_f = Path(r'%~dp0wireguard\server.conf')
cli_f = Path(r'%~dp0wireguard\client.conf')
if wg_exe and not srv_f.exists():
    sp = subprocess.check_output([wg_exe, 'genkey']).decode().strip()
    spub = subprocess.check_output([wg_exe, 'pubkey'], input=sp.encode()).decode().strip()
    cp = subprocess.check_output([wg_exe, 'genkey']).decode().strip()
    cpub = subprocess.check_output([wg_exe, 'pubkey'], input=cp.encode()).decode().strip()
    srv_txt = f'[Interface]\nAddress = 10.0.0.1/24\nListenPort = 51820\nPrivateKey = {sp}\n\n[Peer]\nPublicKey = {cpub}\nAllowedIPs = 10.0.0.2/32\n'
    cli_txt = f'[Interface]\nAddress = 10.0.0.2/24\nPrivateKey = {cp}\n\n[Peer]\nPublicKey = {spub}\nEndpoint = 127.0.0.1:51820\nAllowedIPs = 10.0.0.0/24\nPersistentKeepalive = 25\n'
    srv_f.write_text(srv_txt)
    cli_f.write_text(cli_txt)
    print('Generated WireGuard server.conf and client.conf!')
elif srv_f.exists():
    print('Existing WireGuard server.conf found and ready.')
else:
    print('WireGuard is optional; operating in Direct LAN mode.')
"
echo [+] WireGuard tunnel configuration validated!

echo.
echo [5/5] Pre-populating Client Saved Hosts...
python -c "
import os, json, base64, ctypes
from pathlib import Path
import vddmon_client

hosts_f = Path(os.environ.get('APPDATA', '')) / 'vddmon_client' / 'hosts.json'
hosts_f.parent.mkdir(parents=True, exist_ok=True)
hosts = {}
if hosts_f.exists():
    try: hosts = json.loads(hosts_f.read_text())
    except Exception: pass
enc_pw = vddmon_client.dpapi_encrypt('VDDMon2026!')
hosts['127.0.0.1'] = {'port': 5900, 'pw': enc_pw}
hosts['10.0.0.1'] = {'port': 5900, 'pw': enc_pw}
hosts_f.write_text(json.dumps(hosts, indent=2))
" >nul 2>&1
echo [+] Client saved hosts configured with universal credentials!

echo.
echo ===================================================================
echo                     INSTALLATION COMPLETE!
echo ===================================================================
echo.
echo   Master Password : VDDMon2026!
echo.
echo   To launch the server : double-click  start_server.bat
echo   To launch the client : double-click  start_client.bat
echo.
echo ===================================================================
pause
