@echo off
cd /d "%~dp0"
setlocal enabledelayedexpansion
title VDDMon Server Launcher

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
echo                     VDDMON SERVER LAUNCHER
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
set "TUNNEL_NAME="
set "CONF_FILE="
set "TUNNEL_NEEDED=0"

:: Check if Virtual Display Driver is disabled
set "VDD_DISABLED=0"
if exist "%~dp0driver\devcon.exe" (
    "%~dp0driver\devcon.exe" status "Root\MttVDD*" 2>nul | findstr /i "disabled" >nul
    if !errorLevel! equ 0 set "VDD_DISABLED=1"
)

:: If WireGuard executable not found, skip tunnel setup without requesting Admin
if not defined WG_EXE (
    echo [i] WireGuard not detected on system.
    echo [*] Launching VDDMon server in direct LAN mode...
    if "!VDD_DISABLED!"=="1" goto :CHECK_ADMIN
    goto :SHOW_IPS
)

:: Detect WireGuard server configuration with absolute paths
if exist "%~dp0wireguard\server.conf" set "CONF_FILE=%~dp0wireguard\server.conf"
if not defined CONF_FILE if exist "%~dp0server_wg.conf" set "CONF_FILE=%~dp0server_wg.conf"
if not defined CONF_FILE if exist "%~dp0server.conf" set "CONF_FILE=%~dp0server.conf"
if not defined CONF_FILE if exist "%~dp0wg0.conf" set "CONF_FILE=%~dp0wg0.conf"
if not defined CONF_FILE (
    for %%F in ("%~dp0wireguard\*.conf") do (
        if /i not "%%~nxF"=="server.conf.example" if /i not "%%~nxF"=="client.conf.example" (
            if not defined CONF_FILE set "CONF_FILE=%%~fF"
        )
    )
)

:: If WireGuard is installed but no config found, inform user and proceed in direct LAN mode without Admin
if not defined CONF_FILE (
    echo [+] WireGuard detected at: "!WG_EXE!"
    echo [i] No server configuration file [server.conf] found in .\wireguard\
    echo [*] Tip: Run install.bat to auto-configure WireGuard VPN and virtual displays.
    echo [*] If a tunnel is already managed via the WireGuard GUI, it will be used automatically.
    echo [*] Launching VDDMon server in direct LAN mode...
    if "!VDD_DISABLED!"=="1" goto :CHECK_ADMIN
    goto :SHOW_IPS
)

for %%F in ("!CONF_FILE!") do set "TUNNEL_NAME=%%~nF"
echo [*] WireGuard configuration: !CONF_FILE!
echo [*] WireGuard tunnel name  : !TUNNEL_NAME!

:: Check if tunnel service is already active or registered
sc query "WireGuardTunnel$!TUNNEL_NAME!" >nul 2>&1
if !errorLevel! equ 0 (
    sc query "WireGuardTunnel$!TUNNEL_NAME!" 2>nul | findstr /i "STATE" | findstr /i "RUNNING" >nul
    if !errorLevel! equ 0 (
        echo [+] WireGuard tunnel 'WireGuardTunnel$!TUNNEL_NAME!' is already RUNNING.
        if "!VDD_DISABLED!"=="0" goto :SHOW_IPS
    ) else (
        set "TUNNEL_NEEDED=1"
    )
) else (
    set "TUNNEL_NEEDED=1"
)

:CHECK_ADMIN
if "!IS_ELEVATED!"=="1" goto :SKIP_ELEVATION

fltmc >nul 2>&1
if !errorLevel! neq 0 (
    echo ===================================================================
    if "!VDD_DISABLED!"=="1" (
        echo [*] Virtual Display Driver is disabled and requires activation.
    ) else (
        echo [*] WireGuard tunnel '!TUNNEL_NAME!' requires activation.
    )
    echo [*] Requesting Administrator privileges...
    echo [*] (If UAC is declined, server will proceed in Direct LAN mode)
    echo ===================================================================
    powershell -NoProfile -Command "Start-Process '%~f0' -ArgumentList 'ELEVATED %*' -WorkingDirectory '%~dp0' -Verb RunAs"
    if !errorLevel! equ 0 exit /b
    echo.
    echo [i] Administrator elevation not granted. Continuing in Direct LAN mode...
    goto :SHOW_IPS
)
:SKIP_ELEVATION

:: Running elevated: Enable virtual displays and extend desktop
if exist "%~dp0driver\devcon.exe" (
    echo [*] Enabling Virtual Display Driver (Root\MttVDD*)...
    "%~dp0driver\devcon.exe" enable "Root\MttVDD*" >nul 2>&1
    "%~dp0driver\devcon.exe" enable "@ROOT\DISPLAY\*" >nul 2>&1
)
DisplaySwitch.exe /extend >nul 2>&1
set "VDD_DISABLED=0"

:: Running elevated: Start existing stopped service or install new tunnel service
if "!TUNNEL_NEEDED!"=="1" if defined CONF_FILE (
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
    echo [+] WireGuard tunnel '!TUNNEL_NAME!' activated!
)

:SHOW_IPS
echo.
echo -------------------------------------------------------------------
echo Active Network Endpoints:
"%PY_EXE%" vddmon_server.py ip
echo -------------------------------------------------------------------
echo.

:LAUNCH_SERVER
echo [*] Starting VDDMon server daemon on Port 5900...
echo [*] (Press Ctrl+C in this console to stop the server)
echo.

"%PY_EXE%" vddmon_server.py run !PASSTHROUGH_ARGS!
set "RUN_EXIT=!errorLevel!"

if "!RUN_EXIT!"=="2" (
    echo.
    echo [i] VDDMon server is already running on Port 5900 in another process.
    echo [i] Active streaming session and virtual displays remain undisturbed.
    echo.
    pause
    exit /b 0
)

if "!RUN_EXIT!" neq "0" (
    echo [!] Server process exited with code !RUN_EXIT!.
)

echo.
echo [*] Cleaning up virtual displays and reverting to primary screen...
DisplaySwitch.exe /internal >nul 2>&1
if exist "%~dp0driver\devcon.exe" (
    "%~dp0driver\devcon.exe" disable "Root\MttVDD*" >nul 2>&1
)

if "%TUNNEL_STARTED%"=="1" (
    echo.
    echo [*] Stopping WireGuard tunnel '!TUNNEL_NAME!'...
    net stop "WireGuardTunnel$!TUNNEL_NAME!" >nul 2>&1
    echo [+] WireGuard tunnel stopped.
)

pause
