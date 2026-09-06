# VirtualScreenMonitor

**VirtualScreenMonitor** is a high-performance, ultra-low-latency remote display streaming application for Windows. It provides seamless desktop streaming of physical monitors and secondary virtual displays with **less than 100ms latency**.

<img width="1024" height="575" alt="screenshot" src="https://github.com/user-attachments/assets/fcdcf192-99af-4c09-acff-f381e241516a" />

---

## Key Features

- **Ultra-Low Latency (<100ms):** Optimized dual-engine streaming pipeline combining sparse-tile TurboJPEG differencing and Intel QuickSync (QSV) hardware H.264 encoding.
- **Virtual Display Driver Integration:** Automatically manages and streams secondary virtual screens (using the IddCx Virtual Display Driver) even on headless systems without a physical monitor attached.
- **Hardware Cursor & Input Confinement:** Mouse cursor is strictly confined to physical bounds. Hovering over the client canvas does not warp the physical cursor, and window dragging is smooth with zero monitor-strobing.
- **Zero-FFmpeg Native Decoding:** Fast, in-process C decoder using Windows Media Foundation (`CLSID_CMSH264DecoderMFT`) with tight pitch and stride alignment.

---

## System Requirements & Testing Environment

- **Tested OS:** Developed and verified on **Windows 11 Home (64-bit, Version 25H2, Build 26200)**.
- **Security Notice:** Achieving ultra-low latency (<100ms) and high-performance video streaming was the primary objective of this project rather than hardened enterprise security. Although password authentication and DPAPI credential protection are present, security was not the main focus of this application. Use within trusted local area networks (LAN) or over a private, secured VPN (such as WireGuard).

---

## Quick Start (How to Use the App)

### 1. First-Time Setup (Host / Server)
Run `install.bat` as Administrator to install the Virtual Display Driver, open Windows Firewall ports (TCP 5900), and configure initial credentials:
```cmd
install.bat
```

### 2. Start the Server
Run the batch launcher or executable:
```cmd
start_server.bat
```
*(Or manually: `python vddmon_server.py run` or `VirtualScreenMonitor_Server.exe run`)*

#### Server Command-Line Arguments & Commands
Both `vddmon_server.py` and the compiled server executables (`VirtualScreenMonitor_Server.exe` / `VirtualScreenMonitor_Server_Headless.exe`) support the following commands and arguments:

```cmd
VirtualScreenMonitor_Server.exe [command] [options]
# Or with Python:
python vddmon_server.py [command] [options]
```

##### Commands:
| Command | Description |
|---|---|
| `run` *(default)* | Run the VDDMon server streaming daemon. |
| `pair` | Generate an ephemeral P2P pairing token and start the server. |
| `passwd -p <password>` | Set or update the server master authentication password. |
| `ip` | List all detected local, LAN, and WireGuard connection IP addresses. |
| `qr` | Render a connection and pairing ASCII QR code in the terminal. |
| `install` | Configure autostart service and initialize display drivers. |
| `uninstall` | Clean up virtual displays and remove background autostart tasks. |

##### Options & Flags:
| Flag | Description | Default |
|---|---|---|
| `--host <IP>`, `--bind <IP>` | Listening IP address (e.g. `0.0.0.0` for all, `10.0.0.1` for WireGuard, `127.0.0.1` for local loopback). | `0.0.0.0` |
| `--port <PORT>`, `-p <PORT>` | Listening TCP port. | `5900` |
| `--monitor <INDEX>`, `-m <INDEX>` | Monitor index to capture (e.g. `0` for primary physical display). | Auto-detect virtual display |
| `--pair` | Generate ephemeral P2P pairing token and listen. | Disabled |

---

### 3. Start the Client
Run the client launcher or executable:
```cmd
start_client.bat
```
*(Or manually: `python vddmon_client.py` or `VirtualScreenMonitor_Client.exe`)*

Enter the host IP address (e.g. `127.0.0.1` for local testing or your LAN/WireGuard IP) and click **Connect**.

---

## File Overview (What Each File Does)

| File / Directory | Description |
|---|---|
| `vddmon_server.py` | Core server daemon: captures desktop frames (DXGI / GDI), manages encoding engines, enforces cursor boundaries, handles network connections, and dispatches input. |
| `vddmon_client.py` | Client GUI application (Tkinter): provides the stream viewport canvas, toolbar controls, real-time telemetry HUD, and local input capture. |
| `qsv_encoder.c` | Native C library that hooks into Intel QuickSync Video via the Intel Media SDK for hardware H.264 encoding. |
| `mft_decoder.c` | Native C library that decodes H.264 frames using Windows Media Foundation with zero-copy scanline stride alignment. |
| `mfx/` | Intel Media SDK header files required to compile `qsv_encoder.c` (MIT licensed). |
| `driver/` | Virtual Display Driver package (`MttVDD.inf`, `MttVDD.dll`, `vdd_settings.xml`) for creating secondary virtual monitors. |
| `wireguard/` | Configuration templates (`server.conf.example`, `client.conf.example`) for encrypted peer-to-peer WAN streaming. |
| `overrides.py` | Hotkey override handler and Windows key suppression helper for remote desktop control. |
| `vddmon_p2p.py` | Peer-to-peer NAT traversal and pairing utility. |
| `vddmon_qr.py` | QR-code pairing generation for mobile or fast remote pairing. |
| `install.bat` | 1-Click setup script: installs the virtual display driver, sets up firewall rules, and initializes host configurations. |
| `start_server.bat` | Convenience batch launcher to start the server. |
| `start_client.bat` | Convenience batch launcher to start the client. |
| `vddmon_server.spec` | PyInstaller specification to build both normal (`vddmon_server.exe`) and headless (`vddmon_server_headless.exe`) server binaries. |
| `vddmon_client.spec` | PyInstaller specification to bundle `vddmon_client.py` and `mft_decoder.dll` into a standalone client GUI executable. |

---

## What You Need to Compile

Before running or packaging, the following components must be compiled:

### 1. Native C Dynamic Libraries (`.dll`)
Open the **x64 Native Tools Command Prompt for VS 2022** (or MSVC compiler environment) in the project directory and run:

* **Compile Intel QSV Encoder (`qsv_encoder.dll`):**
  ```cmd
  cl.exe /O2 /LD /arch:AVX /I. qsv_encoder.c /link /DLL /OUT:qsv_encoder.dll
  ```

* **Compile Media Foundation Decoder (`mft_decoder.dll`):**
  ```cmd
  cl.exe /O2 /LD /arch:AVX mft_decoder.c mfplat.lib mfuuid.lib /link /DLL /OUT:mft_decoder.dll
  ```

### 2. Standalone Executables (Optional)
To package the Python scripts into standalone `.exe` files:
```cmd
pip install pyinstaller
python -m PyInstaller --clean vddmon_server.spec
python -m PyInstaller --clean vddmon_client.spec
```
The compiled binaries will be output to the `dist/` directory:
- `dist/vddmon_server.exe` (Normal server with visible console)
- `dist/vddmon_server_headless.exe` (Headless background server; attaches to terminal when invoked from CLI)
- `dist/vddmon_client.exe` (Client GUI with bundled `mft_decoder.dll`)

---

## Third-Party Credits & Notices
- **Virtual Display Driver**: Developed by [MikeTheTech](https://github.com/VirtualDrivers/Virtual-Display-Driver) (MIT License).
- **Intel Media SDK Headers**: Copyright (c) Intel Corporation (MIT License).
