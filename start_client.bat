@echo off
cd /d "%~dp0"
setlocal enabledelayedexpansion
title VDDMon Client Launcher

:: Sentinel loop guard: if invoked with ELEVATED argument, skip re-elevation check
set "IS_ELEVATED=0"
if /i "%~1"=="ELEVATED" (
    set "IS_ELEVATED=1"
    shift
)

set "PASSTHROUGH_ARGS="
for %%A in (%*) do (
    if /i not "%%~A"=="ELEVATED" (
        set "PASSTHROUGH_ARGS=!PASSTHROUGH_ARGS! %%A"
    )
)

echo ===================================================================
echo                     VDDMON CLIENT LAUNCHER
echo ===================================================================

:: Detect Python executable (preserves access under elevated admin environments)
set "PY_EXE="
where python >nul 2>&1
if !errorLevel! equ 0 set "PY_EXE=python"
if not defined PY_EXE (
    if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    if exist "C:\Python310\python.exe" set "PY_EXE=C:\Python310\python.exe"
)
if not defined PY_EXE set "PY_EXE=python"

set "WG_EXE="
if exist "%ProgramFiles%\WireGuard\wireguard.exe" set "WG_EXE=%ProgramFiles%\WireGuard\wireguard.exe"
if not defined WG_EXE if exist "%ProgramFiles(x86)%\WireGuard\wireguard.exe" set "WG_EXE=%ProgramFiles(x86)%\WireGuard\wireguard.exe"
if not defined WG_EXE (
    for /f "delims=" %%I in ('where wireguard.exe 2^>nul') do (
        if not defined WG_EXE set "WG_EXE=%%I"
    )
)

set "TUNNEL_STARTED=0"
set "WG_ACTIVE=0"
set "TUNNEL_NAME="
set "CONF_FILE="

:: If WireGuard is not installed, proceed in direct LAN mode without Admin
if not defined WG_EXE (
    echo [i] WireGuard not detected on system.
    echo [*] Launching VDDMon Client in direct LAN mode...
    goto :LAUNCH_CLIENT
)

:: Detect WireGuard client configuration with absolute paths
if exist "%~dp0wireguard\client.conf" set "CONF_FILE=%~dp0wireguard\client.conf"
if not defined CONF_FILE if exist "%~dp0client_wg.conf" set "CONF_FILE=%~dp0client_wg.conf"
if not defined CONF_FILE if exist "%~dp0client.conf" set "CONF_FILE=%~dp0client.conf"
if not defined CONF_FILE if exist "%~dp0wg0.conf" set "CONF_FILE=%~dp0wg0.conf"
if not defined CONF_FILE (
    for %%F in ("%~dp0wireguard\*.conf") do (
        if /i not "%%~nxF"=="server.conf.example" if /i not "%%~nxF"=="client.conf.example" (
            if not defined CONF_FILE set "CONF_FILE=%%~fF"
        )
    )
)

:: If WireGuard is installed but no client config found, proceed in direct LAN mode without Admin
if not defined CONF_FILE (
    echo [+] WireGuard detected at: "!WG_EXE!"
    echo [i] No client configuration file [client.conf] found in .\wireguard\
    echo [*] Tip: Run install.bat to auto-configure WireGuard VPN and virtual displays.
    echo [*] If WireGuard is active via the WireGuard GUI, it will route automatically.
    echo [*] Launching VDDMon Client GUI...
    goto :LAUNCH_CLIENT
)

for %%F in ("!CONF_FILE!") do set "TUNNEL_NAME=%%~nF"
echo [*] WireGuard configuration: !CONF_FILE!
echo [*] WireGuard tunnel name  : !TUNNEL_NAME!

:: Safeguard 3: Check if tunnel is already active or registered
sc query "WireGuardTunnel$!TUNNEL_NAME!" >nul 2>&1
if !errorLevel! equ 0 (
    sc query "WireGuardTunnel$!TUNNEL_NAME!" 2>nul | findstr /i "STATE" | findstr /i "RUNNING" >nul
    if !errorLevel! equ 0 (
        echo [+] WireGuard tunnel 'WireGuardTunnel$!TUNNEL_NAME!' is already RUNNING.
        set "WG_ACTIVE=1"
        goto :LAUNCH_CLIENT
    )
)

:: Elevate if tunnel service needs start or install
if "!IS_ELEVATED!"=="1" goto :SKIP_CLIENT_ELEVATION

fltmc >nul 2>&1
if !errorLevel! neq 0 (
    echo [*] Elevating to activate WireGuard tunnel service...
    powershell -NoProfile -Command "Start-Process '%~f0' -ArgumentList 'ELEVATED %*' -WorkingDirectory '%~dp0' -Verb RunAs"
    if !errorLevel! equ 0 exit /b
    echo.
    echo [i] Administrator elevation not granted. Continuing in Direct LAN mode...
    goto :LAUNCH_CLIENT
)
:SKIP_CLIENT_ELEVATION

:: Running elevated: Start existing stopped service or install new tunnel service
sc query "WireGuardTunnel$!TUNNEL_NAME!" >nul 2>&1
if !errorLevel! equ 0 (
    echo [*] WireGuard tunnel service exists but was stopped. Starting service...
    net start "WireGuardTunnel$!TUNNEL_NAME!" >nul 2>&1
) else (
    echo [*] Installing and starting WireGuard tunnel '!TUNNEL_NAME!'...
    "!WG_EXE!" /installtunnelservice "!CONF_FILE!"
)
ping -n 3 127.0.0.1 >nul 2>&1
set "TUNNEL_STARTED=1"
set "WG_ACTIVE=1"
echo [+] WireGuard tunnel '!TUNNEL_NAME!' activated!

:LAUNCH_CLIENT
echo.
set "TARGET_HOST="
for %%A in (!PASSTHROUGH_ARGS!) do (
    if not defined TARGET_HOST (
        set "A1=%%~A"
        if not "!A1:~0,1!"=="-" if not "!A1:~0,1!"=="/" set "TARGET_HOST=!A1!"
    )
)
if not defined TARGET_HOST if "!WG_ACTIVE!"=="1" set "TARGET_HOST=10.0.0.1:5900"

echo [*] Launching VDDMon Client GUI...
if defined TARGET_HOST (
    echo [*] Target host: !TARGET_HOST!
    start "" pythonw vddmon_client.py !TARGET_HOST! >nul 2>&1
) else (
    start "" pythonw vddmon_client.py >nul 2>&1
)
if !errorLevel! neq 0 (
    if defined TARGET_HOST (
        start "" "%PY_EXE%" vddmon_client.py !TARGET_HOST! >nul 2>&1
    ) else (
        start "" "%PY_EXE%" vddmon_client.py >nul 2>&1
    )
)

exit /b
