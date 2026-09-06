"""
VDDMon P2P Network & Cryptographic Transport Engine
Provides zero-configuration, serverless P2P streaming across disparate residential NATs:
- Dual-stack router pinholing (Global IPv6 2000::/3 + UPnP/PCP IGD mapping)
- Single-use Ephemeral Pairing Tokens (X25519 + 16B PSK + 120s TTL)
- Terminal ASCII QR code rendering
- Silent-Drop server protection (Zero banners, mandatory first-packet HMAC proof)
- Client Happy Eyeballs (RFC 8305) socket racing with Winsock error handling
- End-to-end ChaCha20Poly1305 AEAD encryption with 96-bit nonces and AAD framing protection
"""

import sys
import os
import time
import socket
import struct
import secrets
import hmac
import hashlib
import base64
import urllib.parse
import ipaddress
import atexit
import signal
import select
import threading

from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

try:
    import qrcode
except ImportError:
    qrcode = None

try:
    import miniupnpc
except ImportError:
    miniupnpc = None

# Global registry for active UPnP leases for automatic router teardown
_ACTIVE_UPNP_MAPPINGS = []

def _cleanup_upnp():
    global _ACTIVE_UPNP_MAPPINGS
    for u, port in list(_ACTIVE_UPNP_MAPPINGS):
        try:
            u.deleteportmapping(port, "TCP")
            print(f"[*] P2P: Released UPnP port mapping for TCP port {port}")
        except Exception:
            pass
    _ACTIVE_UPNP_MAPPINGS = []

atexit.register(_cleanup_upnp)
try:
    signal.signal(signal.SIGINT, lambda s, f: (_cleanup_upnp(), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda s, f: (_cleanup_upnp(), sys.exit(0)))
except Exception:
    pass


# ---------------------------------------------------------------- Network Discovery & Pinholing
def get_global_ipv6():
    """Discovers the active global unicast IPv6 address (2000::/3) via kernel routing table."""
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        # Dummy connect to Google Public DNS IPv6 (does not transmit packets)
        s.connect(('2001:4860:4860::8888', 53))
        ip = s.getsockname()[0]
        s.close()
        addr = ipaddress.IPv6Address(ip)
        if addr.is_global and not addr.is_link_local and not addr.is_multicast:
            return str(addr)
    except Exception:
        pass

    # Fallback: scan interfaces
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6)
        for info in infos:
            ip = info[4][0].split("%")[0]
            addr = ipaddress.IPv6Address(ip)
            if addr.is_global and not addr.is_link_local and not addr.is_multicast:
                return str(addr)
    except Exception:
        pass
    return None

def is_cgnat_or_private(ip_str):
    """Detects RFC 6598 CGNAT (100.64.0.0/10) and RFC 1918 private IPv4 spaces."""
    try:
        ip = ipaddress.IPv4Address(ip_str)
        cgnat_net = ipaddress.IPv4Network("100.64.0.0/10")
        if ip in cgnat_net:
            return True, "RFC 6598 CGNAT (100.64.0.0/10)"
        if ip.is_private:
            return True, "RFC 1918 Private Network"
        return False, "Public Routable IPv4"
    except Exception:
        return False, "Unknown"

def discover_upnp_port(internal_port=5900, lease_duration=300):
    """Discovers UPnP IGD and requests a temporary TCP port mapping with automatic teardown."""
    if not miniupnpc:
        return None, None, False, "miniupnpc module not installed"

    u = miniupnpc.UPnP()
    u.discoverdelay = 200
    try:
        dev_count = u.discover()
        if dev_count <= 0:
            return None, None, False, "No UPnP IGD devices found on local subnet"
        u.selectigd()
        ext_ip = u.externalipaddress()
        if not ext_ip or ext_ip == "0.0.0.0":
            return None, None, False, "UPnP returned invalid external IP"

        is_cgnat, cgnat_desc = is_cgnat_or_private(ext_ip)

        # Attempt port mapping
        ext_port = internal_port
        lan_ip = u.lanaddr
        mapping_ok = False
        for p in (ext_port, ext_port + 1, ext_port + 10, ext_port + 100):
            try:
                res = u.addportmapping(p, "TCP", lan_ip, internal_port, "VDDMon P2P", "")
                if res or res is None: # miniupnpc returns True or None on success
                    ext_port = p
                    mapping_ok = True
                    break
            except Exception:
                continue

        if mapping_ok:
            _ACTIVE_UPNP_MAPPINGS.append((u, ext_port))
            return ext_ip, ext_port, is_cgnat, f"Mapped external port {ext_port} ({cgnat_desc})"
        else:
            return ext_ip, None, is_cgnat, f"UPnP IGD found but port mapping rejected ({cgnat_desc})"
    except Exception as e:
        return None, None, False, f"UPnP error: {e}"

def discover_endpoints(internal_port=5900, lease_duration=300):
    """Discovers dual-stack endpoints: Global IPv6 and UPnP IPv4."""
    v6 = get_global_ipv6()
    v4, upnp_port, is_cgnat, upnp_msg = discover_upnp_port(internal_port, lease_duration)

    if is_cgnat and not v6:
        print("[!] P2P WARNING: ISP Carrier-Grade NAT (CGNAT) detected without Global IPv6.")
        print("    Direct internet connectivity requires IPv6 or a port forward.")

    return {
        "v6": v6,
        "v4": v4,
        "upnp_port": upnp_port or internal_port,
        "is_cgnat": is_cgnat,
        "upnp_msg": upnp_msg,
        "internal_port": internal_port
    }



def get_local_lan_ip():
    """Returns the host primary LAN IP address or loopback."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

# ---------------------------------------------------------------- Ephemeral Token Generation & QR Display
class PairingSession:
    def __init__(self, v6=None, v4=None, port=5900, upnp_port=None, validity_sec=120):
        self.server_priv = x25519.X25519PrivateKey.generate()
        self.server_pub = self.server_priv.public_key()
        self.server_pub_bytes = self.server_pub.public_bytes_raw()
        self.psk = secrets.token_bytes(16)
        self.exp = int(time.time()) + int(validity_sec)
        self.v6 = v6
        self.v4 = v4
        self.port = port
        self.upnp_port = upnp_port or port
        self.consumed = False
        if not self.v6 and not self.v4:
            self.v4 = get_local_lan_ip()

        # Build URI
        q = {
            "pk": base64.urlsafe_b64encode(self.server_pub_bytes).decode().rstrip("="),
            "psk": base64.urlsafe_b64encode(self.psk).decode().rstrip("="),
            "exp": str(self.exp)
        }
        if self.v6:
            q["v6"] = f"[{self.v6}]:{self.port}"
        if self.v4:
            q["v4"] = f"{self.v4}:{self.upnp_port}"

        self.uri = f"vddmon://pair?{urllib.parse.urlencode(q)}"
        self.token_str = base64.urlsafe_b64encode(self.uri.encode("utf-8")).decode().rstrip("=")

    def is_expired(self):
        return time.time() > self.exp

    def print_qr(self):
        """Displays formatted ASCII QR code in terminal for mobile / client scanning."""
        print("\n" + "=" * 65)
        print("                 VDDMON EPHEMERAL PAIRING TOKEN")
        print("=" * 65)
        print(f"[*] Expires In: {max(0, int(self.exp - time.time()))} seconds")
        if self.v6: print(f"[+] IPv6 Endpoint: [{self.v6}]:{self.port}")
        if self.v4: print(f"[+] IPv4 Endpoint: {self.v4}:{self.upnp_port}")
        print("-" * 65)

        if qrcode:
            qr = qrcode.QRCode(border=1)
            qr.add_data(self.uri)
            qr.print_ascii(invert=True)
        else:
            print("[*] (Install 'qrcode' module for terminal ASCII QR rendering)")

        print("-" * 65)
        print("URI String:")
        print(f"  {self.uri}")
        print("\nCompact Base64 Pairing Token:")
        print(f"  {self.token_str}")
        print("=" * 65 + "\n")


# ---------------------------------------------------------------- Silent-Drop Server Handshake
def recv_exact(sock, n, timeout=2.0):
    """Accumulates exactly n bytes from sock with total timeout, handling TCP chunking."""
    sock.settimeout(timeout)
    buf = bytearray()
    t_end = time.time() + timeout
    while len(buf) < n:
        rem = t_end - time.time()
        if rem <= 0:
            return None
        sock.settimeout(rem)
        try:
            chunk = sock.recv(min(n - len(buf), 4096))
            if not chunk:
                return None
            buf.extend(chunk)
        except Exception:
            return None
    return bytes(buf)

def verify_and_handshake(sock, session: PairingSession, timeout=2.0):
    """
    Executes Silent-Drop handshake on incoming connection:
    - Zero banner sent by server.
    - Expects 72 bytes: ClientPubKey (32B) || Timestamp (8B BE uint64) || HMAC-SHA256 (32B).
    - Verifies HMAC, freshness (within 5.0s), and token expiry.
    - On ANY error or failure: closes socket immediately with 0 bytes sent.
    - On success: marks token consumed, performs X25519 ECDH, derives HKDF session key and 4-digit SAS.
    """
    if not session or session.consumed or session.is_expired():
        try: sock.close()
        except Exception: pass
        return None

    data = recv_exact(sock, 72, timeout=timeout)
    if not data or len(data) != 72:
        try: sock.close()
        except Exception: pass
        return None

    client_pub_bytes = data[:32]
    ts_bytes = data[32:40]
    client_hmac = data[40:72]

    try:
        ts = struct.unpack(">Q", ts_bytes)[0]
    except Exception:
        try: sock.close()
        except Exception: pass
        return None

    # Verify HMAC using PSK
    expected_hmac = hmac.new(session.psk, client_pub_bytes + ts_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(client_hmac, expected_hmac):
        try: sock.close()
        except Exception: pass
        return None

    # Check timestamp freshness & token expiration
    now = time.time()
    if abs(now - ts) > 5.0 or now > session.exp:
        try: sock.close()
        except Exception: pass
        return None

    # Invalidate token immediately (Single-Use Replay Protection)
    session.consumed = True

    try:
        client_pub = x25519.X25519PublicKey.from_public_bytes(client_pub_bytes)
        shared_secret = session.server_priv.exchange(client_pub)
        session_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=session.psk,
            info=b"VDDMon-V1-Session"
        ).derive(shared_secret)

        # 4-digit Short Authentication String (SAS)
        sas = int.from_bytes(hmac.new(session_key, b"SAS", hashlib.sha256).digest()[:4], "big") % 10000

        # Zero out private key reference
        session.server_priv = None

        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
        except Exception: pass

        return session_key, f"{sas:04d}"
    except Exception:
        try: sock.close()
        except Exception: pass
        return None


# ---------------------------------------------------------------- Client Happy Eyeballs Connection
def parse_pairing_token(token_str):
    """Decodes a vddmon://pair URI or base64url token string into its component parameters."""
    raw = token_str.strip()
    if not raw.startswith("vddmon://"):
        try:
            pad = len(raw) % 4
            if pad: raw += "=" * (4 - pad)
            raw = base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8")
        except Exception:
            raise ValueError("Invalid VDDMon pairing token format")

    if not raw.startswith("vddmon://pair?"):
        raise ValueError("Invalid token URI scheme; must begin with vddmon://pair?")

    parsed = urllib.parse.urlparse(raw)
    params = urllib.parse.parse_qs(parsed.query)

    if "pk" not in params or "psk" not in params or "exp" not in params:
        raise ValueError("Token missing required cryptographic parameters (pk, psk, exp)")

    pk_raw = params["pk"][0]
    pad = len(pk_raw) % 4
    if pad: pk_raw += "=" * (4 - pad)
    server_pub_bytes = base64.urlsafe_b64decode(pk_raw.encode("ascii"))

    psk_raw = params["psk"][0]
    pad = len(psk_raw) % 4
    if pad: psk_raw += "=" * (4 - pad)
    psk = base64.urlsafe_b64decode(psk_raw.encode("ascii"))

    exp = int(params["exp"][0])

    v6 = params.get("v6", [None])[0]
    v4 = params.get("v4", [None])[0]

    return {
        "server_pub_bytes": server_pub_bytes,
        "psk": psk,
        "exp": exp,
        "v6": v6,
        "v4": v4,
        "uri": raw
    }

def connect_p2p(token_str, timeout=10.0):
    """
    Connects to server using client-side Happy Eyeballs (RFC 8305):
    - Starts non-blocking connection to IPv6.
    - If not completed within 250 ms, starts concurrent non-blocking connection to IPv4.
    - Handles Windows Winsock exceptfds and SO_ERROR check.
    - Sends first-packet HMAC proof.
    - Derives matching session key and SAS code.
    """
    params = parse_pairing_token(token_str)
    if time.time() > params["exp"]:
        raise TimeoutError("Pairing token has expired (>120s)")

    v6_endpoint = params["v6"]
    v4_endpoint = params["v4"]

    if not v6_endpoint and not v4_endpoint:
        raise ValueError("Token contains no reachable IPv6 or IPv4 endpoints")

    sockets = []
    sock_info = {}

    def parse_ep(ep):
        if not ep: return None, None
        if ep.startswith("["):
            parts = ep.rsplit("]:", 1)
            host = parts[0][1:]
            port = int(parts[1]) if len(parts) > 1 else 5900
            return host, port
        else:
            parts = ep.split(":")
            host = parts[0]
            port = int(parts[1]) if len(parts) > 1 else 5900
            return host, port

    # 1. Initiate IPv6 connection
    s_v6 = None
    if v6_endpoint:
        try:
            h6, p6 = parse_ep(v6_endpoint)
            s_v6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            s_v6.setblocking(False)
            s_v6.connect_ex((h6, p6))
            sockets.append(s_v6)
            sock_info[s_v6] = ("IPv6", h6, p6)
        except Exception:
            if s_v6:
                try: s_v6.close()
                except Exception: pass
                s_v6 = None

    connected_sock = None
    t_start = time.time()

    # 2. Wait up to 250ms for IPv6 to complete
    if s_v6:
        r, w, e = select.select([], [s_v6], [s_v6], 0.25)
        if s_v6 in e:
            sockets.remove(s_v6)
            s_v6.close()
            s_v6 = None
        elif s_v6 in w:
            err = s_v6.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if err == 0:
                connected_sock = s_v6
            else:
                sockets.remove(s_v6)
                s_v6.close()
                s_v6 = None

    # 3. If IPv6 did not finish within 250ms, initiate concurrent IPv4 connection
    s_v4 = None
    if connected_sock is None and v4_endpoint:
        try:
            h4, p4 = parse_ep(v4_endpoint)
            s_v4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s_v4.setblocking(False)
            s_v4.connect_ex((h4, p4))
            sockets.append(s_v4)
            sock_info[s_v4] = ("IPv4", h4, p4)
        except Exception:
            if s_v4:
                try: s_v4.close()
                except Exception: pass
                s_v4 = None

    # 4. Race remaining sockets until one connects or total timeout expires
    while not connected_sock and sockets:
        elapsed = time.time() - t_start
        rem = max(0.1, timeout - elapsed)
        if rem <= 0:
            break
        r, w, e = select.select([], sockets, sockets, min(rem, 0.5))
        for s in e:
            if s in sockets: sockets.remove(s)
            try: s.close()
            except Exception: pass
        for s in w:
            err = s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if err == 0:
                connected_sock = s
                break
            else:
                if s in sockets: sockets.remove(s)
                try: s.close()
                except Exception: pass

    if not connected_sock:
        for s in sockets:
            try: s.close()
            except Exception: pass
        raise ConnectionRefusedError("Happy Eyeballs connection failed to all endpoints (IPv6/IPv4)")

    # Close the losing socket immediately
    for s in list(sockets):
        if s is not connected_sock:
            try: s.close()
            except Exception: pass

    connected_sock.setblocking(True)
    try:
        connected_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        connected_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
        connected_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
    except Exception: pass
    proto_tag, win_host, win_port = sock_info[connected_sock]

    # 5. Cryptographic Handshake (72-byte first packet)
    client_priv = x25519.X25519PrivateKey.generate()
    client_pub_bytes = client_priv.public_key().public_bytes_raw()
    ts = int(time.time())
    ts_bytes = struct.pack(">Q", ts)

    client_hmac = hmac.new(params["psk"], client_pub_bytes + ts_bytes, hashlib.sha256).digest()
    proof_packet = client_pub_bytes + ts_bytes + client_hmac
    connected_sock.sendall(proof_packet)

    # Derive session key & SAS
    server_pub = x25519.X25519PublicKey.from_public_bytes(params["server_pub_bytes"])
    shared_secret = client_priv.exchange(server_pub)
    session_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=params["psk"],
        info=b"VDDMon-V1-Session"
    ).derive(shared_secret)

    sas = int.from_bytes(hmac.new(session_key, b"SAS", hashlib.sha256).digest()[:4], "big") % 10000

    return connected_sock, session_key, f"{sas:04d}", f"{proto_tag} ({win_host}:{win_port})"


# ---------------------------------------------------------------- Symmetric AEAD Transport Layer
class P2PTransport:
    """
    Wraps a connected TCP socket in ChaCha20Poly1305 AEAD authenticated encryption.
    - Formats 96-bit nonces: 4-byte direction tag + 8-byte uint64 counter.
    - Passes 5-byte framing header [ptype 1B] + [ciphertext_len 4B] as Associated Data (AAD) to prevent tampering.
    - Uses independent _send_lk and _recv_lk to prevent video transmissions from blocking control inputs or audio.
    """
    def __init__(self, sock, session_key, is_server=False):
        self.sock = sock
        self.session_key = session_key
        self.is_server = is_server
        self.aead = ChaCha20Poly1305(session_key)
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
        except Exception: pass

        self._send_lk = threading.Lock()
        self._recv_lk = threading.Lock()
        self.send_lk = self._send_lk # backward compatibility alias

        # 4-byte direction tags: 1 for server send, 2 for client send
        self.send_tag = 1 if is_server else 2
        self.recv_tag = 2 if is_server else 1
        self.send_counter = 0
        self.recv_counter = 0

    def is_busy(self):
        """Non-blocking check to see if send pipeline is currently busy."""
        if not self._send_lk.acquire(blocking=False):
            return True
        self._send_lk.release()
        return False

    def send_packet(self, ptype, payload):
        """Encrypts payload with ChaCha20Poly1305 and sends framed packet with header as AAD."""
        with self._send_lk:
            try:
                payload_bytes = bytes(payload) if not isinstance(payload, bytes) else payload
                ct_len = len(payload_bytes) + 16 # Poly1305 tag is 16 bytes
                hdr = struct.pack(">BI", ptype, ct_len)

                nonce = struct.pack(">I", self.send_tag) + struct.pack(">Q", self.send_counter)
                self.send_counter += 1

                ciphertext = self.aead.encrypt(nonce, payload_bytes, associated_data=hdr)
                self.sock.sendall(hdr + ciphertext)
                return True
            except Exception:
                return False

    def recv_packet(self, timeout=None):
        """Reads framed packet, verifies AAD and Poly1305 tag, and returns (ptype, plaintext)."""
        with self._recv_lk:
            try:
                if timeout is not None:
                    self.sock.settimeout(timeout)
                hdr = recv_exact(self.sock, 5, timeout=timeout if timeout else 5.0)
                if not hdr:
                    return None, None
                ptype, ct_len = struct.unpack(">BI", hdr)
                if ct_len > 16 * 1024 * 1024 + 16: # Max payload bound
                    return None, None
                ct = recv_exact(self.sock, ct_len, timeout=timeout if timeout else 5.0)
                if not ct:
                    return None, None

                nonce = struct.pack(">I", self.recv_tag) + struct.pack(">Q", self.recv_counter)
                self.recv_counter += 1

                plaintext = self.aead.decrypt(nonce, ct, associated_data=hdr)
                return ptype, plaintext
            except Exception:
                return None, None

    def close(self):
        try: self.sock.close()
        except Exception: pass
