#!/usr/bin/env python3
"""vddmon_qr.py — Standalone Terminal QR Pairing Utility.

Reads authentication credentials and ports, discovers active network interfaces
(prioritizing WireGuard 10.0.0.x and local LAN 192.168.x.x), renders an ANSI
QR code directly in the terminal, and displays raw connection endpoints.
"""

import sys
import os
import json
import socket
from pathlib import Path

# Reconfigure stdout to UTF-8 for crisp Unicode ANSI block rendering on Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    import qrcode
except ImportError:
    print("[ERROR] 'qrcode' module not found. Install via: pip install qrcode")
    sys.exit(1)

def get_base_dir() -> Path:
    """Returns the vddmon storage directory."""
    return Path(os.getenv("VDDMON_HOME", Path.home() / ".vddmon"))

def load_connection_params():
    """Extracts port and credentials from auth.json and config.json."""
    base = get_base_dir()
    port = 5900
    pw = ""

    # Check config.json for port
    cfg_file = base / "config.json"
    if cfg_file.exists():
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
            port = int(cfg.get("port", port))
        except Exception:
            pass

    # Check auth.json for credentials
    auth_candidates = [base / "auth.json", Path(__file__).resolve().parent / "auth.json"]
    for af in auth_candidates:
        if af.exists():
            try:
                data = json.loads(af.read_text(encoding="utf-8"))
                if "port" in data:
                    port = int(data["port"])
                if "pw" in data:
                    pw = data["pw"]
                break
            except Exception:
                pass

    return port, pw

def discover_network_ips():
    """Discovers and categorizes all active local, IPv6, and VPN network interfaces."""
    interfaces = []
    seen = set()
    try:
        import psutil
        for if_name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family == socket.AF_INET:
                    ip = a.address
                    if ip.startswith("127."):
                        continue
                    is_wg = any(k in if_name.lower() for k in ("wg", "wireguard", "server", "client"))
                    if ip.startswith("169.254.") and not is_wg:
                        continue
                    if ip not in seen:
                        seen.add(ip)
                        interfaces.append((if_name, ip, False, is_wg))
                elif a.family == socket.AF_INET6:
                    raw_ip = a.address.split("%")[0]
                    if raw_ip in ("::1", "::"):
                        continue
                    is_wg = any(k in if_name.lower() for k in ("wg", "wireguard", "server", "client"))
                    if raw_ip.lower().startswith("fe80:") and not is_wg:
                        continue
                    if raw_ip not in seen:
                        seen.add(raw_ip)
                        interfaces.append((if_name, raw_ip, True, is_wg))
    except Exception:
        pass

    if not interfaces:
        interfaces.append(("Loopback", "127.0.0.1", False, False))

    # Priority sorting for QR pairing: Global IPv6 > WireGuard > Local LAN 192.168.x > Other
    def ip_priority(entry):
        iface, ip, is_v6, is_wg = entry
        # Prioritize Global/Routable IPv6
        if is_v6 and not ip.lower().startswith("fe80:") and not ip.lower().startswith("::"):
            return 0
        if is_wg or ip.startswith("10.0.0."):
            return 1
        if is_v6:
            return 2
        if ip.startswith("192.168."):
            return 3
        if ip.startswith("172.") or ip.startswith("10."):
            return 4
        return 5

    interfaces.sort(key=ip_priority)
    return interfaces

def main():
    port, _ = load_connection_params()
    ifaces = discover_network_ips()

    primary_name, primary_ip, is_v6, _ = ifaces[0]
    ep_str = f"[{primary_ip}]:{port}" if is_v6 else f"{primary_ip}:{port}"
    uri = f"vddmon://{ep_str}"

    print("=" * 62)
    print("         vddmon Terminal QR Pairing Utility")
    print("=" * 62)
    print()

    # Generate and print ANSI QR Code with robust fallback
    try:
        qr = qrcode.QRCode(border=1)
        qr.add_data(uri)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:
        try:
            qr = qrcode.QRCode(border=1)
            qr.add_data(uri)
            qr.make(fit=True)
            matrix = qr.get_matrix()
            for row in matrix:
                print("".join("##" if cell else "  " for cell in row))
        except Exception as ex:
            print(f"[!] Could not render ASCII QR: {ex}")

    print()
    print(f"Primary Pairing URI: {uri}")
    print(f"Target Interface:    {primary_name} ({ep_str})")
    print()
    print("Available Connection Endpoints:")
    for iface, ip, v6, wg in ifaces:
        curr_ep = f"[{ip}]:{port}" if v6 else f"{ip}:{port}"
        tag = "[WIREGUARD]" if wg else ("[IPv6]" if v6 else "[LAN/DIRECT]")
        print(f"  • {tag:<14} {iface:<22} -> {curr_ep} (vddmon://{curr_ep})")
    print("=" * 62)

    sys.exit(0)

if __name__ == "__main__":
    main()
