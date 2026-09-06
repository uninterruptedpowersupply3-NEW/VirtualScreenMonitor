#!/usr/bin/env python3
"""vddmon client v0.2.0 — Hybrid Dual-Engine (TurboJPEG / QSV H.264) Remote Desktop Suite.

Client for Single-Port Multiplexed Transport (Port 5900 ONLY).
Supports in-memory TurboJPEG and PyAV H.264 decoding, WASAPI loopback audio,
and full in-band displays, resolution, and rekeying controls.
"""
import base64, collections, ctypes, gc, hashlib, hmac as hmac_mod, io, json, os, socket, struct, subprocess, sys
import threading, time, traceback
try:
    import vddmon_p2p
except Exception:
    vddmon_p2p = None
from ctypes import wintypes
from pathlib import Path

# Enable crisp Per-Monitor DPI awareness on Windows to prevent blurry DWM bitmap scaling
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception: pass

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.restype = wintypes.BOOL
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.GetCursor.restype = wintypes.HANDLE
user32.GetCursor.argtypes = []
user32.LoadCursorW.restype = wintypes.HANDLE
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.DrawIconEx.restype = wintypes.BOOL
user32.DrawIconEx.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HANDLE,
    ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.HBRUSH, wintypes.UINT
]

gdi32.SetStretchBltMode.restype = ctypes.c_int
gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]

gdi32.PatBlt.restype = wintypes.BOOL
gdi32.PatBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD]

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]

gdi32.StretchDIBits.restype = ctypes.c_int
gdi32.StretchDIBits.argtypes = [
    wintypes.HDC,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.UINT,
    wintypes.DWORD
]

class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

def dpapi_encrypt(text: str) -> str:
    """Encrypts plaintext credentials with Windows Data Protection API (DPAPI)."""
    if not text: return ""
    try:
        data = text.encode("utf-8")
        in_blob = DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)))
        out_blob = DATA_BLOB()
        if ctypes.windll.crypt32.CryptProtectData(ctypes.byref(in_blob), "vddmon_pw", None, None, None, 0, ctypes.byref(out_blob)):
            enc_bytes = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)
            return "dpapi:" + base64.b64encode(enc_bytes).decode("ascii")
    except Exception: pass
    return text

def dpapi_decrypt(cipher: str) -> str:
    """Decrypts DPAPI credentials with transparent fallback to legacy plaintext."""
    if not cipher: return ""
    if not cipher.startswith("dpapi:"):
        return cipher  # Legacy plaintext fallback
    try:
        raw_b64 = cipher[6:]
        enc_bytes = base64.b64decode(raw_b64)
        in_blob = DATA_BLOB(len(enc_bytes), ctypes.cast(ctypes.create_string_buffer(enc_bytes), ctypes.POINTER(ctypes.c_char)))
        out_blob = DATA_BLOB()
        if ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
            dec_bytes = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)
            return dec_bytes.decode("utf-8", "replace")
    except Exception: pass
    return ""

import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk

has_pygame = False

try:
    import cv2
except Exception:
    cv2 = None

try:
    import numpy as np
except Exception:
    np = None

class NativeH264Decoder:
    """Zero-FFmpeg standalone Windows Media Foundation low-latency H.264 decoder."""
    def __init__(self):
        self.dll = None
        self.w = 0
        self.h = 0
        self.bgrx_buf = None
        self.buf_size = 0
        self.last_byte_count = 0
        self._c_w = ctypes.c_int(0)
        self._c_h = ctypes.c_int(0)
        self._c_pitch = ctypes.c_int(0)
        self.alive = False
        self._init_dll()

    def _init_dll(self):
        try:
            candidates = []
            if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
                candidates.append(Path(sys._MEIPASS) / "mft_decoder.dll")
            candidates.append(Path(sys.executable).resolve().parent / "mft_decoder.dll")
            candidates.append(Path(__file__).resolve().parent / "mft_decoder.dll")
            candidates.append(Path("mft_decoder.dll"))

            p = None
            for c in candidates:
                if c and c.exists():
                    p = str(c)
                    break
            if not p:
                p = "mft_decoder.dll"
            self.dll = ctypes.CDLL(p)
            self.dll.mft_init.argtypes = []
            self.dll.mft_init.restype = ctypes.c_int
            self.dll.mft_flush.argtypes = []
            self.dll.mft_flush.restype = None
            self.dll.mft_shutdown.argtypes = []
            self.dll.mft_shutdown.restype = None
            self.dll.mft_decode_frame.argtypes = [
                ctypes.c_char_p, ctypes.c_int,
                ctypes.c_char_p, ctypes.c_int,
                ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)
            ]
            self.dll.mft_decode_frame.restype = ctypes.c_int
            res = self.dll.mft_init()
            self.alive = (res == 0)
        except Exception:
            self.alive = False

    def decode(self, nal_bytes):
        """Decodes raw Annex-B NAL unit into PIL Image ('RGB' mode via BGRX buffer) and NumPy array."""
        if not self.alive or not self.dll or not nal_bytes:
            return None, None
        needed = 3840 * 2160 * 4
        if self.bgrx_buf is None or self.buf_size < needed:
            self.bgrx_buf = (ctypes.c_char * needed)()
            self.buf_size = needed

        ret = self.dll.mft_decode_frame(
            nal_bytes, len(nal_bytes),
            self.bgrx_buf, self.buf_size,
            ctypes.byref(self._c_w), ctypes.byref(self._c_h), ctypes.byref(self._c_pitch)
        )
        if ret == 0 and self._c_w.value > 0 and self._c_h.value > 0:
            w, h = self._c_w.value, self._c_h.value
            byte_count = w * h * 4
            self.last_byte_count = byte_count
            img = Image.frombuffer("RGB", (w, h), self.bgrx_buf, "raw", "BGRX", 0, 1)
            arr = None
            if np is not None:
                try:
                    arr = np.frombuffer(self.bgrx_buf, dtype=np.uint8, count=byte_count).reshape((h, w, 4))
                except Exception:
                    arr = None
            return img, arr
        return None, None

    def decode_into(self, nal_bytes, dst_buf, max_dst):
        """Decodes raw Annex-B NAL unit directly into pre-allocated memory."""
        if not self.alive or not self.dll or not nal_bytes:
            return None
        try:
            ret = self.dll.mft_decode_frame(
                nal_bytes, len(nal_bytes),
                dst_buf, max_dst,
                ctypes.byref(self._c_w), ctypes.byref(self._c_h), ctypes.byref(self._c_pitch)
            )
            if ret == 0 and self._c_w.value > 0 and self._c_h.value > 0:
                return self._c_w.value, self._c_h.value
        except Exception:
            traceback.print_exc()
        return None

    def get_raw_bgrx(self):
        """Returns zero-copy memoryview of the raw decoded BGRX buffer."""
        if self._c_w.value > 0 and self._c_h.value > 0 and self.bgrx_buf:
            byte_count = self._c_w.value * self._c_h.value * 4
            return memoryview(self.bgrx_buf)[:byte_count]
        return None

    def flush(self):
        if self.alive and self.dll:
            try: self.dll.mft_flush()
            except Exception: pass

    def close(self):
        if self.alive and self.dll:
            try: self.dll.mft_shutdown()
            except Exception: pass
            self.alive = False
            self.dll = None


try:
    from turbojpeg import TurboJPEG, TJPF_RGB
except Exception:
    TurboJPEG = None; TJPF_RGB = None

def parse_jpeg_dimensions(data):
    """Zero-overhead pure binary JPEG header parser extracting (width, height)."""
    if not data or len(data) < 4 or data[0:2] != b'\xff\xd8':
        return None
    idx = 2
    data_len = len(data)
    while idx + 4 < data_len:
        if data[idx] != 0xff:
            idx += 1
            continue
        marker = data[idx + 1]
        if marker in (0x00, 0xff):
            idx += 1
            continue
        # SOF markers: C0..C3, C5..C7, C9..CB, CD..CF
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if idx + 9 <= data_len:
                h, w = struct.unpack(">HH", data[idx + 5 : idx + 9])
                return w, h
            break
        seg_len = struct.unpack(">H", data[idx + 2 : idx + 4])[0]
        idx += 2 + seg_len
    return None

def get_jpeg_dimensions(jpeg_bytes, tj=None):
    if tj is not None and hasattr(tj, "decode_header"):
        try:
            w, h, _, _ = tj.decode_header(jpeg_bytes)
            return w, h
        except Exception:
            pass
    dim = parse_jpeg_dimensions(jpeg_bytes)
    if dim:
        return dim
    try:
        im = Image.open(io.BytesIO(jpeg_bytes))
        return im.size
    except Exception:
        return None

HOSTS_F = Path(os.environ.get("APPDATA", ".")) / "vddmon_client" / "hosts.json"
HOSTS_F.parent.mkdir(parents=True, exist_ok=True)

NAMED = {
    "Return": 0xFF0D, "KP_Enter": 0xFF8D, "Tab": 0xFF09, "BackSpace": 0xFF08,
    "Escape": 0xFF1B, "Home": 0xFF50, "End": 0xFF57, "Prior": 0xFF55, "Next": 0xFF56,
    "Left": 0xFF51, "Right": 0xFF53, "Up": 0xFF52, "Down": 0xFF54, "Insert": 0xFF63,
    "Delete": 0xFFFF, "Shift_L": 0xFFE1, "Shift_R": 0xFFE2, "Control_L": 0xFFE3,
    "Control_R": 0xFFE4, "Alt_L": 0xFFE9, "Alt_R": 0xFFEA, "Caps_Lock": 0xFFE5,
    "Num_Lock": 0xFF7F, "Scroll_Lock": 0xFF14, "space": 0x20
}
for i in range(12): NAMED[f"F{i+1}"] = 0xFFBE + i
NAMED.update({
    "exclam": 0x21, "at": 0x40, "numbersign": 0x23, "dollar": 0x24,
    "percent": 0x25, "asciicircum": 0x5E, "ampersand": 0x26, "asterisk": 0x2A,
    "parenleft": 0x28, "parenright": 0x29, "minus": 0x2D, "underscore": 0x5F,
    "equal": 0x3D, "plus": 0x2B, "bracketleft": 0x5B, "bracketright": 0x5D,
    "braceleft": 0x7B, "braceright": 0x7D, "semicolon": 0x3B, "colon": 0x3A,
    "apostrophe": 0x27, "quotedbl": 0x22, "grave": 0x60, "asciitilde": 0x7E,
    "comma": 0x2C, "less": 0x3C, "period": 0x2E, "greater": 0x3E,
    "slash": 0x2F, "question": 0x3F, "backslash": 0x5C, "bar": 0x7C
})

def fmt_bytes(n):
    if n < 1024: return f"{n} B"
    elif n < 1024**2: return f"{n/1024:.1f} KB"
    elif n < 1024**3: return f"{n/(1024**2):.2f} MB"
    else: return f"{n/(1024**3):.2f} GB"

def rxn(s, n, conn=None, max_n=16*1024*1024):
    if n < 0 or n > max_n:
        return None
    b = bytearray()
    while len(b) < n:
        c = s.recv(min(65536, n - len(b)))
        if not c: return None
        b += c
    if conn is not None:
        conn.bytes_recv += len(b)
    return bytes(b)

class TripleFrameBuffer:
    """Pre-allocated lockless/atomic triple buffer for zero-allocation 60 FPS frame exchange."""
    def __init__(self, max_w=3840, max_h=2160):
        self.capacity = max_w * max_h * 4
        self.buffers = [(ctypes.c_char * self.capacity)() for _ in range(3)]
        self.views = [memoryview(b).cast('B') for b in self.buffers]
        self.write_idx = 0
        self.latest_idx = -1
        self.w = 0
        self.h = 0
        self.timestamp = 0.0
        self.lock = threading.Lock()

    def get_write_buffer(self):
        with self.lock:
            for idx in range(3):
                if idx != self.latest_idx:
                    self.write_idx = idx
                    break
            return self.buffers[self.write_idx], self.capacity

    def commit_write(self, w, h):
        with self.lock:
            self.w = w
            self.h = h
            self.timestamp = time.perf_counter()
            self.latest_idx = self.write_idx

    def get_latest_frame(self):
        with self.lock:
            if self.latest_idx < 0:
                return None
            idx = self.latest_idx
            w, h = self.w, self.h
            ts = self.timestamp
        byte_count = w * h * 4
        ptr = ctypes.addressof(self.buffers[idx])
        return self.views[idx][:byte_count], w, h, ts, ptr

# ---------------------------------------------------------------- Headless Background Engine
class ClientEngine(threading.Thread):
    def __init__(self, host, port, pw, app):
        super().__init__(daemon=True)
        self.host, self.port, self.pw, self.app = host, port, pw, app
        self.sock = None; self.alive = True; self.ready = False
        self.fb = None; self.staging_fb = None; self.lk = threading.Lock(); self.send_lk = threading.Lock()
        self.frame_slot = None; self.frame_slot_lk = threading.Lock()
        self.triple_buf = TripleFrameBuffer()
        self.has_new_frame = False
        self.dirty = None
        self.pending_resize = None; self.pending_cut = None
        self.w = self.h = 0; self.err = None
        self.bytes_recv = 0
        self.user_count = 1
        self.h264_decoder = None
        self.tj = None
        self.last_recv_ms = 0.0
        self.last_dec_ms = 0.0
        self.last_rtt_ms = 0.0
        self.srv_cap_ms = 0.0
        self.srv_enc_ms = 0.0
        self.srv_send_ms = 0.0
        self.transport = None
        self.sas_code = None
        self.target_scale = None
        self.scale_interp = "fast"
        self.proto_tag = None
        if TurboJPEG:
            try: self.tj = TurboJPEG()
            except Exception: pass

    def rx(self, n):
        return rxn(self.sock, n, self)

    def run(self):
        try:
            self._connect()
            self._loop()
        except Exception as e:
            self.err = str(e)
            err_msg = f"Error: {e}"
            self.app.note(err_msg)
            if hasattr(self.app, "post_ui"):
                self.app.post_ui(lambda m=err_msg: self.app.status_var.set(m) if hasattr(self.app, "status_var") else None)
        finally:
            self.alive = False
            if self.sock:
                try: self.sock.close()
                except Exception: pass
            if not self.ready and hasattr(self.app, "btn_connect"):
                try: self.app.post_ui(lambda: self.app.btn_connect.config(text="Connect"))
                except Exception: pass
            disc_msg = f"disconnected: {self.err}" if self.err else f"disconnected (data: {fmt_bytes(self.bytes_recv)}): closed"
            self.app.note(disc_msg)
            if hasattr(self.app, "post_ui"):
                self.app.post_ui(lambda m=disc_msg: self.app.status_var.set(m) if hasattr(self.app, "status_var") else None)

    def _connect(self):
        token_str = self.host.strip()
        is_token = False
        if token_str.startswith("vddmon://pair") or token_str.startswith("vddmon://") or len(token_str) > 40:
            if vddmon_p2p:
                try:
                    vddmon_p2p.parse_pairing_token(token_str)
                    is_token = True
                except Exception:
                    pass

        if is_token:
            try:
                s, session_key, sas, proto_tag = vddmon_p2p.connect_p2p(token_str)
            except TimeoutError:
                raise RuntimeError("Token expired (>120s) - generate a fresh token on host")
            except (ConnectionRefusedError, OSError) as ce:
                raise RuntimeError(f"Connection failed to P2P endpoints ({ce})")

            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
            self.sock = s
            self.transport = vddmon_p2p.P2PTransport(s, session_key, is_server=False)
            self.sas_code = sas
            self.proto_tag = proto_tag

            # Read initial geometry notification packet (packet 0x05 sub 0x02)
            ptype, payload = self.transport.recv_packet(timeout=5.0)
            if ptype is None or not payload:
                try: s.close()
                except Exception: pass
                raise RuntimeError("Token expired or already used - generate a fresh token on host")
            if ptype != 0x05 or payload[0] != 0x02:
                try: s.close()
                except Exception: pass
                raise RuntimeError("Token expired or already used - generate a fresh token on host")
            midx, self.w, self.h = struct.unpack(">BHH", payload[1:6])
            if self.app:
                self.app.fb_w, self.app.fb_h = self.w, self.h

            self.fb = Image.new("RGB", (self.w, self.h), (0, 0, 0))
            self.staging_fb = Image.new("RGB", (self.w, self.h), (0, 0, 0))
            self.pending_resize = (self.w, self.h)

            try:
                self.h264_decoder = NativeH264Decoder()
            except Exception as e:
                self.app.note(f"MFT h264 decoder init: {e!r}")

            self.ready = True
            msg = f"🔐 P2P Connected ({proto_tag}) [SAS: {sas}] ({self.w}x{self.h})"
            self.app.note(msg)
            return

        clean_host = self.host.strip().strip("[]")
        s = socket.create_connection((clean_host, self.port), 10)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
        self.sock = s

        # Handshake: b"VDD01\n" + salt (16B) + challenge (32B)
        magic = self.rx(6)
        if magic != b"VDD01\n":
            raise RuntimeError("server did not provide valid VDD01 protocol header")
        salt = self.rx(16)
        challenge = self.rx(32)
        if not salt or not challenge:
            raise RuntimeError("incomplete handshake payload from host")

        # PBKDF2-HMAC-SHA256 (200k rounds)
        pw_clean = (self.pw or "").strip()
        k = hashlib.pbkdf2_hmac("sha256", pw_clean.encode("utf-8"), salt, 200_000)
        resp = hmac_mod.new(k, challenge, "sha256").digest()
        s.sendall(resp)

        auth_byte = self.rx(1)
        if not auth_byte or auth_byte[0] != 0:
            raise RuntimeError("authentication failed (incorrect password)")

        geo = self.rx(4)
        if not geo: raise RuntimeError("failed to read screen geometry")
        self.w, self.h = struct.unpack(">HH", geo)
        if self.app:
            self.app.fb_w, self.app.fb_h = self.w, self.h

        self.fb = Image.new("RGB", (self.w, self.h), (0, 0, 0))
        self.staging_fb = Image.new("RGB", (self.w, self.h), (0, 0, 0))
        self.pending_resize = (self.w, self.h)

        try:
            self.h264_decoder = NativeH264Decoder()
        except Exception as e:
            self.app.note(f"MFT h264 decoder init: {e!r}")

        self.ready = True
        try:
            dev_val = bool(getattr(self.app, "dev_mode_var", None) and self.app.dev_mode_var.get())
        except Exception:
            dev_val = False
        self.send_dev_mode(dev_val)
        if getattr(self.app, "engine_mode", None) and self.app.engine_mode != "auto":
            self.send_set_engine(self.app.engine_mode)
        self.app.note(f"connected: {clean_host} ({self.w}x{self.h})")

    def _loop(self):
        while self.alive:
            if getattr(self, "transport", None):
                ptype, payload = self.transport.recv_packet()
                if ptype is None: break
                plen = len(payload)
                self.bytes_recv += 5 + plen + 16
                self.last_recv_ms = 0.0
            else:
                hdr = self.rx(5)
                if not hdr: break
                ptype, plen = struct.unpack(">BI", hdr)
                if plen > 16 * 1024 * 1024: break
                t_r0 = time.perf_counter()
                payload = self.rx(plen) if plen > 0 else b""
                if payload is None: break
                t_r1 = time.perf_counter()
                self.last_recv_ms = (t_r1 - t_r0) * 1000.0

            # 0x00: Video Frame Update
            if ptype == 0x00:
                sub = payload[0]
                if sub == 0x01:  # TurboJPEG Tile
                    x, y, w, h = struct.unpack(">HHHH", payload[1:9])
                    jpeg_bytes = payload[9:]
                    t_dec0 = time.perf_counter()
                    try:
                        jw, jh = get_jpeg_dimensions(jpeg_bytes, tj=self.tj)
                        if jw and jh:
                            if w != jw or h != jh:
                                w, h = jw, jh
                        if self.tj:
                            pf = TJPF_RGB if TJPF_RGB is not None else 0
                            arr = self.tj.decode(jpeg_bytes, pixel_format=pf)
                            img = Image.fromarray(arr)
                        else:
                            img = Image.open(io.BytesIO(jpeg_bytes))
                        with self.lk:
                            if self.staging_fb is None or self.staging_fb.size != (self.w, self.h):
                                self.staging_fb = Image.new("RGB", (self.w, self.h), (0, 0, 0))
                            if x == 0 and y == 0 and (w == self.w and h == self.h):
                                self.staging_fb = img
                            else:
                                self.staging_fb.paste(img, (x, y))
                    except Exception: pass
                    self.last_dec_ms = (time.perf_counter() - t_dec0) * 1000.0

                elif sub == 0x02:  # QSV H.264 NAL Unit
                    nal_bytes = payload[1:]
                    t_dec0 = time.perf_counter()
                    with self.lk:
                        if self.h264_decoder is None:
                            self.h264_decoder = NativeH264Decoder()
                        decoder = self.h264_decoder
                    if decoder and decoder.alive:
                        try:
                            write_buf, cap = self.triple_buf.get_write_buffer()
                            dims = decoder.decode_into(nal_bytes, write_buf, cap)
                            if dims:
                                dw, dh = dims
                                self.w, self.h = dw, dh
                                self.triple_buf.commit_write(dw, dh)
                                self.has_new_frame = True
                                latest = self.triple_buf.get_latest_frame()
                                if latest:
                                    with self.frame_slot_lk:
                                        self.frame_slot = (None, None, (0, 0, dw, dh), latest[0])
                        except Exception:
                            traceback.print_exc()
                    self.last_dec_ms = (time.perf_counter() - t_dec0) * 1000.0

                elif sub == 0x03:  # TurboJPEG Atomic Frame Commit
                    if self.staging_fb is not None:
                        dw, dh = self.w, self.h
                        try:
                            write_buf, cap = self.triple_buf.get_write_buffer()
                            raw_bgrx = self.staging_fb.tobytes("raw", "BGRX")
                            needed = min(cap, len(raw_bgrx))
                            ctypes.memmove(write_buf, raw_bgrx, needed)
                            self.triple_buf.commit_write(dw, dh)
                            self.has_new_frame = True
                            latest = self.triple_buf.get_latest_frame()
                            if latest:
                                with self.frame_slot_lk:
                                    self.frame_slot = (self.staging_fb, None, (0, 0, dw, dh), latest[0])
                        except Exception:
                            traceback.print_exc()

            # 0x02: WASAPI Loopback Audio Frame
            elif ptype == 0x02:
                if self.app and self.app.audio_running:
                    mode = payload[0] if len(payload) > 1 else 0
                    pcm_data = payload[1:] if len(payload) > 1 else payload
                    self.app.play_audio(pcm_data, mode=mode)

            # 0x03: Clipboard Synchronization
            elif ptype == 0x03:
                self.pending_cut = payload.decode("utf-8", "replace")

            # 0x04: In-Band Rekey Response
            elif ptype == 0x04:
                ok = bool(payload[0] == 1) if payload else False
                if self.app: self.app.on_rekey_result(ok)

            # 0x05: Displays / Monitor Management Response
            elif ptype == 0x05:
                sub = payload[0] if payload else 0x01
                if sub == 0x02:  # Geometry / Monitor Switch Notification
                    midx, nw, nh = struct.unpack(">BHH", payload[1:6])
                    with self.lk:
                        self.w, self.h = nw, nh
                        self.fb = Image.new("RGB", (self.w, self.h), (0, 0, 0))
                        self.staging_fb = Image.new("RGB", (self.w, self.h), (0, 0, 0))
                        self.pending_resize = (self.w, self.h)
                        self.dirty = (0, 0, self.w, self.h)
                    if self.app: self.app.note(f"switched to monitor {midx} ({nw}x{nh})")
                else:
                    body = payload[1:].decode("utf-8", "replace")
                    try:
                        data = json.loads(body)
                        if sub == 0x01 and self.app:
                            self.app.on_displays_info(data)
                        elif sub == 0x03 and self.app:
                            self.app.on_vdd_install_result(data)
                    except Exception: pass

            # 0x06: Dynamic Resolution / Engine / Quality Management Response
            elif ptype == 0x06:
                sub = payload[0] if payload else 0x03
                if sub == 0x05:  # PING / RTT ECHO
                    if len(payload) >= 9:
                        t_sent = struct.unpack(">d", payload[1:9])[0]
                        self.last_rtt_ms = (time.perf_counter() - t_sent) * 1000.0
                    if len(payload) >= 21:
                        self.srv_cap_ms, self.srv_enc_ms, self.srv_send_ms = struct.unpack(">fff", payload[9:21])
                else:
                    body = payload[1:].decode("utf-8", "replace")
                    try:
                        data = json.loads(body)
                        if sub == 0x03 and self.app:
                            self.app.on_engine_status(data)
                    except Exception: pass

            # 0x07: Emergency Self-Destruct
            elif ptype == 0x07:
                self.app.note("EMERGENCY KILL TRIGGERED: Host terminated immediately!")
                self.alive = False
                try: self.sock.close()
                except Exception: pass
                os._exit(0)

    def send_packet(self, ptype, payload):
        if not self.sock or not self.alive: return False
        try:
            if getattr(self, "transport", None):
                with self.send_lk:
                    ok = self.transport.send_packet(ptype, payload)
                    return ok
            hdr = struct.pack(">BI", ptype, len(payload))
            with self.send_lk:
                self.sock.sendall(hdr + payload)
            return True
        except Exception:
            self.alive = False
            return False

    def send_pointer(self, mask, x, y):
        if getattr(self.app, "view_only_var", None) and self.app.view_only_var.get():
            return
        self.send_packet(0x01, bytes([0x01]) + struct.pack(">BHH", mask, x, y))

    def send_relative_pointer(self, mask, dx, dy):
        """Relative pointer delta packet for 3D games: Packet 0x01, sub 0x03."""
        if getattr(self.app, "view_only_var", None) and self.app.view_only_var.get():
            return
        payload = bytes([0x03]) + struct.pack(">Bhh", mask, int(dx), int(dy))
        self.send_packet(0x01, payload)

    def send_key(self, ks, down):
        if getattr(self.app, "view_only_var", None) and self.app.view_only_var.get():
            return
        self.send_packet(0x01, bytes([0x02]) + struct.pack(">BI", 1 if down else 0, ks))

    def send_cut(self, text):
        if getattr(self.app, "view_only_var", None) and self.app.view_only_var.get():
            return
        self.send_packet(0x03, text.encode("utf-8")[:262144])

    def send_rekey(self, new_pw, old_pw=None):
        cur_pw = old_pw if old_pw is not None else getattr(self, "pw", "")
        payload = cur_pw.encode("utf-8") + b"\x00" + new_pw.encode("utf-8")
        self.send_packet(0x04, payload)

    def send_req_displays(self):
        self.send_packet(0x05, bytes([0x01]))

    def send_set_monitor(self, idx):
        self.send_packet(0x05, bytes([0x02, int(idx)]))

    def send_install_vdd(self):
        self.send_packet(0x05, bytes([0x03]))

    def send_set_res(self, w, h):
        aw = max(64, (w + 15) & ~15) if w > 0 else 0
        ah = max(64, (h + 15) & ~15) if h > 0 else 0
        self.send_packet(0x06, bytes([0x01]) + struct.pack(">HH", max(0, aw), max(0, ah)))

    def send_dev_mode(self, enabled):
        self.send_packet(0x06, bytes([0x06, 1 if enabled else 0]))

    def send_set_engine(self, mode):
        if isinstance(mode, int):
            m = mode
        else:
            m = 0 if mode == "auto" else (1 if mode == "turbojpeg" else 2)
        self.send_packet(0x06, bytes([0x02, m]))
        with self.lk:
            self.dirty = (0, 0, self.w, self.h)

    def set_engine(self, mode):
        return self.send_set_engine(mode)

    def set_audio_filter(self, filt_on):
        is_sub = bool(getattr(self.app, "audio_running", False))
        mode = getattr(self.app, "audio_mode_var", None)
        mode_val = mode.get() if mode else 1
        self.send_audio_sub(is_sub, filt_on, mode_val)

    def _fit_frame(self, raw_img, raw_np):
        """Scales frame to target_scale in background worker thread with zero UI thread contention."""
        if not self.target_scale or raw_img is None:
            return raw_img
        tw, th = self.target_scale
        if tw <= 0 or th <= 0 or (tw, th) == (self.w, self.h):
            return raw_img
        try:
            if cv2 is not None and raw_np is not None:
                interp = cv2.INTER_LINEAR if getattr(self, "scale_interp", "fast") == "smooth" else cv2.INTER_NEAREST
                # Safeguard 1: cv2.resize returns a discrete new buffer, zero memory contention with UI thread
                scaled_np = cv2.resize(raw_np, (tw, th), interpolation=interp)
                return Image.fromarray(scaled_np)
            elif raw_np is not None and np is not None:
                idx_y = np.linspace(0, raw_np.shape[0] - 1, th).astype(np.int32)[:, None]
                idx_x = np.linspace(0, raw_np.shape[1] - 1, tw).astype(np.int32)[None, :]
                scaled_np = raw_np[idx_y, idx_x]
                return Image.fromarray(scaled_np)
            else:
                return raw_img.resize((tw, th), Image.NEAREST)
        except Exception:
            return raw_img

    def send_set_quality(self, *args, **kwargs):
        """Sends Packet 0x06 sub 0x04 with 13-byte struct >BBHBIBBBB:
        [0x04, idle_ms, target_hz, rc_mode, bitrate_kbps, qp, target_usage, tj_quality, tj_subsamp]"""
        if len(args) == 2 and isinstance(args[0], (int, float)):
            tj_q, qsv_kbps = args
            payload = struct.pack(
                ">BBHBIBBBB",
                0x04, 16, 60, 0, max(100, min(60000, int(qsv_kbps))), 21, 4, max(1, min(100, int(tj_q))), 0
            )
            self.send_packet(0x06, payload)
            return

        idle_ms = kwargs.get("idle_ms", args[0] if len(args) > 0 else 16)
        target_hz = kwargs.get("target_hz", args[1] if len(args) > 1 else 60)
        rc_mode = kwargs.get("rc_mode", args[2] if len(args) > 2 else 0)
        bitrate_kbps = kwargs.get("bitrate_kbps", args[3] if len(args) > 3 else 12000)
        qp = kwargs.get("qp", args[4] if len(args) > 4 else 21)
        target_usage = kwargs.get("target_usage", args[5] if len(args) > 5 else 4)
        tj_quality = kwargs.get("tj_quality", args[6] if len(args) > 6 else 85)
        tj_subsamp = kwargs.get("tj_subsamp", args[7] if len(args) > 7 else 0)

        rc = 1 if (rc_mode == 1 or str(rc_mode).upper() == "CQP") else 0
        samp = 1 if (tj_subsamp == 1 or "4:4:4" in str(tj_subsamp) or "444" in str(tj_subsamp)) else 0
        payload = struct.pack(
            ">BBHBIBBBB",
            0x04,
            max(0, min(50, int(idle_ms))),
            max(15, min(144, int(target_hz))),
            rc,
            max(100, min(60000, int(bitrate_kbps))),
            max(1, min(51, int(qp))),
            max(1, min(7, int(target_usage))),
            max(1, min(100, int(tj_quality))),
            samp
        )
        self.send_packet(0x06, payload)

    def send_ping(self):
        payload = bytes([0x05]) + struct.pack(">d", time.perf_counter())
        self.send_packet(0x06, payload)

    def send_audio_sub(self, enable, filter_on=True, audio_mode=1):
        self.send_packet(0x02, bytes([1 if enable else 0, 1 if filter_on else 0, int(audio_mode)]))

    def send_emergency_kill(self):
        self.send_packet(0x07, b"")
        self.alive = False
        os._exit(0)

    def close(self):
        self.alive = False
        try: self.sock.close()
        except Exception: pass
        if getattr(self, "h264_decoder", None):
            try: self.h264_decoder.close()
            except Exception: pass
            self.h264_decoder = None

    def stop(self):
        self.close()

# Backward-compatible alias
Conn = ClientEngine

# ---------------------------------------------------------------- Backward Compatibility
class PygameViewport:
    """Stub placeholder for backward compatibility."""
    pass


# ---------------------------------------------------------------- Client Application GUI
class App:
    ENGINE_MAP_MODE_TO_DISPLAY = {
        "auto": "Auto (Hybrid)",
        "turbojpeg": "TurboJPEG",
        "qsv": "QSV H.264",
    }
    ENGINE_MAP_DISPLAY_TO_MODE = {
        "Auto (Hybrid)": "auto",
        "TurboJPEG": "turbojpeg",
        "QSV H.264": "qsv",
    }

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("vddmon client — Hybrid Dual-Engine (Port 5900)")
        self.root.geometry("1180x750")
        self.conn = None; self.photo = None; self.mask = 0
        self.held = set()
        self.audio_running = False
        self.last_pos = (0, 0)
        self.note_lk = threading.Lock(); self.notes = []
        self._ui_queue = collections.deque()
        self._ui_queue_lk = threading.Lock()
        self.engine_mode = "auto"
        self._last_scrollregion = None
        self._active_eng_dlg_var = None
        self._cached_fit_cw = None
        self._cached_fit_ch = None
        self._cached_fit_fb_w = None
        self._cached_fit_fb_h = None
        self._last_img_coords = None

        # Fine-Grained Performance & Engine Governor Settings
        self.idle_ms_var = tk.IntVar(value=16)
        self.target_hz_var = tk.IntVar(value=60)
        self.rc_mode_var = tk.StringVar(value="VBR")
        self.bitrate_mbps_var = tk.DoubleVar(value=12.0)
        self.qp_var = tk.IntVar(value=21)
        self.target_usage_var = tk.IntVar(value=4)
        self.tj_quality_var = tk.IntVar(value=85)
        self.tj_subsamp_var = tk.StringVar(value="4:2:0")

        self.scale_fit = tk.BooleanVar(value=True)
        self.fb_w = self.fb_h = 0
        self.disp_w = self.disp_h = 0
        self.offset_x = self.offset_y = 0
        self.last_bytes = 0
        self.last_rate_time = time.time()
        self.client_perf_samples = collections.deque(maxlen=60)
        self.last_perf_log_t = time.perf_counter()
        self.last_ping_t = 0.0
        self.last_render_ms = 0.0
        self._last_mouse_pos = None

        top = ttk.Frame(self.root, padding=4); top.pack(fill="x")
        self.host = ttk.Combobox(top, width=24); self.host.pack(side="left")
        self.pw = ttk.Entry(top, width=12, show="*"); self.pw.pack(side="left", padx=3)
        self.remember = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="save", variable=self.remember).pack(side="left")
        self.btn_connect = ttk.Button(top, text="Connect", command=self.toggle_connect)
        self.btn_connect.pack(side="left", padx=2)
        ttk.Button(top, text="Displays", command=self.open_displays_dialog).pack(side="left", padx=2)
        ttk.Button(top, text="⚙ Settings", command=self.open_res_dialog).pack(side="left", padx=2)
        ttk.Button(top, text="Rekey", command=self.open_rekey_dialog).pack(side="left", padx=2)
        ttk.Checkbutton(top, text="Fit Window", variable=self.scale_fit, command=self.on_scale_toggle).pack(side="left", padx=3)

        self.view_only_var = tk.BooleanVar(value=False)
        self.view_only_btn = ttk.Checkbutton(top, text="👁️ View Only",
                                             variable=self.view_only_var,
                                             command=self._on_view_only_toggle)
        self.view_only_btn.pack(side="left", padx=3)
        self.dev_mode_var = tk.BooleanVar(value=False)

        self.audio_var = tk.StringVar(value="off")
        self.audio_mode_var = tk.IntVar(value=1)
        self.current_audio_mode = None
        self.filter_var = tk.StringVar(value="on")
        self.scale_interp_var = tk.StringVar(value="fast")
        ttk.Label(top, text="Audio:").pack(side="left", padx=(4, 0))
        ttk.Radiobutton(top, text="Off", variable=self.audio_var, value="off", command=self.on_audio_radio).pack(side="left")
        ttk.Radiobutton(top, text="On", variable=self.audio_var, value="on", command=self.on_audio_radio).pack(side="left")

        ttk.Button(top, text="Fullscreen", command=self.toggle_fs).pack(side="left", padx=3)

        self.engine_var = tk.StringVar(value="Auto (Hybrid)")
        ttk.Label(top, text="Engine:").pack(side="left", padx=(4, 0))
        self.engine_combo = ttk.Combobox(top, textvariable=self.engine_var,
                                         values=["Auto (Hybrid)", "TurboJPEG", "QSV H.264"],
                                         state="readonly", width=13)
        self.engine_combo.pack(side="left", padx=2)
        self.engine_combo.bind("<<ComboboxSelected>>", self._on_engine_selected)
        self.toolbar_engine_combo = self.engine_combo

        self.res_var = tk.StringVar(value="Match Screen (Native)")
        self.quality_var = tk.StringVar(value="Q85 | 12.0M @ 60Hz")

        self.mouse_lock_var = tk.BooleanVar(value=False)
        self.mouse_lock_btn = ttk.Checkbutton(top, text="🎮 Lock Mouse (Ctrl+Alt)",
                                              variable=self.mouse_lock_var,
                                              command=self._on_mouse_lock_toggle)
        self.mouse_lock_btn.pack(side="left", padx=3)

        self.users_var = tk.StringVar(value="👥 0/2")
        self.users_lbl = ttk.Label(top, textvariable=self.users_var, font=("Segoe UI", 9, "bold"), foreground="#008800")
        self.users_lbl.pack(side="left", padx=4)

        kill_btn = tk.Button(top, text="💀 Self-Destruct", bg="#cc0000", fg="white",
                             font=("Segoe UI", 8, "bold"), relief="flat",
                             activebackground="#ff0000", activeforeground="white",
                             cursor="hand2", command=self.on_self_destruct)
        kill_btn.pack(side="left", padx=4)

        self.data_var = tk.StringVar(value="Data: 0 B (0 B/s)")
        self.data_lbl = ttk.Label(top, textvariable=self.data_var, font=("Segoe UI", 9, "bold"), foreground="#0066cc")
        self.data_lbl.pack(side="right", padx=6)

        self.status_var = tk.StringVar(value="idle")
        ttk.Label(self.root, textvariable=self.status_var, anchor="w", padding=(4, 2)).pack(fill="x")

        frame = ttk.Frame(self.root); frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(frame, bg="black", highlightthickness=0, cursor="arrow")
        self.canvas_img_id = None
        self.sy = ttk.Scrollbar(frame, orient="vertical", command=self.canvas.yview)
        self.sx = ttk.Scrollbar(frame, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.sy.set, xscrollcommand=self.sx.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.sy.grid(row=0, column=1, sticky="ns"); self.sx.grid(row=1, column=0, sticky="ew")
        self.sy.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.sx.bind("<MouseWheel>", lambda e: self.canvas.xview_scroll(-1 if e.delta > 0 else 1, "units"))
        frame.rowconfigure(0, weight=1); frame.columnconfigure(0, weight=1)

        self._bind_input()
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Leave>", self._on_canvas_leave)
        self.canvas.bind("<Enter>", self._on_canvas_enter)

        def _check_token_input(_e=None):
            val = self.host.get().strip()
            is_tok = False
            if val.startswith("vddmon://") or len(val) > 40:
                if vddmon_p2p:
                    try:
                        vddmon_p2p.parse_pairing_token(val)
                        is_tok = True
                    except Exception: pass
            if is_tok:
                self.pw.config(state="disabled")
            else:
                self.pw.config(state="normal")

        self.host.bind("<KeyRelease>", _check_token_input)

        def _on_host_select(_e=None):
            raw = self.host.get().strip()
            if raw.startswith("[") and "]:" in raw:
                h = raw.split("]:", 1)[0][1:].strip()
            elif ":" in raw and raw.count(":") == 1:
                h = raw.split(":", 1)[0].strip()
            else:
                h = raw.strip("[]")
            target = self.hosts.get(raw) or self.hosts.get(h)
            if target:
                self.pw.delete(0, "end")
                saved_pw = dpapi_decrypt(target.get("pw", ""))
                if saved_pw:
                    self.pw.insert(0, saved_pw)
                    self.remember.set(True)
                else:
                    self.remember.set(False)

        self.host.bind("<<ComboboxSelected>>", lambda e: (_on_host_select(e), _check_token_input(e)))
        try:
            self.hosts = json.loads(HOSTS_F.read_text())
            self.host["values"] = list(self.hosts)
            if self.hosts:
                first = list(self.hosts)[0]
                self.host.set(first)
                _on_host_select()
        except Exception: self.hosts = {}

        # Optional CLI target host (e.g. python vddmon_client.py 10.0.0.1:5900)
        cli_target = None
        for arg in sys.argv[1:]:
            arg_s = arg.strip()
            if arg_s and not arg_s.startswith("-"):
                cli_target = arg_s
                break
        for i, arg in enumerate(sys.argv):
            if arg in ("--password", "-p") and i + 1 < len(sys.argv):
                self.pw.delete(0, "end")
                self.pw.insert(0, sys.argv[i + 1])

        if cli_target:
            self.host.set(cli_target)
            if cli_target.startswith("vddmon://"):
                self.root.after(100, self.connect)
            else:
                _on_host_select()
                if "--connect" in sys.argv:
                    self.root.after(100, self.connect)

        if "--qsv" in sys.argv:
            self.set_engine_mode("qsv", notify_server=False)
        elif "--turbojpeg" in sys.argv:
            self.set_engine_mode("turbojpeg", notify_server=False)

        self.root.after(33, self.tick)

    def set_engine_mode(self, mode, notify_server=True):
        """Bidirectionally synchronizes engine selection between settings dialog and toolbar."""
        if isinstance(mode, int):
            mode_str = "auto" if mode == 0 else ("turbojpeg" if mode == 1 else "qsv")
        else:
            m = str(mode).lower().strip()
            if "auto" in m:
                mode_str = "auto"
            elif "turbo" in m or "jpeg" in m:
                mode_str = "turbojpeg"
            elif "qsv" in m or "h.264" in m or "h264" in m:
                mode_str = "qsv"
            else:
                mode_str = "auto"

        # Safeguard 4: No-Op Guard on server status / unprompted reflection
        if not notify_server and self.engine_mode == mode_str:
            return

        self.engine_mode = mode_str
        display_name = self.ENGINE_MAP_MODE_TO_DISPLAY.get(mode_str, "Auto (Hybrid)")

        if hasattr(self, "engine_var") and self.engine_var.get() != display_name:
            self.engine_var.set(display_name)

        if getattr(self, "toolbar_engine_combo", None):
            try:
                if self.toolbar_engine_combo.get() != display_name:
                    self.toolbar_engine_combo.set(display_name)
            except Exception: pass

        if getattr(self, "_active_eng_dlg_var", None):
            try:
                if self._active_eng_dlg_var.get() != mode_str:
                    self._active_eng_dlg_var.set(mode_str)
            except Exception:
                pass

        if notify_server and self.conn and self.conn.alive:
            mode_idx = 0 if mode_str == "auto" else (1 if mode_str == "turbojpeg" else 2)
            self.conn.send_set_engine(mode_idx)
            self.note(f"switched engine to {display_name}")

    def _recompute_scale_dims(self, fb_w, fb_h):
        cw = max(self.canvas.winfo_width(), 10)
        ch = max(self.canvas.winfo_height(), 10)
        if (
            (cw, ch, fb_w, fb_h) == (self._cached_fit_cw, self._cached_fit_ch, self._cached_fit_fb_w, self._cached_fit_fb_h)
            and getattr(self, "_cached_fit_dw", None) is not None
        ):
            self.disp_w = self._cached_fit_dw
            self.disp_h = self._cached_fit_dh
            self.offset_x = self._cached_fit_ox
            self.offset_y = self._cached_fit_oy
            return self.disp_w, self.disp_h, self.offset_x, self.offset_y

        scale = min(cw / fb_w, ch / fb_h)
        dw = max(1, int(fb_w * scale))
        dh = max(1, int(fb_h * scale))
        self.disp_w, self.disp_h = dw, dh
        self.offset_x = (cw - dw) // 2
        self.offset_y = (ch - dh) // 2
        self._cached_fit_cw = cw
        self._cached_fit_ch = ch
        self._cached_fit_fb_w = fb_w
        self._cached_fit_fb_h = fb_h
        self._cached_fit_dw = dw
        self._cached_fit_dh = dh
        self._cached_fit_ox = self.offset_x
        self._cached_fit_oy = self.offset_y
        return dw, dh, self.offset_x, self.offset_y

    def apply_fine_quality(self):
        if self.conn and self.conn.alive:
            bitrate_kbps = int(round(self.bitrate_mbps_var.get() * 1000))
            self.conn.send_set_quality(
                idle_ms=self.idle_ms_var.get(),
                target_hz=self.target_hz_var.get(),
                rc_mode=self.rc_mode_var.get(),
                bitrate_kbps=bitrate_kbps,
                qp=self.qp_var.get(),
                target_usage=self.target_usage_var.get(),
                tj_quality=self.tj_quality_var.get(),
                tj_subsamp=self.tj_subsamp_var.get()
            )
            summary = f"Q{self.tj_quality_var.get()} | {self.bitrate_mbps_var.get():.1f}M @ {self.target_hz_var.get()}Hz"
            self.quality_var.set(summary)

    def post_ui(self, fn, *args, **kwargs):
        """Thread-safe UI dispatcher: queues callback to execute on main Tk thread in tick()."""
        with self._ui_queue_lk:
            self._ui_queue.append((fn, args, kwargs))

    def on_displays_info(self, info):
        self.displays_info = info
        if hasattr(self, "_disp_refresh_cb") and self._disp_refresh_cb:
            self.post_ui(self._disp_refresh_cb, info)

    def on_vdd_install_result(self, res):
        if hasattr(self, "_vdd_result_cb") and self._vdd_result_cb:
            self.post_ui(self._vdd_result_cb, res)

    def on_engine_status(self, st):
        if hasattr(self, "_eng_status_cb") and self._eng_status_cb:
            self.post_ui(self._eng_status_cb, st)
        eng = st.get("engine") or st.get("scale_engine")
        if eng:
            self.post_ui(lambda: self.set_engine_mode(eng, notify_server=False))

    def open_displays_dialog(self):
        if not self.conn or not self.conn.alive:
            messagebox.showinfo("Displays", "Connect to the remote host on Port 5900 first to query displays.")
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("Physical & Virtual Displays")
        dlg.geometry("500x420")
        dlg.resizable(False, False)
        dlg.transient(self.root); dlg.grab_set()

        ttk.Label(dlg, text="Select Screen / Virtual Display:", font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=14, pady=(12, 2))
        ttk.Label(dlg, text="Switch between physical monitors and headless virtual screens (In-Band 5900).", font=("Segoe UI", 8), foreground="#555555").pack(anchor="w", padx=14, pady=(0, 6))

        mon_var = tk.IntVar(value=0)
        mons_frame = ttk.LabelFrame(dlg, text="Available Displays on Server", padding=8)
        mons_frame.pack(fill="both", expand=True, padx=14, pady=4)

        status_lbl = ttk.Label(dlg, text="Querying host displays in-band...", foreground="#0066cc")
        status_lbl.pack(pady=2)

        def _update_mons_ui(info):
            for child in mons_frame.winfo_children():
                child.destroy()
            mons_list = info.get("monitors", [])
            active_m = info.get("active_monitor", 0)
            mon_var.set(active_m)
            if not mons_list:
                ttk.Label(mons_frame, text="No monitors reported by server.").pack()
                return
            for m in mons_list:
                idx = m.get("idx", 0)
                w, h = m.get("width", 0), m.get("height", 0)
                prim = "Primary Display" if m.get("primary") else "Secondary / Virtual Display"
                label = f"Monitor {idx}: {w}x{h} ({prim})"
                ttk.Radiobutton(mons_frame, text=label, variable=mon_var, value=idx).pack(anchor="w", pady=3)
            status_lbl.config(text=f"✓ Host displays synced ({len(mons_list)} found)", foreground="#008800")

        self._disp_refresh_cb = _update_mons_ui
        self.conn.send_req_displays()

        def _on_vdd_result(res):
            ok = res.get("ok", False)
            msg = res.get("msg", "")
            status_lbl.config(text=f"VDD Setup: {msg[:60]}", foreground="#008800" if ok else "red")
            self.conn.send_req_displays()

        self._vdd_result_cb = _on_vdd_result

        def _setup_vdd():
            status_lbl.config(text="Installing Virtual Display Driver on host...", foreground="#0066cc")
            dlg.update()
            self.conn.send_install_vdd()

        def _apply():
            target_idx = mon_var.get()
            if self.conn and self.conn.alive:
                self.conn.send_set_monitor(target_idx)
            status_lbl.config(text=f"✓ Switched to Monitor {target_idx}", foreground="#008800")
            self.status_var.set(f"Viewing Monitor {target_idx}")
            self.root.after(700, dlg.destroy)

        btn_f = ttk.Frame(dlg)
        btn_f.pack(fill="x", padx=14, pady=8)
        ttk.Button(btn_f, text="➕ Setup Virtual Display", command=_setup_vdd).pack(side="left")
        ttk.Button(btn_f, text="Switch Display", command=_apply).pack(side="right", padx=4)
        ttk.Button(btn_f, text="Close", command=dlg.destroy).pack(side="right")

    def open_res_dialog(self):
        if hasattr(self, "_settings_dlg") and self._settings_dlg and self._settings_dlg.winfo_exists():
            self._settings_dlg.lift()
            self._settings_dlg.focus_set()
            return

        dlg = tk.Toplevel(self.root)
        self._settings_dlg = dlg
        dlg.title("Stream Settings & Engine Governor")
        dlg.geometry("570x720")
        dlg.transient(self.root)

        btn_f = ttk.Frame(dlg, padding=8)
        btn_f.pack(side="bottom", fill="x")

        canvas_dlg = tk.Canvas(dlg, highlightthickness=0)
        sb_dlg = ttk.Scrollbar(dlg, orient="vertical", command=canvas_dlg.yview)
        canvas_dlg.configure(yscrollcommand=sb_dlg.set)
        sb_dlg.pack(side="right", fill="y")
        canvas_dlg.pack(side="left", fill="both", expand=True)

        content = ttk.Frame(canvas_dlg, padding=12)
        win_id = canvas_dlg.create_window((0, 0), window=content, anchor="nw")
        def _on_content_cfg(_e):
            canvas_dlg.configure(scrollregion=canvas_dlg.bbox("all"))
        content.bind("<Configure>", _on_content_cfg)
        canvas_dlg.bind("<Configure>", lambda e: canvas_dlg.itemconfig(win_id, width=e.width))

        ttk.Label(content, text="Stream Performance & Engine Controls", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(content, text="Fine-grained sweeper governor, QSV H.264 ASIC rate control, and TurboJPEG options.", font=("Segoe UI", 8), foreground="#555555").pack(anchor="w", pady=(0, 6))

        # --- 1. Resolution & Downscaling ---
        res_f = ttk.LabelFrame(content, text="Display & Downscaling Resolution", padding=8)
        res_f.pack(fill="x", pady=4)
        cur_res = self.res_var.get()
        res_var = tk.StringVar(value="native" if "Native" in cur_res else cur_res)
        cur_w = getattr(self, "stream_w", 0) or getattr(self, "fb_w", 0) or 1920
        cur_h = getattr(self, "stream_h", 0) or getattr(self, "fb_h", 0) or 1080
        if "x" in cur_res and "Native" not in cur_res:
            try:
                cp = cur_res.split("x")
                cur_w, cur_h = int(cp[0]), int(cp[1])
            except Exception: pass
        w_var = tk.StringVar(value=str(cur_w))
        h_var = tk.StringVar(value=str(cur_h))
        savings_var = tk.StringVar(value="Current: Native Display (100% Quality, Full Bandwidth)")

        presets = [
            ("Native Screen Match (Full Detail)", "native", "", ""),
            ("720p (1280 x 720) [Default / Recommended for QSV]", "1280x720", "1280", "720"),
            ("1080p (1920 x 1080) [FHD Standard High Quality]", "1920x1080", "1920", "1080"),
            ("1440p (2560 x 1440) [2K QHD Crisp Resolution]", "2560x1440", "2560", "1440"),
            ("4K (3840 x 2160) [UHD Ultra High Definition]", "3840x2160", "3840", "2160"),
            ("Custom Resolution (16x16 -> 8K)", "custom", "", "")
        ]

        def _update_savings():
            val = res_var.get()
            if val == "native":
                savings_var.set("Selected: Native Display (Full Bandwidth)")
                return
            try:
                tw, th = int(w_var.get().strip()), int(h_var.get().strip())
                nw = self.conn.w if (self.conn and self.conn.w) else 1920
                nh = self.conn.h if (self.conn and self.conn.h) else 1080
                saved = max(0.0, (1 - ((tw * th) / (nw * nh))) * 100)
                savings_var.set(f"Estimated Savings: ~{saved:.1f}% less data & memory bandwidth")
            except Exception:
                savings_var.set("Custom dimensions (enter valid numbers)")

        def _on_res_radio():
            val = res_var.get()
            for label, key, pw, ph in presets:
                if key == val and key not in ("custom", "native"):
                    w_var.set(pw); h_var.set(ph)
            _update_savings()

        for label, key, pw, ph in presets:
            ttk.Radiobutton(res_f, text=label, variable=res_var, value=key, command=_on_res_radio).pack(anchor="w", pady=1)

        custom_box = ttk.Frame(res_f)
        custom_box.pack(fill="x", pady=2)
        ttk.Label(custom_box, text="Custom:").pack(side="left", padx=(4, 2))
        w_ent = ttk.Entry(custom_box, textvariable=w_var, width=6)
        w_ent.pack(side="left", padx=2)
        ttk.Label(custom_box, text="x").pack(side="left")
        h_ent = ttk.Entry(custom_box, textvariable=h_var, width=6)
        h_ent.pack(side="left", padx=2)
        w_ent.bind("<FocusIn>", lambda e: (res_var.set("custom"), _update_savings()))
        h_ent.bind("<FocusIn>", lambda e: (res_var.set("custom"), _update_savings()))
        w_ent.bind("<KeyRelease>", lambda e: _update_savings())
        h_ent.bind("<KeyRelease>", lambda e: _update_savings())

        ttk.Label(res_f, textvariable=savings_var, font=("Segoe UI", 8, "bold"), foreground="#0066cc").pack(anchor="w", pady=2)

        # --- 2. Engine Governor & Throttle ---
        gov_f = ttk.LabelFrame(content, text="Engine Governor & Frame Pacing", padding=8)
        gov_f.pack(fill="x", pady=4)
        gov_grid = ttk.Frame(gov_f)
        gov_grid.pack(fill="x")

        ttk.Label(gov_grid, text="Idle Sleep Cap:").grid(row=0, column=0, sticky="w", pady=2)
        idle_scale_var = tk.IntVar(value=self.idle_ms_var.get())
        idle_lbl = ttk.Label(gov_grid, text=f"{idle_scale_var.get()} ms" + (" (Max FPS / Unconstrained)" if idle_scale_var.get() == 0 else ""))
        idle_lbl.grid(row=0, column=2, sticky="w", padx=6)
        def _on_idle_slider(v):
            val = int(float(v))
            idle_scale_var.set(val)
            idle_lbl.config(text=f"{val} ms" + (" (Max FPS / Unconstrained)" if val == 0 else ""))
        idle_scale = ttk.Scale(gov_grid, from_=0, to=50, orient="horizontal", command=_on_idle_slider)
        idle_scale.set(idle_scale_var.get())
        idle_scale.grid(row=0, column=1, sticky="ew", padx=4)

        ttk.Label(gov_grid, text="Target Framerate / Polling Hz:").grid(row=1, column=0, sticky="w", pady=4)
        hz_spin_var = tk.IntVar(value=self.target_hz_var.get())
        hz_spin = ttk.Spinbox(gov_grid, from_=15, to=144, textvariable=hz_spin_var, width=6)
        hz_spin.grid(row=1, column=1, sticky="w", padx=4, pady=4)
        ttk.Label(gov_grid, text="Hz (15 - 144)").grid(row=1, column=2, sticky="w", padx=6)
        gov_grid.columnconfigure(1, weight=1)

        # --- 3. Video Stream (QSV H.264) ---
        qsv_f = ttk.LabelFrame(content, text="Video Stream (Intel QSV H.264 ASIC)", padding=8)
        qsv_f.pack(fill="x", pady=4)

        rc_dlg_var = tk.StringVar(value=self.rc_mode_var.get())
        rc_frame = ttk.Frame(qsv_f)
        rc_frame.pack(fill="x", pady=2)
        ttk.Label(rc_frame, text="Rate Control:").pack(side="left")

        qsv_grid = ttk.Frame(qsv_f)
        qsv_grid.pack(fill="x", pady=4)

        ttk.Label(qsv_grid, text="Target Bitrate:").grid(row=0, column=0, sticky="w")
        bitrate_dlg_var = tk.DoubleVar(value=self.bitrate_mbps_var.get())
        bitrate_lbl = ttk.Label(qsv_grid, text=f"{bitrate_dlg_var.get():.1f} Mbps")
        bitrate_lbl.grid(row=0, column=2, sticky="w", padx=6)
        def _on_bitrate_slider(v):
            val = round(float(v) * 10) / 10.0
            bitrate_dlg_var.set(val)
            bitrate_lbl.config(text=f"{val:.1f} Mbps")
        bitrate_scale = ttk.Scale(qsv_grid, from_=0.1, to=30.0, orient="horizontal", command=_on_bitrate_slider)
        bitrate_scale.set(bitrate_dlg_var.get())
        bitrate_scale.grid(row=0, column=1, sticky="ew", padx=4)

        ttk.Label(qsv_grid, text="Constant QP:").grid(row=1, column=0, sticky="w", pady=2)
        qp_dlg_var = tk.IntVar(value=self.qp_var.get())
        qp_lbl = ttk.Label(qsv_grid, text=f"QP {qp_dlg_var.get()}")
        qp_lbl.grid(row=1, column=2, sticky="w", padx=6)
        def _on_qp_slider(v):
            val = int(float(v))
            qp_dlg_var.set(val)
            desc = " (Lossless)" if val <= 14 else (" (Heavy Compression)" if val >= 35 else "")
            qp_lbl.config(text=f"QP {val}{desc}")
        qp_scale = ttk.Scale(qsv_grid, from_=12, to=40, orient="horizontal", command=_on_qp_slider)
        qp_scale.set(qp_dlg_var.get())
        qp_scale.grid(row=1, column=1, sticky="ew", padx=4)

        def _update_rc_state():
            mode = rc_dlg_var.get().upper()
            if mode == "CQP":
                bitrate_scale.state(["disabled"])
                bitrate_lbl.config(foreground="#888888")
                qp_scale.state(["!disabled"])
                qp_lbl.config(foreground="#000000")
            else:
                bitrate_scale.state(["!disabled"])
                bitrate_lbl.config(foreground="#000000")
                qp_scale.state(["disabled"])
                qp_lbl.config(foreground="#888888")

        ttk.Radiobutton(rc_frame, text="VBR (Variable Bitrate)", variable=rc_dlg_var, value="VBR", command=_update_rc_state).pack(side="left", padx=8)
        ttk.Radiobutton(rc_frame, text="CQP (Constant QP)", variable=rc_dlg_var, value="CQP", command=_update_rc_state).pack(side="left", padx=8)
        _update_rc_state()

        ttk.Label(qsv_grid, text="Target Usage (TU):").grid(row=2, column=0, sticky="w", pady=2)
        tu_dlg_var = tk.IntVar(value=self.target_usage_var.get())
        tu_names = {1: "1 (Best Quality)", 2: "2 (High Quality)", 3: "3 (Quality)", 4: "4 (Balanced)", 5: "5 (Speed)", 6: "6 (Fast)", 7: "7 (Best Speed)"}
        tu_lbl = ttk.Label(qsv_grid, text=tu_names.get(tu_dlg_var.get(), f"TU {tu_dlg_var.get()}"))
        tu_lbl.grid(row=2, column=2, sticky="w", padx=6)
        def _on_tu_slider(v):
            val = int(float(v))
            tu_dlg_var.set(val)
            tu_lbl.config(text=tu_names.get(val, f"TU {val}"))
        tu_scale = ttk.Scale(qsv_grid, from_=1, to=7, orient="horizontal", command=_on_tu_slider)
        tu_scale.set(tu_dlg_var.get())
        tu_scale.grid(row=2, column=1, sticky="ew", padx=4)
        qsv_grid.columnconfigure(1, weight=1)

        # --- 4. TurboJPEG Desktop Stream ---
        tj_f = ttk.LabelFrame(content, text="TurboJPEG Desktop Stream", padding=8)
        tj_f.pack(fill="x", pady=4)

        tj_grid = ttk.Frame(tj_f)
        tj_grid.pack(fill="x", pady=2)

        ttk.Label(tj_grid, text="JPEG Quality:").grid(row=0, column=0, sticky="w")
        tj_q_dlg_var = tk.IntVar(value=self.tj_quality_var.get())
        tj_q_lbl = ttk.Label(tj_grid, text=f"Q{tj_q_dlg_var.get()}")
        tj_q_lbl.grid(row=0, column=2, sticky="w", padx=6)
        def _on_tj_q_slider(v):
            val = int(float(v))
            tj_q_dlg_var.set(val)
            tj_q_lbl.config(text=f"Q{val}")
        tj_q_scale = ttk.Scale(tj_grid, from_=1, to=100, orient="horizontal", command=_on_tj_q_slider)
        tj_q_scale.set(tj_q_dlg_var.get())
        tj_q_scale.grid(row=0, column=1, sticky="ew", padx=4)
        tj_grid.columnconfigure(1, weight=1)

        sub_frame = ttk.Frame(tj_f)
        sub_frame.pack(fill="x", pady=4)
        ttk.Label(sub_frame, text="Chroma Subsampling:").pack(anchor="w")
        tj_sub_dlg_var = tk.StringVar(value=self.tj_subsamp_var.get())
        ttk.Radiobutton(sub_frame, text="4:2:0 (Bandwidth Saver — Recommended for WAN/WiFi)", variable=tj_sub_dlg_var, value="4:2:0").pack(anchor="w", padx=8, pady=1)
        ttk.Radiobutton(sub_frame, text="4:4:4 (Crisp Text — No Subsampling for Dense Text/Coding)", variable=tj_sub_dlg_var, value="4:4:4").pack(anchor="w", padx=8, pady=1)

        # --- 5. Engine Selection ---
        eng_f = ttk.LabelFrame(content, text="Hybrid Video Engine Selection", padding=8)
        eng_f.pack(fill="x", pady=4)
        eng_var = tk.StringVar(value=self.engine_mode)
        self._active_eng_dlg_var = eng_var
        ttk.Radiobutton(eng_f, text="Auto (Hybrid: TurboJPEG when idle, QSV H.264 when gaming)",
                        variable=eng_var, value="auto",
                        command=lambda: self.set_engine_mode("auto", notify_server=True)).pack(anchor="w", pady=1)
        ttk.Radiobutton(eng_f, text="Force TurboJPEG (Zero CPU/Bandwidth on Static Screens)",
                        variable=eng_var, value="turbojpeg",
                        command=lambda: self.set_engine_mode("turbojpeg", notify_server=True)).pack(anchor="w", pady=1)
        ttk.Radiobutton(eng_f, text="Force QSV H.264 (Low-Latency 60 FPS Video Stream)",
                        variable=eng_var, value="qsv",
                        command=lambda: self.set_engine_mode("qsv", notify_server=True)).pack(anchor="w", pady=1)

        # --- 6. Audio Fidelity & Quality Mode ---
        aud_f = ttk.LabelFrame(content, text="Audio Fidelity & Channels", padding=8)
        aud_f.pack(fill="x", pady=4)
        aud_mode_dlg_var = tk.IntVar(value=self.audio_mode_var.get())
        ttk.Radiobutton(aud_f, text="High Fidelity (48 kHz Stereo) [Full Quality & Spatial Separation]",
                        variable=aud_mode_dlg_var, value=1).pack(anchor="w", pady=1)
        ttk.Radiobutton(aud_f, text="Voice / Low Bandwidth (16 kHz Mono) [Bandwidth Saver]",
                        variable=aud_mode_dlg_var, value=0).pack(anchor="w", pady=1)

        # --- Audio Noise Suppression & Filtering ---
        flt_f = ttk.LabelFrame(content, text="Audio Noise Suppression & Filtering", padding=8)
        flt_f.pack(fill="x", pady=4)
        flt_dlg_var = tk.StringVar(value=self.filter_var.get())
        ttk.Radiobutton(flt_f, text="Filters Off (Raw Direct Audio Stream)",
                        variable=flt_dlg_var, value="off").pack(anchor="w", pady=1)
        ttk.Radiobutton(flt_f, text="Filters On (Bandpass & Lowpass Noise Suppression Active)",
                        variable=flt_dlg_var, value="on").pack(anchor="w", pady=1)

        # --- 7. Input Control & View-Only Mode ---
        vo_f = ttk.LabelFrame(content, text="Input Control & Monitoring Mode", padding=8)
        vo_f.pack(fill="x", pady=4)
        vo_dlg_var = tk.BooleanVar(value=self.view_only_var.get())
        ttk.Checkbutton(vo_f, text="👁️ View-Only Mode (Completely Block Remote Mouse & Keyboard Input)",
                        variable=vo_dlg_var).pack(anchor="w", pady=2)
        ttk.Label(vo_f, text="Safe for passive monitoring: prevents accidental clicks or key presses on host.",
                   font=("Segoe UI", 8), foreground="#555555").pack(anchor="w")

        dev_dlg_var = tk.BooleanVar(value=self.dev_mode_var.get())
        ttk.Checkbutton(vo_f, text="🛠️ Dev / Loopback Mode (Prevent Host Mouse Warp on Display 2)",
                        variable=dev_dlg_var).pack(anchor="w", pady=(6, 2))
        ttk.Label(vo_f, text="Protects local developer mouse cursor when streaming secondary or virtual display over 127.0.0.1.",
                   font=("Segoe UI", 8), foreground="#555555").pack(anchor="w")

        # --- 8. Saved Host Profile Management ---
        prof_f = ttk.LabelFrame(content, text="Saved Host Profile Management", padding=8)
        prof_f.pack(fill="x", pady=4)

        cur_selected_host = self.host.get().strip() or "(None selected)"
        target_host_lbl = ttk.Label(prof_f, text=f"Active Profile: {cur_selected_host}", font=("Segoe UI", 9, "bold"), foreground="#0066cc")
        target_host_lbl.pack(anchor="w", pady=(0, 2))

        prof_desc = ttk.Label(
            prof_f,
            text="Permanently deletes the currently selected host from your saved host history\n"
                 "and securely wipes its encrypted DPAPI credentials from disk.",
            font=("Segoe UI", 8),
            foreground="#555555"
        )
        prof_desc.pack(anchor="w", pady=(0, 6))

        del_status_lbl = ttk.Label(prof_f, text="", font=("Segoe UI", 9))
        del_status_lbl.pack(anchor="w", pady=(0, 4))

        def _do_delete_profile():
            ok, msg = self.delete_host()
            if ok:
                new_sel = self.host.get().strip() or "(None selected)"
                target_host_lbl.config(text=f"Active Profile: {new_sel}")
                del_status_lbl.config(text=f"✓ {msg}", foreground="#008800")
            else:
                del_status_lbl.config(text=f"✗ {msg}", foreground="#cc0000")

        del_btn = ttk.Button(prof_f, text="🗑️ Delete Selected Host & Credentials", command=_do_delete_profile)
        del_btn.pack(anchor="w", pady=2)

        status_lbl = ttk.Label(btn_f, text="", foreground="#0066cc")
        status_lbl.pack(side="left", padx=4)

        def _apply():
            val = res_var.get()
            if val == "native":
                target_w, target_h = 0, 0
                self.res_var.set("Match Screen (Native)")
            else:
                try:
                    target_w = int(w_var.get().strip())
                    target_h = int(h_var.get().strip())
                    if not (16 <= target_w <= 8192 and 16 <= target_h <= 4320):
                        status_lbl.config(text="Resolution must be between 16x16 and 8192x4320", foreground="red")
                        return
                    matching = f"{target_w}x{target_h}"
                    if matching in ["1280x720", "1920x1080", "2560x1440", "3840x2160"]:
                        self.res_var.set(matching)
                except ValueError:
                    status_lbl.config(text="Invalid numbers for Width/Height", foreground="red")
                    return

            self.idle_ms_var.set(idle_scale_var.get())
            self.target_hz_var.set(max(15, min(144, int(hz_spin_var.get()))))
            self.rc_mode_var.set(rc_dlg_var.get())
            self.bitrate_mbps_var.set(bitrate_dlg_var.get())
            self.qp_var.set(qp_dlg_var.get())
            self.target_usage_var.set(tu_dlg_var.get())
            self.tj_quality_var.set(tj_q_dlg_var.get())
            self.tj_subsamp_var.set(tj_sub_dlg_var.get())
            self.set_engine_mode(eng_var.get(), notify_server=True)

            # Safeguard 4: Audio Filter Modal Commit Isolation
            new_flt = flt_dlg_var.get()
            self.filter_var.set(new_flt)
            filt_on = bool(new_flt == "on")

            # Apply Audio Mode
            old_aud_mode = self.audio_mode_var.get()
            new_aud_mode = aud_mode_dlg_var.get()
            self.audio_mode_var.set(new_aud_mode)
            if self.conn and self.conn.alive:
                self.conn.set_audio_filter(filt_on)
            if self.audio_running:
                if old_aud_mode != new_aud_mode:
                    self._switch_audio_mode(new_aud_mode)
                if self.conn and self.conn.alive:
                    self.conn.send_audio_sub(True, filt_on, new_aud_mode)

            # Apply View-Only
            old_vo = self.view_only_var.get()
            new_vo = vo_dlg_var.get()
            if old_vo != new_vo:
                self.view_only_var.set(new_vo)
                self._on_view_only_toggle()

            # Apply Dev Mode
            new_dev = dev_dlg_var.get()
            self.dev_mode_var.set(new_dev)
            if self.conn and self.conn.alive:
                self.conn.send_dev_mode(new_dev)

            self.apply_fine_quality()

            target_res_tuple = (target_w, target_h)
            cur_active_tuple = getattr(self, "_applied_res_tuple", (0, 0) if "Native" in cur_res else (cur_w, cur_h))
            if target_res_tuple != cur_active_tuple:
                self._applied_res_tuple = target_res_tuple
                if self.conn and self.conn.alive:
                    self.conn.send_set_res(target_w, target_h)

            display_res = f"{target_w}x{target_h}" if target_w > 0 else "Native"
            summary = f"Q{self.tj_quality_var.get()} | {self.bitrate_mbps_var.get():.1f}M @ {self.target_hz_var.get()}Hz"
            self.status_var.set(f"Applied {display_res} [{self.engine_mode} - {summary}]")
            status_lbl.config(text="✓ Settings applied live", foreground="#008800")

        def _on_close():
            self._active_eng_dlg_var = None
            try: dlg.destroy()
            except Exception: pass

        dlg.protocol("WM_DELETE_WINDOW", _on_close)
        ttk.Button(btn_f, text="Apply Settings", command=_apply).pack(side="right", padx=4)
        ttk.Button(btn_f, text="Close", command=_on_close).pack(side="right")

    def open_rekey_dialog(self):
        if not self.conn or not self.conn.alive:
            messagebox.showwarning("Rekey", "You must be connected to rekey credentials.")
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("In-Band Master Password Rekeying")
        dlg.minsize(380, 240)
        dlg.geometry("400x250")
        dlg.transient(self.root)
        dlg.grab_set()

        f = ttk.Frame(dlg, padding=16)
        f.pack(fill="both", expand=True)

        ttk.Label(f, text="In-Band Master Password Rekeying", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 4))
        ttk.Label(f, text="Update the host master password over this active session.", font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 8))

        old_row = ttk.Frame(f)
        old_row.pack(fill="x", pady=4)
        ttk.Label(old_row, text="Current Password:", width=16).pack(side="left")
        e_old_pw = ttk.Entry(old_row, show="*", width=22)
        e_old_pw.pack(side="left", fill="x", expand=True)
        cur_known = getattr(self.conn, "pw", "") or self.pw.get()
        if cur_known:
            e_old_pw.insert(0, cur_known)

        new_row = ttk.Frame(f)
        new_row.pack(fill="x", pady=4)
        ttk.Label(new_row, text="New Password:", width=16).pack(side="left")
        e_new_pw = ttk.Entry(new_row, show="*", width=22)
        e_new_pw.pack(side="left", fill="x", expand=True)
        e_new_pw.focus_set()

        status_lbl = ttk.Label(f, text="", font=("Segoe UI", 9))
        status_lbl.pack(fill="x", pady=6)

        def _do_rekey():
            old_p = e_old_pw.get()
            new_p = e_new_pw.get()
            if not new_p:
                status_lbl.config(text="New password cannot be empty", foreground="#cc0000")
                return
            if not old_p:
                status_lbl.config(text="Current password is required", foreground="#cc0000")
                return
            self._pending_rekey_pw = new_p
            self._rekey_dlg = dlg
            self._rekey_status_lbl = status_lbl
            self.conn.send_rekey(new_p, old_pw=old_p)
            status_lbl.config(text="Sending rekey request...", foreground="#0066cc")

        e_old_pw.bind("<Return>", lambda _e: _do_rekey())
        e_new_pw.bind("<Return>", lambda _e: _do_rekey())

        btn_f = ttk.Frame(f)
        btn_f.pack(fill="x", side="bottom", pady=(8, 0))
        btn_update = ttk.Button(btn_f, text="Update Password", command=_do_rekey)
        btn_update.pack(side="right", padx=(4, 0))
        btn_cancel = ttk.Button(btn_f, text="Cancel", command=dlg.destroy)
        btn_cancel.pack(side="right")

    def on_rekey_result(self, ok):
        def _cb():
            if hasattr(self, "_rekey_dlg") and self._rekey_dlg and self._rekey_dlg.winfo_exists():
                if ok:
                    new_pw = getattr(self, "_pending_rekey_pw", None)
                    if new_pw and self.conn:
                        self.conn.pw = new_pw
                        raw = self.host.get().strip()
                        target = self.hosts.get(raw)
                        if target and target.get("pw"):
                            target["pw"] = dpapi_encrypt(new_pw)
                            HOSTS_F.write_text(json.dumps(self.hosts))
                        if not self.pw.get() or self.remember.get():
                            self.pw.delete(0, "end")
                            self.pw.insert(0, new_pw)
                    if hasattr(self, "_rekey_status_lbl") and self._rekey_status_lbl:
                        self._rekey_status_lbl.config(text="✓ Password updated successfully!", foreground="#008800")
                    self.note("Server credentials updated successfully in-band!")
                    self.root.after(1200, lambda: self._rekey_dlg.destroy() if self._rekey_dlg and self._rekey_dlg.winfo_exists() else None)
                else:
                    if hasattr(self, "_rekey_status_lbl") and self._rekey_status_lbl:
                        self._rekey_status_lbl.config(text="✗ Rekey refused: invalid current password", foreground="#cc0000")
                    self.note("In-band rekey refused by server!")
            else:
                if ok:
                    self.note("Server credentials updated successfully in-band!")
                else:
                    self.note("In-band rekey failed!")
        self.post_ui(_cb)

    def _ensure_audio_out(self, mode=None):
        target_mode = self.audio_mode_var.get() if mode is None else mode
        if hasattr(self, "audio_out_stream") and self.audio_out_stream:
            if getattr(self, "current_audio_mode", None) == target_mode:
                return
            self._close_audio_out()

        try:
            try: import pyaudiowpatch as pyaudio
            except Exception:
                try: import pyaudio
                except Exception: pyaudio = None
            if pyaudio:
                if not hasattr(self, "pa_inst") or self.pa_inst is None:
                    self.pa_inst = pyaudio.PyAudio()
                ch = 2 if target_mode == 1 else 1
                rate = 48000 if target_mode == 1 else 16000
                fpb = 1920 if target_mode == 1 else 640
                self.audio_out_stream = self.pa_inst.open(
                    format=pyaudio.paInt16, channels=ch, rate=rate, output=True,
                    frames_per_buffer=fpb
                )
                self.current_audio_mode = target_mode
                mode_name = "48kHz Stereo (Hi-Fi)" if target_mode == 1 else "16kHz Mono (Voice)"
                self.note(f"audio output configured: {mode_name}")
        except Exception as e:
            self.note(f"audio output init: {e}")

    def _switch_audio_mode(self, mode):
        if getattr(self, "current_audio_mode", None) == mode:
            return
        self.audio_mode_var.set(mode)
        if self.audio_running:
            self._close_audio_out()
            self._ensure_audio_out(mode)

    def _close_audio_out(self):
        try:
            if hasattr(self, "audio_out_stream") and self.audio_out_stream:
                self.audio_out_stream.stop_stream()
                self.audio_out_stream.close()
                self.audio_out_stream = None
            self.current_audio_mode = None
            if hasattr(self, "pa_inst") and self.pa_inst:
                self.pa_inst.terminate()
                self.pa_inst = None
        except Exception: pass

    def play_audio(self, data, mode=None):
        if not self.audio_running: return
        if mode is not None and getattr(self, "current_audio_mode", None) != mode:
            self._switch_audio_mode(mode)
        else:
            self._ensure_audio_out()
        if hasattr(self, "audio_out_stream") and self.audio_out_stream:
            try:
                self.audio_out_stream.write(data, exception_on_underflow=False)
            except Exception: pass

    def on_filter_radio(self):
        filt_on = bool(self.filter_var.get() == "on")
        mode = self.audio_mode_var.get()
        if self.conn and self.conn.alive:
            self.conn.send_audio_sub(self.audio_var.get() == "on", filt_on, mode)
        st_txt = "Bandpass Filter: ON (80Hz-6.5kHz anti-loopback)" if filt_on else "Pass Filters: OFF (Raw Audio)"
        self.status_var.set(st_txt)

    def on_audio_radio(self):
        val = self.audio_var.get()
        filt_on = bool(self.filter_var.get() == "on")
        mode = self.audio_mode_var.get()
        if val == "on":
            if not self.conn or not self.conn.ready:
                self.status_var.set("connect to remote server first")
                self.audio_var.set("off"); return
            self.audio_running = True
            self._ensure_audio_out(mode)
            self.conn.send_audio_sub(True, filt_on, mode)
            mode_name = "48kHz Stereo" if mode == 1 else "16kHz Mono"
            self.status_var.set(f"audio on ({mode_name}, {'filters active' if filt_on else 'raw audio'})")
        else:
            self.audio_running = False
            if self.conn and self.conn.alive:
                self.conn.send_audio_sub(False, filt_on, mode)
            self._close_audio_out()
            self.status_var.set("audio off")

    def _on_canvas_leave(self, _e=None):
        self._last_mouse_pos = None
        self.render_frame()

    def _focus_in(self, _e=None):
        if not self.mouse_lock_var.get():
            try: self.canvas.config(cursor="arrow")
            except Exception: pass

    def _on_canvas_enter(self, e=None):
        if e:
            self._last_mouse_pos = (e.x, e.y)
        if not self.mouse_lock_var.get():
            try: self.canvas.config(cursor="arrow")
            except Exception: pass
        self.render_frame()

    def _on_canvas_configure(self, _e=None):
        if self.scale_fit.get() and self.conn and self.conn.ready and self.conn.fb:
            fb_w, fb_h = self.conn.fb.size
            if fb_w > 0 and fb_h > 0:
                self._recompute_scale_dims(fb_w, fb_h)
                if self.canvas_img_id is not None:
                    self.canvas.coords(self.canvas_img_id, self.offset_x, self.offset_y)
                    self._last_img_coords = (self.offset_x, self.offset_y)
        self.render_frame()

    def on_scale_toggle(self):
        # Invalidate dimension and coordinate caches on every mode switch
        self._cached_fit_cw = None
        self._cached_fit_ch = None
        self._cached_fit_fb_w = None
        self._cached_fit_fb_h = None
        self._cached_fit_dw = None
        self._cached_fit_dh = None
        self._cached_fit_ox = None
        self._cached_fit_oy = None
        self._last_img_coords = None
        self._last_scrollregion = None

        c = self.conn
        w_val = getattr(c, "w", 0) if c else 0
        h_val = getattr(c, "h", 0) if c else 0
        c_w = w_val if isinstance(w_val, int) and w_val > 0 else 0
        c_h = h_val if isinstance(h_val, int) and h_val > 0 else 0
        fb_sz = c.fb.size if (c and getattr(c, "fb", None) and hasattr(c.fb, "size")) else (0, 0)
        fb_w = getattr(self, "fb_w", 0) or c_w or (fb_sz[0] if isinstance(fb_sz[0], int) and fb_sz[0] > 0 else 0)
        fb_h = getattr(self, "fb_h", 0) or c_h or (fb_sz[1] if isinstance(fb_sz[1], int) and fb_sz[1] > 0 else 0)

        if self.scale_fit.get():
            # In Fit Mode, scrollbars remain permanently visible with scrollregion == (0,0,cw,ch) (full thumb)
            self.canvas.xview_moveto(0)
            self.canvas.yview_moveto(0)
            cw = max(self.canvas.winfo_width(), 10)
            ch = max(self.canvas.winfo_height(), 10)
            self.canvas.configure(scrollregion=(0, 0, cw, ch))
            self._last_scrollregion = (0, 0, cw, ch)
            if fb_w > 0 and fb_h > 0:
                dw, dh, ox, oy = self._recompute_scale_dims(fb_w, fb_h)
                if self.conn:
                    self.conn.target_scale = (dw, dh) if (dw, dh) != (fb_w, fb_h) else None
        else:
            self.offset_x = 0
            self.offset_y = 0
            if self.conn:
                self.conn.target_scale = None
            if fb_w > 0 and fb_h > 0:
                self.disp_w, self.disp_h = fb_w, fb_h
                self.canvas.configure(scrollregion=(0, 0, fb_w, fb_h))
                self._last_scrollregion = (0, 0, fb_w, fb_h)
                if self.canvas_img_id is not None:
                    self.canvas.coords(self.canvas_img_id, 0, 0)
                    self._last_img_coords = (0, 0)
        if c and getattr(c, "fb", None):
            self.render_frame(c.fb, target_np=getattr(c, "fb_np", None))
        else:
            self.render_frame()

    def update_user_count(self, count):
        def _ui():
            if count >= 2:
                self.users_var.set(f"👥 {count}/2 [STEALTH 🔒]")
                self.users_lbl.configure(foreground="#8800aa")
            else:
                self.users_var.set(f"👥 {count}/2")
                self.users_lbl.configure(foreground="#008800")
        self.post_ui(_ui)

    def on_self_destruct(self):
        ans = messagebox.askyesno(
            "EMERGENCY SELF-DESTRUCT",
            "Are you sure you want to trigger EMERGENCY SELF-DESTRUCT?\n\n"
            "This will IMMEDIATELY force close and terminate Python on ALL connected parties (Host & Clients) instantaneously.",
            icon="warning"
        )
        if ans:
            if self.conn and self.conn.alive:
                self.conn.send_emergency_kill()
            else:
                os._exit(0)

    def note(self, text):
        with self.note_lk: self.notes.append(text)

    def toggle_connect(self):
        if self.conn and self.conn.alive:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        if self.conn and self.conn.alive: self.disconnect()
        hp = self.host.get().strip()
        if not hp:
            self.status_var.set("Host / Token cannot be empty")
            return

        is_token = False
        if hp.startswith("vddmon://pair?") or (len(hp) > 40 and "pair?" in hp):
            if vddmon_p2p:
                try:
                    vddmon_p2p.parse_pairing_token(hp)
                    is_token = True
                except Exception: pass
        elif hp.startswith("vddmon://"):
            hp = hp[len("vddmon://"):].strip()

        if is_token:
            self.note("Connecting via P2P ephemeral pairing token...")
            self.last_bytes = 0
            self.last_rate_time = time.time()
            if hasattr(self, "btn_connect"):
                self.btn_connect.config(text="Disconnect")
            self.conn = Conn(hp, 0, "", self)
            self.conn.start()
            return

        # Direct LAN / IP connection
        if hp.startswith("[") and "]:" in hp:
            host_part, port_str = hp.split("]:", 1)
            host = host_part[1:].strip()
        elif ":" in hp and not hp.count(":") > 1:
            host, port_str = hp.rsplit(":", 1)
        elif hp.count(":") > 1 and not hp.startswith("["):
            host, port_str = hp, "5900"
        elif " " in hp:
            host, port_str = hp.split(None, 1)
        else:
            host, port_str = hp, "5900"
        host = host.strip().strip("[]")
        pw = self.pw.get().strip()
        if not pw:
            self.status_var.set("Password required for direct host connection")
            return

        try: port = int(port_str.strip())
        except ValueError:
            self.status_var.set("Bad port (use IP:port)")
            return

        self.note(f"Connecting to {host}:{port}...")
        self.last_bytes = 0
        self.last_rate_time = time.time()
        if hasattr(self, "btn_connect"):
            self.btn_connect.config(text="Disconnect")
        self.conn = Conn(host, port, pw, self)
        self.conn.start()
        save_key = f"{host}:{port}" if port != 5900 else host
        self.hosts[save_key] = {"port": port, "pw": dpapi_encrypt(pw) if self.remember.get() else ""}
        HOSTS_F.write_text(json.dumps(self.hosts))
        self.host["values"] = list(self.hosts)

    def delete_host(self):
        raw = self.host.get().strip()
        if not raw:
            return False, "No host selected to delete"

        target_key = None
        if raw in self.hosts:
            target_key = raw
        else:
            if raw.startswith("[") and "]:" in raw:
                h = raw.split("]:", 1)[0][1:].strip()
            elif ":" in raw and raw.count(":") == 1:
                h = raw.split(":", 1)[0].strip()
            else:
                h = raw.strip("[]")
            if h in self.hosts:
                target_key = h

        if target_key and target_key in self.hosts:
            self.hosts.pop(target_key, None)
            try:
                HOSTS_F.write_text(json.dumps(self.hosts, indent=2))
            except Exception: pass

            host_vals = list(self.hosts)
            self.host["values"] = host_vals

            if host_vals:
                next_host = host_vals[0]
                self.host.set(next_host)
                saved_pw = dpapi_decrypt(self.hosts[next_host].get("pw", ""))
                self.pw.delete(0, "end")
                if saved_pw:
                    self.pw.insert(0, saved_pw)
                    self.remember.set(True)
                else:
                    self.remember.set(False)
            else:
                self.host.set("")
                self.pw.delete(0, "end")
                self.remember.set(False)

            msg = f"Host '{target_key}' deleted (saved password wiped)"
            self.status_var.set(msg)
            self.note(msg)
            return True, msg
        else:
            msg = f"Host '{raw}' is not in saved host history"
            self.status_var.set(msg)
            return False, msg

    def disconnect(self):
        self.audio_running = False
        self._close_audio_out()
        self.audio_var.set("off")
        if self.canvas_img_id is not None:
            try: self.canvas.delete(self.canvas_img_id)
            except Exception: pass
            self.canvas_img_id = None
        if self.conn:
            for ks in list(self.held): self.conn.send_key(ks, False)
            self.held.clear(); self.mask = 0
            self.conn.close(); self.conn = None
            self.status_var.set("disconnected")
        if hasattr(self, "btn_connect"):
            self.btn_connect.config(text="Connect")
        self.update_user_count(0)

    def toggle_fs(self):
        self.root.attributes("-fullscreen", not self.root.attributes("-fullscreen"))

    def render_stream_frame(self, raw_bytes_or_img, fw=None, fh=None, raw_ptr=None):
        c = self.conn
        if not (c and c.ready):
            return

        t_rend0 = time.perf_counter()

        if isinstance(raw_bytes_or_img, Image.Image):
            img = raw_bytes_or_img
            fw, fh = img.size
            raw_bytes = img.tobytes("raw", "BGRX")
            raw_ptr = None
        else:
            raw_bytes = raw_bytes_or_img
            if fw is None or fh is None or fw <= 0 or fh <= 0:
                return

        session_w = getattr(c, "w", fw) or fw
        session_h = getattr(c, "h", fh) or fh

        self.stream_w, self.stream_h = session_w, session_h
        self.fb_w, self.fb_h = session_w, session_h

        cw = max(self.canvas.winfo_width(), 10)
        ch = max(self.canvas.winfo_height(), 10)

        if self.scale_fit.get():
            scale = min(cw / float(session_w), ch / float(session_h))
            dst_w = max(1, int(session_w * scale))
            dst_h = max(1, int(session_h * scale))
            dst_x = (cw - dst_w) // 2
            dst_y = (ch - dst_h) // 2

            self.disp_w, self.disp_h = dst_w, dst_h
            self.offset_x, self.offset_y = dst_x, dst_y

            target_sr = (0, 0, cw, ch)
            if self._last_scrollregion != target_sr:
                self.canvas.configure(scrollregion=target_sr)
                self._last_scrollregion = target_sr
        else:
            dst_w, dst_h = session_w, session_h
            dst_x, dst_y = 0, 0
            self.disp_w, self.disp_h = session_w, session_h
            self.offset_x, self.offset_y = 0, 0

            target_sr = (0, 0, session_w, session_h)
            if self._last_scrollregion != target_sr:
                self.canvas.configure(scrollregion=target_sr)
                self._last_scrollregion = target_sr

        # Direct HWND StretchDIBits blit (<1.5ms zero-allocation presentation)
        try:
            hwnd = self.canvas.winfo_id()
            hdc = user32.GetDC(hwnd)
            if hdc:
                # Mode 3: COLORONCOLOR / STRETCH_DELETESCANS
                gdi32.SetStretchBltMode(hdc, 3)

                cur_geom = (cw, ch, dst_x, dst_y, dst_w, dst_h)
                if getattr(self, "_last_blit_geom", None) != cur_geom:
                    self._last_blit_geom = cur_geom
                    # Letterbox black margins
                    if dst_y > 0:
                        gdi32.PatBlt(hdc, 0, 0, cw, dst_y, 0x00000042)
                        gdi32.PatBlt(hdc, 0, dst_y + dst_h, cw, max(0, ch - (dst_y + dst_h)), 0x00000042)
                    if dst_x > 0:
                        gdi32.PatBlt(hdc, 0, 0, dst_x, ch, 0x00000042)
                        gdi32.PatBlt(hdc, dst_x + dst_w, 0, max(0, cw - (dst_x + dst_w)), ch, 0x00000042)

                bmi = BITMAPINFOHEADER()
                bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
                bmi.biWidth = fw
                bmi.biHeight = -fh # Top-down DIB
                bmi.biPlanes = 1
                bmi.biBitCount = 32
                bmi.biCompression = 0
                bmi.biSizeImage = fw * fh * 4

                if raw_ptr:
                    p_bits = ctypes.c_void_p(raw_ptr)
                elif hasattr(raw_bytes, "ctypes"):
                    p_bits = ctypes.c_void_p(raw_bytes.ctypes.data)
                else:
                    p_bits = ctypes.cast(ctypes.c_char_p(bytes(raw_bytes)), ctypes.c_void_p)

                gdi32.StretchDIBits(
                    hdc,
                    dst_x, dst_y, dst_w, dst_h,
                    0, 0, fw, fh,
                    p_bits,
                    ctypes.byref(bmi),
                    0,
                    0x00CC0020
                )

                if not self.mouse_lock_var.get() and getattr(self, "_last_mouse_pos", None) is not None:
                    mx, my = self._last_mouse_pos
                    if 0 <= mx < cw and 0 <= my < ch:
                        try:
                            h_cur = user32.GetCursor() or user32.LoadCursorW(None, ctypes.c_void_p(32512))
                            if h_cur:
                                user32.DrawIconEx(hdc, mx, my, h_cur, 0, 0, 0, None, 3)
                        except Exception:
                            pass

                user32.ReleaseDC(hwnd, hdc)
                self._last_rendered_args = (raw_bytes_or_img, fw, fh, raw_ptr)
        except Exception:
            pass

        self.last_render_ms = (time.perf_counter() - t_rend0) * 1000.0

    def render_frame(self, target_img=None, target_np=None):
        if target_img is not None:
            self.render_stream_frame(target_img)
            return
        c = self.conn
        if not (c and c.ready):
            return
        if hasattr(c, "triple_buf"):
            latest = c.triple_buf.get_latest_frame()
            if latest is not None:
                raw_bytes, fw, fh, ts, raw_ptr = latest
                self.render_stream_frame(raw_bytes, fw, fh, raw_ptr=raw_ptr)
                return
        if getattr(c, "staging_fb", None):
            self.render_stream_frame(c.staging_fb)
            return
        elif getattr(c, "fb", None):
            self.render_stream_frame(c.fb)
            return
        if getattr(self, "_last_rendered_args", None) is not None:
            last_args = self._last_rendered_args
            self.render_stream_frame(last_args[0], last_args[1], last_args[2], raw_ptr=last_args[3])

    def on_resize(self, w, h):
        self.fb_w, self.fb_h = w, h
        self.render_frame()
        try: self.canvas.focus_set()
        except Exception: pass
        self.note(f"remote desktop resized to {w}x{h}")

    def _bind_input(self):
        r, cv = self.root, self.canvas
        r.bind("<KeyPress>", self._key_down)
        r.bind("<KeyRelease>", self._key_up)
        r.bind("<FocusOut>", self._focus_out)
        r.bind("<FocusIn>", self._focus_in)
        r.bind("<Control-v>", lambda e: self._paste_clip())
        r.bind("<Control-Alt_L>", lambda e: self._toggle_mouse_lock())
        r.bind("<Control-Alt_R>", lambda e: self._toggle_mouse_lock())
        r.bind("<Alt-Control_L>", lambda e: self._toggle_mouse_lock())
        r.bind("<Alt-Control_R>", lambda e: self._toggle_mouse_lock())
        r.bind("<Escape>", lambda e: self._release_mouse_lock_emergency())
        r.bind("<Control-Shift-Escape>", lambda e: self._release_mouse_lock_emergency())
        for btn, m in (("1", 1), ("2", 2), ("3", 4)):
            cv.bind(f"<ButtonPress-{btn}>", lambda e, m=m: self._click(e, m, True))
            cv.bind(f"<ButtonRelease-{btn}>", lambda e, m=m: self._click(e, m, False))
            cv.bind(f"<Button-{btn}>", lambda e: cv.focus_set(), add="+")
        cv.bind("<Expose>", lambda e: self.render_frame())
        cv.bind("<Motion>", self._motion)
        cv.bind("<MouseWheel>", self._wheel)

    def _release_mouse_lock_emergency(self, _e=None):
        if self.mouse_lock_var.get():
            self.mouse_lock_var.set(False)
            self._on_mouse_lock_toggle()
        else:
            self._synthetic_centering = False
            try: ctypes.windll.user32.ClipCursor(None)
            except Exception: pass
            try: self.canvas.config(cursor="arrow")
            except Exception: pass
        self.status_var.set("Mouse Lock released (Escape)")

    def _recenter_mouse(self):
        cw = max(10, self.canvas.winfo_width() // 2)
        ch = max(10, self.canvas.winfo_height() // 2)
        self._synthetic_centering = True
        try:
            sx = self.canvas.winfo_rootx() + cw
            sy = self.canvas.winfo_rooty() + ch
            ctypes.windll.user32.SetCursorPos(sx, sy)
        except Exception:
            try:
                self.root.event_generate('<Motion>', warp=True, x=cw, y=ch)
            except Exception:
                self._synthetic_centering = False

    def _on_view_only_toggle(self):
        is_vo = self.view_only_var.get()
        if is_vo:
            if self.mouse_lock_var.get():
                self.mouse_lock_var.set(False)
                self._on_mouse_lock_toggle()
            if self.conn and self.conn.alive:
                if self.held:
                    for ks in list(self.held):
                        self.conn.send_packet(0x01, bytes([0x02]) + struct.pack(">BI", 0, ks))
                    self.held.clear()
                if self.mask != 0:
                    x, y = self.last_pos
                    self.mask = 0
                    self.conn.send_packet(0x01, bytes([0x01]) + struct.pack(">BHH", 0, x, y))
            self.status_var.set("👁️ View-Only Mode ACTIVE (Input disabled)")
            self.note("View-Only enabled: remote input suppressed")
        else:
            try:
                cx = self.canvas.winfo_pointerx() - self.canvas.winfo_rootx()
                cy = self.canvas.winfo_pointery() - self.canvas.winfo_rooty()
                class _PosEvt:
                    def __init__(self, px, py):
                        self.x = px
                        self.y = py
                rx, ry = self._xy(_PosEvt(cx, cy))
                if rx >= 0 and ry >= 0:
                    self.last_pos = (rx, ry)
                    if self.conn and self.conn.alive:
                        self.conn.send_packet(0x01, bytes([0x01]) + struct.pack(">BHH", self.mask, rx, ry))
            except Exception:
                pass
            self.status_var.set("Normal Input Mode active")
            self.note("View-Only disabled: normal input resumed")

    def _toggle_mouse_lock(self):
        if self.view_only_var.get():
            self.status_var.set("⚠️ Mouse Lock unavailable in View-Only mode")
            return
        self.mouse_lock_var.set(not self.mouse_lock_var.get())
        self._on_mouse_lock_toggle()

    def _on_mouse_lock_toggle(self):
        if self.view_only_var.get():
            self.mouse_lock_var.set(False)
            self.status_var.set("⚠️ Mouse Lock unavailable in View-Only mode")
            return
        locked = self.mouse_lock_var.get()
        if locked:
            self.canvas.config(cursor="none")
            self.status_var.set("🎮 3D Game Mouse Lock ACTIVE (Press Ctrl+Alt or Esc to unlock)")
            if self.conn and self.conn.alive:
                self.conn.send_packet(0x01, bytes([0x04]))
            try:
                self.canvas.focus_set()
                self._recenter_mouse()
            except Exception: pass
        else:
            self._synthetic_centering = False
            try:
                ctypes.windll.user32.ClipCursor(None)
            except Exception: pass
            self.canvas.config(cursor="arrow")
            self.status_var.set("3D Mouse Lock released")

    def _on_engine_selected(self, event=None):
        val = self.engine_var.get()
        self.set_engine_mode(val, notify_server=True)

    def _on_res_selected(self, event=None):
        val = self.res_var.get()
        if "Native" in val:
            target_w, target_h = 0, 0
        else:
            try:
                parts = val.split("x")
                target_w, target_h = int(parts[0]), int(parts[1])
            except Exception:
                target_w, target_h = 1280, 720
        if self.conn and self.conn.alive:
            self.conn.send_set_res(target_w, target_h)
            display = f"{target_w}x{target_h}" if target_w > 0 else "Native"
            self.note(f"requested streaming resolution: {display}")

    def _on_quality_selected(self, event=None):
        self.apply_fine_quality()

    def _focus_out(self, e=None):
        if self.mouse_lock_var.get():
            self.mouse_lock_var.set(False)
            self._on_mouse_lock_toggle()
            try: ctypes.windll.user32.ClipCursor(None)
            except Exception: pass
        self._synthetic_centering = False
        try: self.canvas.config(cursor="arrow")
        except Exception: pass
        if self.conn and self.conn.alive:
            if self.held:
                for ks in list(self.held):
                    self.conn.send_key(ks, False)
                self.held.clear()
            if self.mask != 0:
                self.mask = 0
                self.conn.send_pointer(0, self.last_pos[0], self.last_pos[1])
            self.mask = 0

    def _ks(self, e):
        if e.keysym in NAMED: return NAMED[e.keysym]
        if len(e.keysym) == 1:
            if e.keysym.isalpha(): return ord(e.keysym.lower())
            if e.keysym.isdigit(): return ord(e.keysym)
        if len(e.char) == 1:
            o = ord(e.char)
            if 32 <= o < 127: return o
            if 1 <= o <= 26: return 0x61 + o - 1
        return None

    def _key_down(self, e):
        if self.view_only_var.get(): return
        ks = self._ks(e)
        if ks and self.conn and self.conn.alive:
            self.held.add(ks); self.conn.send_key(ks, True)

    def _key_up(self, e):
        if self.view_only_var.get(): return
        ks = self._ks(e)
        if ks and self.conn and self.conn.alive:
            self.held.discard(ks); self.conn.send_key(ks, False)

    def _xy(self, e):
        c = self.conn
        if not (c and c.ready): return 0, 0
        srv_w = getattr(self, "stream_w", 0) or getattr(c, "w", 0) or getattr(self, "fb_w", 0)
        srv_h = getattr(self, "stream_h", 0) or getattr(c, "h", 0) or getattr(self, "fb_h", 0)
        if not (isinstance(srv_w, int) and isinstance(srv_h, int) and srv_w > 0 and srv_h > 0): return 0, 0
        if self.scale_fit.get() and self.disp_w > 0 and self.disp_h > 0:
            cx = self.canvas.canvasx(e.x)
            cy = self.canvas.canvasy(e.y)
            # Normalized letterboxed coordinate translation with strict bounds check
            norm_x = (cx - self.offset_x) / float(self.disp_w)
            norm_y = (cy - self.offset_y) / float(self.disp_h)
            if norm_x < 0.0 or norm_x > 1.0 or norm_y < 0.0 or norm_y > 1.0:
                return -1, -1
            rx = int(norm_x * srv_w)
            ry = int(norm_y * srv_h)
            return min(srv_w - 1, max(0, rx)), min(srv_h - 1, max(0, ry))
        else:
            abs_x = self.canvas.canvasx(e.x)
            abs_y = self.canvas.canvasy(e.y)
            if abs_x < 0 or abs_x >= srv_w or abs_y < 0 or abs_y >= srv_h:
                return -1, -1
            return int(abs_x), int(abs_y)

    def _click(self, e, m, down):
        try: self.canvas.focus_set()
        except Exception: pass
        if self.view_only_var.get(): return
        if not (self.conn and self.conn.ready and self.conn.alive): return
        x, y = self._xy(e)
        if x < 0 or y < 0: return
        self.mask = self.mask | m if down else self.mask & ~m
        self.last_pos = (x, y)
        self.conn.send_pointer(self.mask, x, y)
        # Immediately re-present cached frame so Tkinter focus/click redraw does not blank canvas to black
        self.render_frame()

    def _motion(self, e):
        self._last_mouse_pos = (e.x, e.y)
        if self.view_only_var.get(): return
        if not (self.conn and self.conn.ready and self.conn.alive): return
        if self.mouse_lock_var.get():
            # If this event was synthetic from centering/warping, ignore it to prevent camera oscillation
            if getattr(self, "_synthetic_centering", False):
                self._synthetic_centering = False
                return

            cw = max(10, self.canvas.winfo_width() // 2)
            ch = max(10, self.canvas.winfo_height() // 2)
            dx = e.x - cw
            dy = e.y - ch
            if dx == 0 and dy == 0:
                return

            self.conn.send_relative_pointer(self.mask, dx, dy)
            self._recenter_mouse()
            return

        x, y = self._xy(e)
        if x >= 0 and y >= 0:
            self.last_pos = (x, y)
            self.conn.send_pointer(self.mask, x, y)

    def _wheel(self, e):
        if self.view_only_var.get(): return
        if not (self.conn and self.conn.ready and self.conn.alive): return
        b = 8 if e.delta > 0 else 16
        x, y = self.last_pos
        self.conn.send_pointer(self.mask | b, x, y)
        self.conn.send_pointer(self.mask, x, y)

    def _paste_clip(self):
        if self.view_only_var.get(): return
        try:
            txt = self.root.clipboard_get()
            if self.conn: self.conn.send_cut(txt)
        except Exception: pass

    def tick_internal(self):
        c = self.conn
        if not (c and c.ready):
            return
        if not getattr(c, "_init_synced", False):
            c._init_synced = True
            self._on_quality_selected()
            init_res = self.res_var.get()
            if "Native" not in init_res:
                self._on_res_selected()
        if getattr(c, "user_count", None) is not None:
            cnt = c.user_count
            c.user_count = None
            self.update_user_count(cnt)
        if c.pending_resize:
            w, h = c.pending_resize
            c.pending_resize = None
            self.on_resize(w, h)
        cut = None
        with c.lk:
            if c.pending_cut is not None:
                cut = c.pending_cut
                c.pending_cut = None
        if cut is not None:
            self.root.clipboard_clear()
            self.root.clipboard_append(cut)

        now_perf = time.perf_counter()
        if len(self.client_perf_samples) >= 60 and (now_perf - self.last_perf_log_t) >= 2.0:
            avg_recv = sum(s[0] for s in self.client_perf_samples) / len(self.client_perf_samples)
            avg_dec = sum(s[1] for s in self.client_perf_samples) / len(self.client_perf_samples)
            avg_rend = sum(s[2] for s in self.client_perf_samples) / len(self.client_perf_samples)
            fps = len(self.client_perf_samples) / (now_perf - self.last_perf_log_t)
            transit_ms = c.last_rtt_ms / 2.0
            total_pipeline = c.srv_cap_ms + c.srv_enc_ms + c.srv_send_ms + transit_ms + avg_dec + avg_rend
            print(f"[CLIENT PERF (60-frame avg)] Server[Cap: {c.srv_cap_ms:.1f}ms, Enc: {c.srv_enc_ms:.1f}ms, SndWait: {c.srv_send_ms:.1f}ms] | Transit: {transit_ms:.1f}ms | Client[Dec: {avg_dec:.1f}ms, Blit: {avg_rend:.1f}ms] | Total Latency: {total_pipeline:.1f}ms | UI FPS: {fps:.1f}", flush=True)
            self.last_perf_log_t = now_perf
            self.perf_telemetry_str = f" | Lag: ~{total_pipeline:.0f}ms [Cap {c.srv_cap_ms:.0f}ms, Enc {c.srv_enc_ms:.0f}ms, Net {transit_ms:.0f}ms, Dec {avg_dec:.0f}ms, Blit {avg_rend:.0f}ms]"

        now = time.time()
        if now - self.last_rate_time >= 0.5:
            dt = now - self.last_rate_time
            db = c.bytes_recv - self.last_bytes
            rate = db / dt if dt > 0 else 0
            perf_txt = getattr(self, "perf_telemetry_str", "")
            self.data_var.set(f"Data: {fmt_bytes(c.bytes_recv)} ({fmt_bytes(rate)}/s){perf_txt}")
            self.last_bytes = c.bytes_recv
            self.last_rate_time = now

        if now - getattr(self, "last_ping_t", 0.0) >= 1.0:
            self.last_ping_t = now
            c.send_ping()

        if not hasattr(self, "_last_trim"):
            self._last_trim = 0.0
        if now - self._last_trim >= 20.0:
            self._last_trim = now
            try:
                gc.collect()
                psapi = getattr(ctypes.windll, "psapi", None)
                if psapi:
                    psapi.EmptyWorkingSet(ctypes.windll.kernel32.GetCurrentProcess())
            except Exception: pass

    def tick(self):
        is_conn = bool(self.conn and self.conn.alive)
        if hasattr(self, "btn_connect"):
            expected_text = "Disconnect" if is_conn else "Connect"
            if self.btn_connect["text"] != expected_text:
                self.btn_connect.config(text=expected_text)
        with self.note_lk:
            notes, self.notes = self.notes, []
        if notes: self.status_var.set(notes[-1])
        with self._ui_queue_lk:
            tasks, self._ui_queue = list(self._ui_queue), collections.deque()
        for fn, args, kwargs in tasks:
            try: fn(*args, **kwargs)
            except Exception: pass

        c = self.conn
        if c and c.ready and c.alive:
            try:
                self.tick_internal()

                # Pull and render latest frame from TripleFrameBuffer
                rendered_new = False
                if hasattr(c, "triple_buf"):
                    frame_data = c.triple_buf.get_latest_frame()
                    if frame_data is not None:
                        raw_bytes, fw, fh, ts, raw_ptr = frame_data
                        last_ts = getattr(self, "_last_rendered_ts", 0.0)
                        if ts != last_ts:
                            self._last_rendered_ts = ts
                            self.render_stream_frame(raw_bytes, fw, fh, raw_ptr=raw_ptr)
                            rendered_new = True
                            self.client_perf_samples.append((
                                getattr(c, "last_recv_ms", 0.0),
                                getattr(c, "last_dec_ms", 0.0),
                                getattr(self, "last_render_ms", 0.0)
                            ))

                # Fallback for TurboJPEG / frame_slot
                if not rendered_new and hasattr(c, "frame_slot_lk"):
                    slot_data = None
                    with c.frame_slot_lk:
                        if c.frame_slot is not None:
                            slot_data = c.frame_slot
                            c.frame_slot = None
                    if slot_data is not None:
                        img = slot_data[0]
                        raw_bgrx = slot_data[3] if len(slot_data) > 3 else None
                        if raw_bgrx is not None and len(slot_data) > 2 and slot_data[2]:
                            _, _, (_, _, fw, fh), _ = slot_data
                            self.render_stream_frame(raw_bgrx, fw, fh)
                        elif img is not None:
                            self.render_stream_frame(img)
                        self.client_perf_samples.append((
                            getattr(c, "last_recv_ms", 0.0),
                            getattr(c, "last_dec_ms", 0.0),
                            getattr(self, "last_render_ms", 0.0)
                        ))
            except Exception as e:
                traceback.print_exc()

        self.root.after(8, self.tick)

if __name__ == "__main__":
    App().root.mainloop()