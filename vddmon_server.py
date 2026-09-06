#!/usr/bin/env python3
"""vddmon server v0.2.0 — Hybrid Dual-Engine (TurboJPEG / QSV H.264) Remote Desktop Suite.

Hardware Target: Intel Core i5-2400S (Sandy Bridge) / Intel HD Graphics 2000 (GT1).
Single-Port Multiplexed Transport: Port 5900 ONLY.
"""
import argparse, atexit, collections, ctypes, gc, hashlib, hmac, io, json
import os, queue, secrets, shutil, socket, struct, subprocess, sys, threading, time, zlib
from ctypes import wintypes
from pathlib import Path

def queue_put_drop_stale(target_queue, item):
    """Enforces maxsize=1 single-slot queue with non-blocking atomic stale-frame discard."""
    try:
        target_queue.put_nowait(item)
    except queue.Full:
        try:
            target_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            target_queue.put_nowait(item)
        except queue.Full:
            pass


VERSION = "0.2.0"
BASE = Path(os.environ.get("LOCALAPPDATA", ".")) / "VDDMon"
CFG_F, AUTH_F, LOG_F, LOCK_F = (BASE/"config.json", BASE/"auth.json",
                                BASE/"server.log", BASE/"instance.lock")
BASE.mkdir(parents=True, exist_ok=True)

try:    import numpy as np
except Exception: np = None
try:    import mss
except Exception: mss = None
try:    from PIL import Image
except Exception: Image = None
try:    from turbojpeg import TurboJPEG, TJSAMP_420, TJSAMP_444, TJPF_BGRX, TJFLAG_FASTDCT
except Exception: TurboJPEG = None; TJPF_BGRX = None; TJSAMP_420 = 2; TJSAMP_444 = 0; TJFLAG_FASTDCT = 2048
try:
    import dxcam
    os.environ["DXCAM_WINRT_CURSOR_CAPTURE"] = "0"
    _orig_dxcam_create = dxcam.create
    def _safe_dxcam_create(*args, **kwargs):
        kwargs.pop("capture_cursor", None)
        kwargs.setdefault("max_buffer_len", 1)
        return _orig_dxcam_create(*args, **kwargs)
    dxcam.create = _safe_dxcam_create
except Exception:
    dxcam = None

def find_dxgi_target(target_dev, mon_coords=None):
    """Enumerates DXGI adapters and outputs to locate the exact (device_idx, output_idx)
    matching the target monitor device name or desktop coordinates.
    Prevents DXGI_ERROR_UNSUPPORTED crashes on dual-GPU laptops."""
    if not dxcam:
        return None, None
    try:
        from dxcam._libs.dxgi import DXGI_OUTPUT_DESC
        factory = dxcam.__dict__.get("__factory")
        if not factory or not hasattr(factory, "devices"):
            return None, None
        for dev_idx, dev in enumerate(factory.devices):
            try:
                outputs = dev.enum_outputs()
            except Exception:
                continue
            for out_idx, out in enumerate(outputs):
                try:
                    desc = DXGI_OUTPUT_DESC()
                    out.GetDesc(ctypes.byref(desc))
                    if not desc.AttachedToDesktop:
                        continue
                    if target_dev and desc.DeviceName == target_dev:
                        return dev_idx, out_idx
                    if mon_coords:
                        c = desc.DesktopCoordinates
                        if (c.left, c.top) == (mon_coords[0], mon_coords[1]):
                            return dev_idx, out_idx
                except Exception:
                    pass
    except Exception:
        pass
    return None, None


CAP = None
try:
    import vddmon_p2p
except Exception:
    vddmon_p2p = None

ACTIVE_PAIRING_SESSION = None
RUNNING = True
TS = 32  # tile size
LOG_RING = collections.deque(maxlen=1000)

# ---------------------------------------------------------------- Win32 Initialization
try: ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try: ctypes.windll.user32.SetProcessDPIAware()
    except Exception: pass

user32 = ctypes.windll.user32
gdi32  = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32
winmm = getattr(ctypes.windll, "winmm", None)

try:
    kernel32.AttachConsole.argtypes = [wintypes.DWORD]
    kernel32.AttachConsole.restype = wintypes.BOOL
    if kernel32.AttachConsole(0xFFFFFFFF):
        try:
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        except Exception: pass
    elif sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
        sys.stderr = open(os.devnull, "w")
except Exception:
    pass

user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.restype = wintypes.BOOL
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.SelectObject.restype = wintypes.HANDLE
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
kernel32.VirtualLock.restype = wintypes.BOOL
kernel32.VirtualLock.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.GetCurrentThread.restype = wintypes.HANDLE
kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.SetPriorityClass.restype = wintypes.BOOL
kernel32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
kernel32.SetThreadPriority.restype = wintypes.BOOL

psapi = getattr(ctypes.windll, "psapi", None)
if psapi:
    psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
    psapi.EmptyWorkingSet.restype = wintypes.BOOL

user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.OpenInputDesktop.restype = wintypes.HANDLE
user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
user32.SetThreadDesktop.restype = wintypes.BOOL
user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
user32.CloseDesktop.restype = wintypes.BOOL
user32.CloseDesktop.argtypes = [wintypes.HANDLE]

def attach_input_desktop():
    """Binds calling thread to active input desktop with Per-Monitor DPI context."""
    try:
        if hasattr(user32, "SetThreadDpiAwarenessContext"):
            user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        hdesk = user32.OpenInputDesktop(0, False, 0x0100)
        if hdesk:
            user32.SetThreadDesktop(hdesk)
            user32.CloseDesktop(hdesk)
    except Exception: pass

def say(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    try: print(line, flush=True)
    except Exception: pass
    LOG_RING.append(line)

def flush_logs_to_disk():
    if not LOG_RING: return
    try:
        with open(LOG_F, "a", encoding="utf-8") as f:
            while LOG_RING:
                f.write(LOG_RING.popleft() + "\n")
    except Exception: pass

atexit.register(flush_logs_to_disk)

MAX_PAYLOAD_LEN = 16 * 1024 * 1024  # 16 MB bounds clamp to prevent allocation DoS

def rxn(s, n, max_n=MAX_PAYLOAD_LEN):
    if n < 0 or n > max_n:
        return None
    b = bytearray()
    while len(b) < n:
        c = s.recv(min(65536, n - len(b)))
        if not c: return None
        b += c
    return bytes(b)

# ---------------------------------------------------------------- Win32 Structures
class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]

class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]

user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]

class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

class CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hCursor", wintypes.HANDLE),
        ("ptScreenPos", POINT)
    ]

class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", ctypes.c_void_p),
        ("hbmColor", ctypes.c_void_p)
    ]

user32.GetCursorInfo.restype = wintypes.BOOL
user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
user32.GetIconInfo.restype = wintypes.BOOL
user32.GetIconInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(ICONINFO)]
user32.DrawIconEx.restype = wintypes.BOOL
user32.DrawIconEx.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HANDLE,
    ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.HBRUSH, wintypes.UINT
]
user32.ClipCursor.restype = wintypes.BOOL
user32.ClipCursor.argtypes = [ctypes.POINTER(wintypes.RECT)]

class DEVMODEW(ctypes.Structure):
    _fields_ = [
        ('dmDeviceName', wintypes.WCHAR * 32),
        ('dmSpecVersion', wintypes.WORD),
        ('dmDriverVersion', wintypes.WORD),
        ('dmSize', wintypes.WORD),
        ('dmDriverExtra', wintypes.WORD),
        ('dmFields', wintypes.DWORD),
        ('dmPosition_x', wintypes.LONG),
        ('dmPosition_y', wintypes.LONG),
        ('dmDisplayOrientation', wintypes.DWORD),
        ('dmDisplayFixedOutput', wintypes.DWORD),
        ('dmColor', wintypes.SHORT),
        ('dmDuplex', wintypes.SHORT),
        ('dmYResolution', wintypes.SHORT),
        ('dmTTOption', wintypes.SHORT),
        ('dmCollate', wintypes.SHORT),
        ('dmFormName', wintypes.WCHAR * 32),
        ('dmLogPixels', wintypes.WORD),
        ('dmBitsPerPel', wintypes.DWORD),
        ('dmPelsWidth', wintypes.DWORD),
        ('dmPelsHeight', wintypes.DWORD),
        ('dmDisplayFlags', wintypes.DWORD),
        ('dmDisplayFrequency', wintypes.DWORD),
        ('dmICMMethod', wintypes.DWORD),
        ('dmICMIntent', wintypes.DWORD),
        ('dmMediaType', wintypes.DWORD),
        ('dmDitherType', wintypes.DWORD),
        ('dmReserved1', wintypes.DWORD),
        ('dmReserved2', wintypes.DWORD),
        ('dmPanningWidth', wintypes.DWORD),
        ('dmPanningHeight', wintypes.DWORD),
    ]

class DISPLAY_DEVICE(ctypes.Structure):
    _fields_ = [
        ('cb', wintypes.DWORD),
        ('DeviceName', wintypes.WCHAR * 32),
        ('DeviceString', wintypes.WCHAR * 128),
        ('StateFlags', wintypes.DWORD),
        ('DeviceID', wintypes.WCHAR * 128),
        ('DeviceKey', wintypes.WCHAR * 128)
    ]

class LUID(ctypes.Structure):
    _fields_ = [('LowPart', wintypes.DWORD), ('HighPart', wintypes.LONG)]

class DISPLAYCONFIG_RATIONAL(ctypes.Structure):
    _fields_ = [('Numerator', wintypes.UINT), ('Denominator', wintypes.UINT)]

class DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [
        ('adapterId', LUID),
        ('id', wintypes.UINT),
        ('modeInfoIdx', wintypes.UINT),
        ('statusFlags', wintypes.UINT)
    ]

class DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [
        ('adapterId', LUID),
        ('id', wintypes.UINT),
        ('modeInfoIdx', wintypes.UINT),
        ('outputTechnology', wintypes.UINT),
        ('rotation', wintypes.UINT),
        ('scaling', wintypes.UINT),
        ('refreshRate', DISPLAYCONFIG_RATIONAL),
        ('scanLineOrdering', wintypes.UINT),
        ('targetAvailable', wintypes.BOOL),
        ('statusFlags', wintypes.UINT)
    ]

class DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
    _fields_ = [
        ('sourceInfo', DISPLAYCONFIG_PATH_SOURCE_INFO),
        ('targetInfo', DISPLAYCONFIG_PATH_TARGET_INFO),
        ('flags', wintypes.UINT)
    ]

class DISPLAYCONFIG_MODE_INFO(ctypes.Structure):
    _fields_ = [
        ('infoType', wintypes.UINT),
        ('id', wintypes.UINT),
        ('adapterId', LUID),
        ('modeInfo', ctypes.c_byte * 48)
    ]

class DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [
        ('type', wintypes.UINT),
        ('size', wintypes.UINT),
        ('adapterId', LUID),
        ('id', wintypes.UINT)
    ]

class DISPLAYCONFIG_TARGET_DEVICE_NAME(ctypes.Structure):
    _fields_ = [
        ('header', DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ('flags', wintypes.UINT),
        ('outputTechnology', wintypes.UINT),
        ('edidManufactureId', wintypes.USHORT),
        ('edidProductCodeId', wintypes.USHORT),
        ('connectorInstance', wintypes.UINT),
        ('monitorFriendlyDeviceName', wintypes.WCHAR * 64),
        ('monitorDevicePath', wintypes.WCHAR * 128)
    ]


def ensure_physical_primary():
    """Guarantees that the physical monitor is always Primary."""
    try:
        dd = DISPLAY_DEVICE()
        dd.cb = ctypes.sizeof(DISPLAY_DEVICE)
        i = 0
        primary_is_virtual = False
        physical_dev = None
        while user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
            if dd.StateFlags & 0x1:
                is_prim = bool(dd.StateFlags & 0x4)
                is_virt = ("Virtual" in dd.DeviceString or "MTT" in dd.DeviceString or "Idd" in dd.DeviceString)
                if is_prim and is_virt:
                    primary_is_virtual = True
                if not is_virt and physical_dev is None:
                    physical_dev = dd.DeviceName
            i += 1
        if primary_is_virtual and physical_dev:
            say(f"SAFETY GUARD: Restoring physical screen {physical_dev} as Primary Display!")
            dm = DEVMODEW()
            dm.dmSize = ctypes.sizeof(DEVMODEW)
            dm.dmFields = 0x00000020
            dm.dmPosition_x, dm.dmPosition_y = 0, 0
            user32.ChangeDisplaySettingsExW(physical_dev, ctypes.byref(dm), None, 0x00000001 | 0x00000008, None)
            user32.ChangeDisplaySettingsExW(None, None, None, 0, None)
    except Exception: pass

def enum_monitors():
    ensure_physical_primary()
    out = []
    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    def cb(hm, hdc, lpr, lp):
        r = lpr.contents
        mi = MONITORINFO(); mi.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(hm, ctypes.byref(mi))
        out.append({"hmon": hm, "rect": (r.left, r.top, r.right, r.bottom),
                    "primary": bool(mi.dwFlags & 1)})
        return True
    user32.EnumDisplayMonitors(0, None, cb, 0)
    return out

def unclip_and_recenter_cursor():
    """Immediately unclips Windows cursor restrictions and recenters cursor onto primary physical display."""
    try:
        user32.ClipCursor(None)
    except Exception: pass
    try:
        mons = enum_monitors()
        if mons:
            prim = next((m for m in mons if m.get("primary")), mons[0])
            cx = (prim["rect"][0] + prim["rect"][2]) // 2
            cy = (prim["rect"][1] + prim["rect"][3]) // 2
            user32.SetCursorPos(cx, cy)
    except Exception: pass
    try:
        user32.ClipCursor(None)
    except Exception: pass

PHYSICAL_BOUNDS = None
IS_INJECTING = False
LAST_INJECT_TIME = 0.0
INJECTED_POS = (0, 0)

def get_physical_desktop_bounds():
    """Calculates the dynamic bounding box (min_x, min_y, max_x, max_y) of all active physical displays."""
    min_x, min_y = 0, 0
    max_x, max_y = 1920, 1080
    found = False
    try:
        dd = DISPLAY_DEVICE()
        dd.cb = ctypes.sizeof(DISPLAY_DEVICE)
        i = 0
        while user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
            if (dd.StateFlags & 0x1): # Attached to desktop
                is_virt = any(k in dd.DeviceString.lower() or k in dd.DeviceName.lower() or k in dd.DeviceID.lower() for k in ("mtt", "vdd", "virtual", "idd"))
                if not is_virt:
                    cur_dm = DEVMODEW()
                    cur_dm.dmSize = ctypes.sizeof(DEVMODEW)
                    if user32.EnumDisplaySettingsW(dd.DeviceName, -1, ctypes.byref(cur_dm)):
                        px = cur_dm.dmPosition_x
                        py = cur_dm.dmPosition_y
                        pw = cur_dm.dmPelsWidth
                        ph = cur_dm.dmPelsHeight
                        if not found:
                            min_x, min_y = px, py
                            max_x, max_y = px + pw, py + ph
                            found = True
                        else:
                            min_x = min(min_x, px)
                            min_y = min(min_y, py)
                            max_x = max(max_x, px + pw)
                            max_y = max(max_y, py + ph)
            i += 1
    except Exception:
        pass
    return min_x, min_y, max_x, max_y

def cursor_barrier_worker():
    """Continuously monitors cursor position and prevents physical host mouse from escaping into virtual monitor space,
    while preserving uninhibited injection and interaction from remote clients."""
    global PHYSICAL_BOUNDS, IS_INJECTING
    pt = POINT()
    last_bounds_check = 0.0
    last_clip_time = 0.0
    min_x, min_y, max_x, max_y = 0, 0, 1920, 1080
    phys_rc = None

    while RUNNING:
        try:
            now = time.monotonic()
            if now - last_bounds_check > 2.0 or PHYSICAL_BOUNDS is None:
                PHYSICAL_BOUNDS = get_physical_desktop_bounds()
                min_x, min_y, max_x, max_y = PHYSICAL_BOUNDS
                phys_rc = wintypes.RECT(min_x, min_y, max_x, max_y)
                last_bounds_check = now

            # If client is injecting or actively dragging on Screen 2: DO NOT clamp the cursor
            is_active = IS_INJECTING
            if CAP and hasattr(CAP, "injector") and CAP.injector and CAP.injector.button_mask != 0:
                is_active = True

            if not is_active and phys_rc is not None:
                # Keep ClipCursor engaged on physical monitors so cursor cannot cross to virtual screens
                if now - last_clip_time > 0.2:
                    user32.ClipCursor(ctypes.byref(phys_rc))
                    last_clip_time = now

                if user32.GetCursorPos(ctypes.byref(pt)):
                    cx, cy = pt.x, pt.y
                    clamped_x = max(min_x, min(max_x - 1, cx))
                    clamped_y = max(min_y, min(max_y - 1, cy))
                    if clamped_x != cx or clamped_y != cy:
                        user32.SetCursorPos(clamped_x, clamped_y)
                        user32.ClipCursor(ctypes.byref(phys_rc))
        except Exception:
            pass
        time.sleep(0.01)

# ---------------------------------------------------------------- GPU 3D & Gaming Monitor
class GPUMonitor:
    """Windows native zero-overhead GPU 3D Engine & Fullscreen Game Detector."""
    def __init__(self):
        self.hQuery = ctypes.c_void_p()
        self.hCounter = ctypes.c_void_p()
        self.valid = False
        self.last_check = 0.0
        self.cached_load = 0.0
        self.is_gaming = False
        try:
            pdh = ctypes.windll.pdh
            if pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.hQuery)) == 0:
                if pdh.PdhAddEnglishCounterW(self.hQuery,
                                             '\\GPU Engine(*engtype_3D*)\\Utilization Percentage',
                                             0, ctypes.byref(self.hCounter)) == 0:
                    pdh.PdhCollectQueryData(self.hQuery)
                    self.valid = True
        except Exception: pass

    def check_load(self):
        now = time.monotonic()
        if now - self.last_check < 2.0:
            return self.cached_load, self.is_gaming
        self.last_check = now
        try:
            hwnd = user32.GetForegroundWindow()
            if hwnd:
                rect = (ctypes.c_long * 4)()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                sw = user32.GetSystemMetrics(0)
                sh = user32.GetSystemMetrics(1)
                is_zoomed = bool(user32.IsZoomed(hwnd))
                self.is_gaming = bool(rect[0] <= 0 and rect[1] <= 0 and
                                      rect[2] >= sw and rect[3] >= sh and
                                      user32.GetDesktopWindow() != hwnd and not is_zoomed)
        except Exception:
            self.is_gaming = False

        if self.valid:
            try:
                pdh = ctypes.windll.pdh
                if pdh.PdhCollectQueryData(self.hQuery) == 0:
                    c_type = ctypes.c_ulong()
                    c_val = ctypes.c_double()
                    if pdh.PdhGetFormattedCounterValue(self.hCounter, 0x00000200,
                                                       ctypes.byref(c_type), ctypes.byref(c_val)) == 0:
                        cur = float(c_val.value)
                        self.cached_load = (0.7 * self.cached_load) + (0.3 * cur)
            except Exception: pass
        return self.cached_load, self.is_gaming

# ---------------------------------------------------------------- Server Process Governor
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
NORMAL_PRIORITY_CLASS       = 0x00000020

def set_server_priority_and_affinity(gaming_mode: bool):
    """Dynamically applies or restores server priority and CPU core affinity via kernel32."""
    try:
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.SetPriorityClass.restype = ctypes.c_int
        k32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        k32.SetProcessAffinityMask.restype = ctypes.c_int

        h_proc = k32.GetCurrentProcess()
        num_cores = max(1, os.cpu_count() or 4)

        if gaming_mode:
            k32.SetPriorityClass(h_proc, BELOW_NORMAL_PRIORITY_CLASS)
            mask_last = 1 << (num_cores - 1)
            k32.SetProcessAffinityMask(h_proc, ctypes.c_size_t(mask_last))
        else:
            k32.SetPriorityClass(h_proc, NORMAL_PRIORITY_CLASS)
            mask_all = (1 << num_cores) - 1
            k32.SetProcessAffinityMask(h_proc, ctypes.c_size_t(mask_all))
        return True
    except Exception:
        return False

# ---------------------------------------------------------------- Hardware Codecs
class TurboJPEGEncoder:
    """Engine 0: Fast JPEG tile compressor using Quality and 4:2:0 / 4:4:4 subsampling."""
    def __init__(self, quality=85, subsamp=0):
        self.quality = quality
        self.subsamp = subsamp  # 0 = 4:2:0, 1 = 4:4:4
        self.tj = None
        if TurboJPEG:
            try: self.tj = TurboJPEG()
            except Exception: pass

    def encode(self, bgra_tile, quality=None):
        """Encodes (H, W, 4) uint8 BGRA tile to JPEG bytes."""
        if bgra_tile is None or bgra_tile.size == 0: return b""
        if bgra_tile.shape[0] < 2 or bgra_tile.shape[1] < 2: return b""
        q = quality or self.quality
        samp = TJSAMP_444 if (self.subsamp == 1 and TJSAMP_444 is not None) else (TJSAMP_420 if TJSAMP_420 is not None else 2)
        if self.tj:
            try:
                pf = TJPF_BGRX if TJPF_BGRX is not None else 2
                flags = TJFLAG_FASTDCT if TJFLAG_FASTDCT is not None else 2048
                return self.tj.encode(bgra_tile, quality=q, subsamp=samp, pixel_format=pf, flags=flags)
            except Exception: pass
        if Image:
            try:
                im = Image.fromarray(bgra_tile[:, :, [2, 1, 0]], 'RGB')
                buf = io.BytesIO()
                im.save(buf, format='JPEG', quality=q, subsampling=(0 if self.subsamp == 1 else 2))
                return buf.getvalue()
            except Exception: return b""
        return b""

def ensure_qsv_dll():
    candidates = []
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidates.append(Path(sys._MEIPASS) / "qsv_encoder.dll")
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "qsv_encoder.dll")
    candidates.append(Path(__file__).resolve().parent / "qsv_encoder.dll")
    for cand in candidates:
        if cand.exists():
            return str(cand)

    dll_path = Path(__file__).resolve().parent / "qsv_encoder.dll"
    c_path = dll_path.with_suffix(".c")
    if not c_path.exists():
        return None
    vcvars_candidates = [
        r"C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat",
        r"C:\Program Files\Microsoft Visual Studio\2022\Professional\VC\Auxiliary\Build\vcvars64.bat",
        r"C:\Program Files\Microsoft Visual Studio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat",
        r"C:\Program Files (x86)\Microsoft Visual Studio\2019\Community\VC\Auxiliary\Build\vcvars64.bat",
        r"C:\Program Files (x86)\Microsoft Visual Studio\2019\Professional\VC\Auxiliary\Build\vcvars64.bat"
    ]
    for vc in vcvars_candidates:
        if os.path.exists(vc):
            cmd = f'call "{vc}" && cl.exe /O2 /Oi /Ot /Oy /arch:AVX /LD "{c_path}" /Fe:"{dll_path}"'
            try:
                subprocess.run(f'cmd.exe /c "{cmd}"', shell=True, capture_output=True, cwd=str(dll_path.parent))
                if dll_path.exists(): return str(dll_path)
            except Exception: pass
    try:
        subprocess.run(["gcc", "-O3", "-msse2", "-shared", "-o", str(dll_path), str(c_path)], capture_output=True)
        if dll_path.exists(): return str(dll_path)
    except Exception: pass
    return str(dll_path) if dll_path.exists() else None

class QSVEncoder:
    """Engine 1: Native in-process Intel Quick Sync Video (libmfxhw64.dll) via qsv_encoder.dll."""
    def __init__(self, w, h, on_nal_cb, target_kbps=12000, max_kbps=18000, fps=60, rc_mode=0, qp=21, target_usage=4):
        self.lock = threading.RLock()
        self.w, self.h = w, h
        self.on_nal_cb = on_nal_cb
        self.target_kbps = max(100, min(60000, int(target_kbps)))
        self.max_kbps = min(60000, max(self.target_kbps, int(max_kbps)))
        self.fps = fps
        self.rc_mode = rc_mode
        self.qp = qp
        self.target_usage = target_usage
        self.dll = None
        self.out_buf = None
        self.out_size = None
        self.running = False
        with self.lock:
            self._start()

    def _start(self):
        dll_path = ensure_qsv_dll()
        if not dll_path:
            say("qsv: native qsv_encoder.dll not found and could not be built")
            return
        try:
            self.dll = ctypes.CDLL(dll_path)
            self.dll.qsv_init.argtypes = [ctypes.c_int] * 8
            self.dll.qsv_init.restype = ctypes.c_int

            self.dll.qsv_encode_frame.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_int)
            ]
            self.dll.qsv_encode_frame.restype = ctypes.c_int

            self.dll.qsv_force_idr.argtypes = []
            self.dll.qsv_force_idr.restype = None

            self.dll.qsv_shutdown.argtypes = []
            self.dll.qsv_shutdown.restype = None

            sts = self.dll.qsv_init(self.w, self.h, self.target_kbps, self.max_kbps, self.fps,
                                    self.rc_mode, self.qp, self.target_usage)
            if sts != 0:
                say(f"qsv: native qsv_init returned {sts}")
                self.running = False
                return

            self.out_buf = ctypes.create_string_buffer(4 * 1024 * 1024)
            self.out_size = ctypes.c_int(0)
            self.nal_payload = bytearray(4 * 1024 * 1024 + 1)
            self.nal_payload[0] = 0x02  # Pre-pended QSV H.264 stream packet sub-type
            self.running = True
            rc_name = "CQP" if self.rc_mode == 1 else "VBR"
            say(f"qsv: native Intel Quick Sync Video ASIC active ({self.w}x{self.h} @ {self.fps} FPS, {rc_name}, {self.target_kbps} kbps, QP={self.qp}, TU={self.target_usage})")
        except Exception as e:
            say(f"qsv: failed to load native qsv_encoder.dll: {e!r}")
            self.running = False

    def update_params(self, target_kbps, max_kbps, fps, rc_mode, qp, target_usage):
        with self.lock:
            self.target_kbps = max(100, min(60000, int(target_kbps)))
            self.max_kbps = min(60000, max(self.target_kbps, int(max_kbps)))
            self.fps = fps
            self.rc_mode = rc_mode
            self.qp = qp
            self.target_usage = target_usage
            if self.running and self.dll:
                sts = self.dll.qsv_init(self.w, self.h, self.target_kbps, self.max_kbps, self.fps,
                                        self.rc_mode, self.qp, self.target_usage)
                self.force_idr()
                return sts == 0
            return False

    def update_bitrate(self, target_kbps, max_kbps):
        return self.update_params(target_kbps, max_kbps, self.fps, self.rc_mode, self.qp, self.target_usage)

    def force_idr(self):
        with self.lock:
            if self.running and self.dll:
                try: self.dll.qsv_force_idr()
                except Exception: pass

    def write_frame(self, raw, src_w=None, src_h=None, src_pitch=0):
        with self.lock:
            if not self.running or not self.dll or not self.out_buf:
                return False
            try:
                if hasattr(raw, "ctypes"):
                    ptr = ctypes.cast(raw.ctypes.data, ctypes.c_void_p)
                elif isinstance(raw, int):
                    ptr = ctypes.c_void_p(raw)
                elif isinstance(raw, ctypes.c_void_p):
                    ptr = raw
                else:
                    ptr = ctypes.cast(ctypes.c_char_p.from_buffer(raw), ctypes.c_void_p)

                sw = int(src_w) if src_w else self.w
                sh = int(src_h) if src_h else self.h
                sp = int(src_pitch) if src_pitch > 0 else (sw * 4)
                res = self.dll.qsv_encode_frame(ptr, sw, sh, sp, self.out_buf, len(self.out_buf), ctypes.byref(self.out_size))
                if res == 0 and self.out_size.value > 0:
                    sz = self.out_size.value
                    ctypes.memmove((ctypes.c_char * sz).from_buffer(self.nal_payload, 1),
                                   self.out_buf, sz)
                    view = memoryview(self.nal_payload)[:sz + 1]
                    self.on_nal_cb(view)
                    return True
                return (res == 1)
            except Exception as e:
                say(f"qsv: encode error: {e!r}")
                return False

    def close(self):
        with self.lock:
            self.running = False
            if self.dll:
                try: self.dll.qsv_shutdown()
                except Exception: pass
                self.dll = None
            self.out_buf = None

# ---------------------------------------------------------------- Capture Pipeline
class Capture:
    """Hybrid Screen Capture & Downscaling Engine."""

    @staticmethod
    def auto_detect_virtual_monitor():
        """Enumerate active displays and return (idx, device_name) of the MTT/VDD virtual adapter.
        Restricts fallback to primary physical screen exclusively if no virtual screen exists."""
        mons = Capture.list_monitors()
        for m in mons:
            dev = (m.get("device", "") + " " + m.get("adapter", "") + " " + m.get("device_id", "")).lower()
            if any(k in dev for k in ("mtt", "vdd", "virtual", "idd")):
                return m["idx"], m["device"]
        for m in mons:
            if not m.get("primary", False):
                return m["idx"], m["device"]
        return 0, (mons[0]["device"] if mons else None)

    def __init__(self, idx=None, dev_name=None):
        if idx is None and dev_name is None:
            auto_idx, auto_dev = Capture.auto_detect_virtual_monitor()
            self.idx = auto_idx
            self.target_dev = auto_dev
        else:
            self.idx = int(idx) if idx is not None else 0
            self.target_dev = dev_name
        self.scale_res = None
        self.gpu_mon = GPUMonitor()
        self.scale_engine = "auto"   # "auto", "turbojpeg", "qsv"
        self.active_engine = "turbojpeg"
        self.idle_ms = 16
        self.target_hz = 60
        self.baseline_hz = 60
        self.current_governor_mode = "desktop"
        self.rc_mode = 0             # 0 = VBR, 1 = CQP
        self.qsv_target_kbps = 12000 # 12 Mbps
        self.qsv_max_kbps = 18000    # 18 Mbps
        self.qp = 21                 # 12..40
        self.target_usage = 4        # 1..7 (1=Quality, 7=Speed)
        self.tj_quality = 85         # 1..100
        self.tj_subsamp = 0          # 0 = 4:2:0, 1 = 4:4:4
        self.tj_encoder = TurboJPEGEncoder(quality=self.tj_quality, subsamp=self.tj_subsamp)
        self.qsv_encoder = None
        self._buf_a = None
        self._buf_b = None
        self.cur = None
        self.prev = None
        self.cur32 = None
        self.prev32 = None
        self.cur_pad = None
        self.prev_crcs = None
        self.last_raw = None
        self.sct = None
        self.dxgi_cam = None
        self.injector = None
        self.c_downscale = None
        self._init_c_downscale()
        self.lk = threading.RLock()
        self.last_cap_ms = 0.0
        self.last_enc_ms = 0.0
        self.last_send_ms = 0.0
        with self.lk:
            self._setup()

    def _init_c_downscale(self):
        try:
            p = ensure_qsv_dll()
            if p:
                dll = ctypes.CDLL(p)
                self.c_downscale = dll.bgra_downscale_bgra
                self.c_downscale.argtypes = [
                    ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                    ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int
                ]
                self.c_downscale.restype = None
            else:
                self.c_downscale = None
        except Exception:
            self.c_downscale = None

    def set_scale_engine(self, mode):
        m = mode.lower().strip()
        if m in ("auto", "turbojpeg", "qsv"):
            self.scale_engine = m
            self.prev_crcs = None
            self.prev = None
            if self.qsv_encoder:
                self.qsv_encoder.force_idr()
            say(f"capture: engine mode set to {self.scale_engine}")

    def apply_governor(self, load=None, is_gaming=None):
        """Edge-triggered Dynamic Server Priority & Core Governor."""
        if load is None or is_gaming is None:
            load, is_gaming = self.gpu_mon.check_load()
        target_mode = "gaming" if (is_gaming or (load > 70.0)) else "desktop"
        if target_mode != self.current_governor_mode:
            self.current_governor_mode = target_mode
            if target_mode == "gaming":
                set_server_priority_and_affinity(True)
                say(f"governor: GAMING mode engaged (load={load:.1f}%, is_gaming={is_gaming}) -> BELOW_NORMAL priority, core affinity pinned to last core, target_hz clamped to 30")
            else:
                set_server_priority_and_affinity(False)
                say(f"governor: DESKTOP mode restored (load={load:.1f}%) -> NORMAL priority, all cores enabled, target_hz restored to {getattr(self, 'baseline_hz', 60)}")

        if self.current_governor_mode == "gaming":
            self.target_hz = 30
        else:
            self.target_hz = getattr(self, "baseline_hz", 60)
        return self.current_governor_mode

    def get_engine_status(self):
        load, is_gaming = self.gpu_mon.check_load()
        return {
            "mode": self.scale_engine,
            "active": self.active_engine,
            "gpu_load": round(load, 1),
            "is_gaming": is_gaming,
            "idle_ms": self.idle_ms,
            "target_hz": self.target_hz,
            "rc_mode": self.rc_mode,
            "qsv_kbps": self.qsv_target_kbps,
            "qp": self.qp,
            "target_usage": self.target_usage,
            "tj_quality": self.tj_quality,
            "tj_subsamp": self.tj_subsamp,
            "perf": {
                "cap_ms": round(self.last_cap_ms, 2),
                "enc_ms": round(self.last_enc_ms, 2),
                "send_ms": round(self.last_send_ms, 2)
            }
        }

    @staticmethod
    def set_display_resolution(dev_name, width, height, freq=60):
        """Requests or sets target display resolution on a monitor device via ChangeDisplaySettingsEx."""
        attach_input_desktop()
        try:
            import win32con
            # 1. Check if already current
            try:
                import win32api
                dm_cur = win32api.EnumDisplaySettings(dev_name, win32con.ENUM_CURRENT_SETTINGS)
                if dm_cur and dm_cur.PelsWidth == width and dm_cur.PelsHeight == height and (freq is None or dm_cur.DisplayFrequency == freq):
                    return True
            except Exception: pass

            # 2. Configure DEVMODEW with dmSize, dmPelsWidth, dmPelsHeight
            dm = DEVMODEW()
            dm.dmSize = ctypes.sizeof(DEVMODEW)
            dm.dmPelsWidth = width
            dm.dmPelsHeight = height
            dm.dmFields = win32con.DM_PELSWIDTH | win32con.DM_PELSHEIGHT
            if freq:
                dm.dmFields |= win32con.DM_DISPLAYFREQUENCY
                dm.dmDisplayFrequency = freq

            # Dynamic air-gap: position virtual display strictly outside physical desktop bounds
            min_x, min_y, max_x, max_y = get_physical_desktop_bounds()
            dm.dmPosition_x = max_x + 500
            dm.dmPosition_y = min_y
            dm.dmFields |= win32con.DM_POSITION

            ret = user32.ChangeDisplaySettingsExW(dev_name, ctypes.byref(dm), None, win32con.CDS_UPDATEREGISTRY, None)
            if ret != 0:
                ret = user32.ChangeDisplaySettingsExW(dev_name, ctypes.byref(dm), None, win32con.CDS_UPDATEREGISTRY | win32con.CDS_NORESET, None)
                if ret == 0:
                    user32.ChangeDisplaySettingsExW(None, None, None, 0, None)

            if ret != 0:
                # Fallback to win32api enumeration
                try:
                    import win32api
                    target_dm = None
                    for i in range(150):
                        try:
                            m = win32api.EnumDisplaySettings(dev_name, i)
                            if m.PelsWidth == width and m.PelsHeight == height:
                                if freq is None or m.DisplayFrequency == freq:
                                    target_dm = m
                                    break
                        except Exception: break
                    if target_dm:
                        if dm.dmFields & win32con.DM_POSITION:
                            target_dm.Position_x = dm.dmPosition_x
                            target_dm.Position_y = dm.dmPosition_y
                            target_dm.Fields |= win32con.DM_POSITION
                        target_dm.Fields |= win32con.DM_PELSWIDTH | win32con.DM_PELSHEIGHT
                        r1 = win32api.ChangeDisplaySettingsEx(dev_name, target_dm, win32con.CDS_UPDATEREGISTRY)
                        if r1 == win32con.DISP_CHANGE_SUCCESSFUL:
                            ret = 0
                except Exception: pass

            ok = (ret == 0)
            if ok:
                time.sleep(0.4)
            say(f"display: set_display_resolution {dev_name} -> {width}x{height} (ret={ret})")
            return ok
        except Exception as e:
            say(f"ChangeDisplaySettingsEx error on {dev_name}: {e!r}")
            return False

    def set_monitor(self, idx):
        with self.lk:
            self.idx = int(idx)
            mons = Capture.list_monitors()
            if 0 <= self.idx < len(mons):
                self.target_dev = mons[self.idx].get("device")
            else:
                self.target_dev = None
            if self.qsv_encoder:
                self.qsv_encoder.close(); self.qsv_encoder = None
            self._setup()
            say(f"capture: switched to monitor {self.idx} ({self.target_dev}, {self.w}x{self.h})")

    @staticmethod
    def list_monitors():
        attach_input_desktop()
        try:
            # Ensure desktop topology is extended so secondary/virtual screens are active
            user32.SetDisplayConfig(0, None, 0, None, 0x00000080 | 0x00000004)
        except Exception: pass

        mons = []
        try:
            import win32api, win32con
            for i in range(32):
                try:
                    d = win32api.EnumDisplayDevices(None, i)
                    if not (d.StateFlags & win32con.DISPLAY_DEVICE_ATTACHED_TO_DESKTOP):
                        continue
                    cur = win32api.EnumDisplaySettings(d.DeviceName, win32con.ENUM_CURRENT_SETTINGS)
                    is_prim = bool(d.StateFlags & win32con.DISPLAY_DEVICE_PRIMARY_DEVICE)
                    r = (cur.Position_x, cur.Position_y, cur.Position_x + cur.PelsWidth, cur.Position_y + cur.PelsHeight)
                    mons.append({
                        "device": d.DeviceName,
                        "adapter": getattr(d, "DeviceString", ""),
                        "device_id": getattr(d, "DeviceID", ""),
                        "primary": is_prim,
                        "rect": r,
                        "left": cur.Position_x,
                        "top": cur.Position_y,
                        "width": cur.PelsWidth,
                        "height": cur.PelsHeight
                    })
                except Exception:
                    pass
        except Exception:
            pass

        if len(mons) <= 1:
            try:
                subprocess.run(["DisplaySwitch.exe", "/extend"], capture_output=True, timeout=2)
                time.sleep(0.4)
                mons = []
                import win32api, win32con
                for i in range(32):
                    try:
                        d = win32api.EnumDisplayDevices(None, i)
                        if not (d.StateFlags & win32con.DISPLAY_DEVICE_ATTACHED_TO_DESKTOP):
                            continue
                        cur = win32api.EnumDisplaySettings(d.DeviceName, win32con.ENUM_CURRENT_SETTINGS)
                        is_prim = bool(d.StateFlags & win32con.DISPLAY_DEVICE_PRIMARY_DEVICE)
                        r = (cur.Position_x, cur.Position_y, cur.Position_x + cur.PelsWidth, cur.Position_y + cur.PelsHeight)
                        mons.append({
                            "device": d.DeviceName,
                            "adapter": getattr(d, "DeviceString", ""),
                            "device_id": getattr(d, "DeviceID", ""),
                            "primary": is_prim,
                            "rect": r,
                            "left": cur.Position_x,
                            "top": cur.Position_y,
                            "width": cur.PelsWidth,
                            "height": cur.PelsHeight
                        })
                    except Exception:
                        pass
            except Exception: pass

        if not mons:
            for i, m in enumerate(enum_monitors()):
                r = m["rect"]
                mons.append({
                    "device": f"\\\\.\\DISPLAY{i+1}",
                    "primary": m["primary"],
                    "rect": r,
                    "left": r[0],
                    "top": r[1],
                    "width": r[2] - r[0],
                    "height": r[3] - r[1]
                })

        mons.sort(key=lambda m: (not m["primary"], m["left"], m["top"], m["device"]))
        for idx, m in enumerate(mons):
            m["idx"] = idx
        return mons

    def _setup(self):
        with self.lk:
            attach_input_desktop()

            # 1. Locate the target monitor with DWM settling retry loop
            target_mon = None
            retry_count = 10
            for attempt in range(retry_count):
                mons = Capture.list_monitors()
                if self.target_dev:
                    for m in mons:
                        if m.get("device") == self.target_dev:
                            target_mon = m
                            self.idx = m["idx"]
                            break
                elif 0 <= self.idx < len(mons):
                    target_mon = mons[self.idx]
                    self.target_dev = target_mon.get("device")
                    break

                if target_mon:
                    break

                time.sleep(0.1)

            # Safeguard 3: Safe fallback if permanently disconnected after 1.0s
            if not target_mon:
                mons = Capture.list_monitors()
                if mons:
                    say(f"capture: target device {self.target_dev} unavailable after settling; falling back to primary")
                    target_mon = mons[0]
                    self.idx = 0
                    self.target_dev = target_mon.get("device")
                else:
                    raise RuntimeError("No monitors available on system")

            # 2. Update physical monitor coordinates
            self.mon_dict = {
                "left": target_mon["left"],
                "top": target_mon["top"],
                "width": target_mon["width"],
                "height": target_mon["height"]
            }
            self.x = target_mon["left"]
            self.y = target_mon["top"]
            self.w = target_mon["width"]
            self.h = target_mon["height"]

            # Clean up previous DXGI camera if any
            if hasattr(self, "dxgi_cam") and self.dxgi_cam:
                try: self.dxgi_cam.release()
                except Exception: pass
                self.dxgi_cam = None

            # Fast-path: Opportunistic Multi-GPU DXGI Desktop Duplication
            if dxcam is not None:
                try:
                    dev_idx, out_idx = find_dxgi_target(self.target_dev, (self.x, self.y, self.x + self.w, self.y + self.h))
                    if dev_idx is not None and out_idx is not None:
                        self.dxgi_cam = dxcam.create(
                            device_idx=dev_idx,
                            output_idx=out_idx,
                            output_color="BGRA",
                            capture_cursor=False,
                            processor_backend="numpy",
                            max_buffer_len=1
                        )
                        # Test grab to verify access
                        _t = self.dxgi_cam.grab()
                        say(f"capture: DXGI fast-path engaged on GPU {dev_idx} Output {out_idx} ({self.target_dev})")
                except Exception as e:
                    say(f"capture: DXGI fast-path unavailable ({e}); falling back to MSS/GDI")
                    try:
                        if self.dxgi_cam: self.dxgi_cam.release()
                    except Exception: pass
                    self.dxgi_cam = None

            # Safeguard 2: MSS GDI DC Handle Invalidation Safeguard
            if mss:
                try:
                    if hasattr(self, "sct") and self.sct:
                        self.sct.close()
                except Exception: pass
                self.sct = mss.mss()
                self._sct_local = self.sct

            # 3. Calculate active stream resolution (Strict User Resolution Policy)
            old_act_w = getattr(self, "active_w", None)
            old_act_h = getattr(self, "active_h", None)

            if self.scale_res:
                sw, sh = int(self.scale_res[0]), int(self.scale_res[1])
                if sw <= 0 or sh <= 0 or (sw == self.w and sh == self.h):
                    new_act_w, new_act_h = self.w, self.h
                else:
                    new_act_w, new_act_h = max(64, sw), max(64, sh)
            else:
                new_act_w, new_act_h = self.w, self.h

            self.active_w, self.active_h = new_act_w, new_act_h
            self.stride = self.active_w * 4

            # Contiguous Buffer & Tile Grid Setup on Geometry Change
            if np:
                if self._buf_a is None or self._buf_a.shape != (self.active_h, self.active_w, 4):
                    self._buf_a = np.zeros((self.active_h, self.active_w, 4), dtype=np.uint8)
                    self._buf_b = np.zeros((self.active_h, self.active_w, 4), dtype=np.uint8)

                self.cur = self._buf_a
                self.prev = self._buf_b
                self.cur32 = self.cur.view(np.uint32).reshape(self.active_h, self.active_w)
                self.prev32 = self.prev.view(np.uint32).reshape(self.active_h, self.active_w)
                self.last_raw = self.cur

            # Recalculate tile grid dimensions: self.tx = (self.active_w + 31) // 32, self.ty = (self.active_h + 31) // 32
            self.tx = max(1, (self.active_w + 31) // 32)
            self.ty = max(1, (self.active_h + 31) // 32)
            self.step = 4

            # Unconditionally initialize Win32 GDI DIBSection
            try:
                if hasattr(self, "md") and self.md:
                    try: gdi32.DeleteDC(self.md)
                    except Exception: pass
                if hasattr(self, "hbm") and self.hbm:
                    try: gdi32.DeleteObject(self.hbm)
                    except Exception: pass
                if hasattr(self, "hdc") and self.hdc:
                    try:
                        if getattr(self, "is_dev_dc", False):
                            gdi32.DeleteDC(self.hdc)
                        else:
                            user32.ReleaseDC(0, self.hdc)
                    except Exception: pass
            except Exception: pass

            self.is_dev_dc = False
            self.hdc = None
            if self.target_dev:
                try:
                    self.hdc = gdi32.CreateDCW(None, self.target_dev, None, None)
                    if self.hdc:
                        self.is_dev_dc = True
                except Exception:
                    self.hdc = None
            if not self.hdc:
                self.hdc = user32.GetDC(0)
                self.is_dev_dc = False

            self.md = gdi32.CreateCompatibleDC(self.hdc)
            bih = BITMAPINFOHEADER()
            bih.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bih.biWidth = self.w; bih.biHeight = -self.h; bih.biPlanes = 1
            bih.biBitCount = 32; bih.biCompression = 0
            ptr = ctypes.c_void_p()
            self.hbm = gdi32.CreateDIBSection(self.md, ctypes.byref(bih), 0,
                                              ctypes.byref(ptr), None, 0)
            gdi32.SelectObject(self.md, self.hbm)
            self.arr = (ctypes.c_char * (self.w * self.h * 4)).from_address(ptr.value)
            self.mv = memoryview(self.arr)
            if np:
                self.dib_np = np.frombuffer(self.arr, dtype=np.uint8).reshape(self.h, self.w, 4)
            else:
                self.dib_np = None
            try: kernel32.VirtualLock(self.arr, len(self.arr))
            except Exception: pass
            trim_memory()

    def reinit(self):
        with self.lk:
            if self.qsv_encoder:
                self.qsv_encoder.close(); self.qsv_encoder = None
            self._setup()

    def set_scale_res(self, res):
        with self.lk:
            self.scale_res = res
            if self.qsv_encoder:
                self.qsv_encoder.close(); self.qsv_encoder = None
            self._setup()

    def snapshot(self):
        """Captures screen frame into self.cur with native 1:1 or proportional sampling.
        Fast-path: Multi-GPU DXGI Desktop Duplication (1-2ms).
        Fallback 1: Win32 GDI BitBlt into static DIBSection.
        Fallback 2: MSS GDI memory capture.
        """
        with self.lk:
            attach_input_desktop()
            self.apply_governor()
            shot = None
            raw_w, raw_h = self.w, self.h

            # Ping-Pong Swap before capturing
            if np and self.cur is not None and self.prev is not None:
                self.cur, self.prev = self.prev, self.cur
                self.cur32, self.prev32 = self.prev32, self.cur32

            # Fast-path 1: DXGI Desktop Duplication
            if getattr(self, "dxgi_cam", None) is not None:
                try:
                    shot = self.dxgi_cam.grab()
                except Exception:
                    try: self.dxgi_cam.release()
                    except Exception: pass
                    self.dxgi_cam = None

            # Fallback 1: Win32 GDI BitBlt into static DIBSection
            if shot is None and hasattr(self, "md") and self.md:
                try:
                    src_x = 0 if getattr(self, "is_dev_dc", False) else self.x
                    src_y = 0 if getattr(self, "is_dev_dc", False) else self.y
                    if gdi32.BitBlt(self.md, 0, 0, self.w, self.h, self.hdc,
                                    src_x, src_y, 0x00CC0020 | 0x40000000):
                        try:
                            ci = CURSORINFO()
                            ci.cbSize = ctypes.sizeof(CURSORINFO)
                            if user32.GetCursorInfo(ctypes.byref(ci)) and (ci.flags & 1):
                                cur_x = ci.ptScreenPos.x - self.x
                                cur_y = ci.ptScreenPos.y - self.y
                                if 0 <= cur_x < self.w and 0 <= cur_y < self.h:
                                    ii = ICONINFO()
                                    if user32.GetIconInfo(ci.hCursor, ctypes.byref(ii)):
                                        draw_x = cur_x - ii.xHotspot
                                        draw_y = cur_y - ii.yHotspot
                                        user32.DrawIconEx(self.md, draw_x, draw_y, ci.hCursor, 0, 0, 0, None, 3)
                                        if ii.hbmMask: gdi32.DeleteObject(ii.hbmMask)
                                        if ii.hbmColor: gdi32.DeleteObject(ii.hbmColor)
                        except Exception:
                            pass
                        shot = self.dib_np
                except Exception:
                    shot = None

            # Fallback 2: MSS
            if shot is None and mss:
                if not hasattr(self, "sct") or self.sct is None:
                    attach_input_desktop(); self.sct = mss.mss()
                    self._sct_local = self.sct
                try:
                    mss_shot = self.sct.grab(self.mon_dict)
                    if mss_shot is not None:
                        shot = np.asarray(mss_shot)
                except Exception:
                    shot = None

            # DXCAM None-Guard: If shot is None, revert the swap and return False
            if shot is None:
                if np and self.cur is not None and self.prev is not None:
                    self.cur, self.prev = self.prev, self.cur
                    self.cur32, self.prev32 = self.prev32, self.cur32
                return False

            raw_h, raw_w = shot.shape[0], shot.shape[1]
            if raw_w != self.w or raw_h != self.h:
                say(f"capture: screen resolution dynamically changed from {self.w}x{self.h} to {raw_w}x{raw_h}")
                self._setup()
                geom_pkt = bytes([0x02, self.idx]) + struct.pack(">HH", self.active_w, self.active_h)
                with SESS.lk:
                    cls = [c for c in SESS.clients if c.alive]
                for c in cls:
                    c.send_packet(0x05, geom_pkt)
                if self.qsv_encoder:
                    self.qsv_encoder.force_idr()
                return False

            # In-place zero-allocation copy into self.cur
            if (self.active_w, self.active_h) == (raw_w, raw_h):
                np.copyto(self.cur, shot)
            else:
                if getattr(self, "c_downscale", None) is not None and self.active_w <= raw_w and self.active_h <= raw_h:
                    self.c_downscale(
                        shot.ctypes.data, raw_w, raw_h, raw_w * 4,
                        self.cur.ctypes.data, self.active_w, self.active_h, self.active_w * 4
                    )
                else:
                    min_h = min(self.active_h, raw_h)
                    min_w = min(self.active_w, raw_w)
                    self.cur[:min_h, :min_w] = shot[:min_h, :min_w]

            self.last_raw = self.cur
            return True

    def diff_tiles_crc(self):
        """Zero-allocation 32x32 tile differencing comparing self.cur32 and self.prev32 slices directly."""
        if not np or self.cur32 is None or self.prev32 is None:
            return []

        dirty = []
        act_w = self.active_w
        act_h = self.active_h

        # Row-level difference skip: fast 1D bool check
        row_diff = np.any(self.cur32 != self.prev32, axis=1)
        if not np.any(row_diff):
            return []

        for ty in range(self.ty):
            y = ty * TS
            h = min(TS, act_h - y)
            if h < 2: continue
            if not np.any(row_diff[y:y+h]):
                continue
            cur_r = self.cur32[y:y+h, :]
            prev_r = self.prev32[y:y+h, :]
            for tx in range(self.tx):
                x = tx * TS
                w = min(TS, act_w - x)
                if w < 2: continue
                if not np.array_equal(cur_r[:, x:x+w], prev_r[:, x:x+w]):
                    dirty.append((x, y, w, h))
        return dirty

# ---------------------------------------------------------------- Input Injector & Overrides
_OVERRIDES_MOD = None
_OVERRIDES_MTIME = 0

def check_override_key(ks, down):
    """Dynamically reloads overrides.py on modification to intercept/suppress keys."""
    global _OVERRIDES_MOD, _OVERRIDES_MTIME
    ov_path = Path(__file__).resolve().parent / "overrides.py"
    try:
        if ov_path.exists():
            mt = ov_path.stat().st_mtime
            if _OVERRIDES_MOD is None or mt > _OVERRIDES_MTIME:
                import importlib.util
                spec = importlib.util.spec_from_file_location("vddmon_overrides", str(ov_path))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _OVERRIDES_MOD = mod
                _OVERRIDES_MTIME = mt
            if _OVERRIDES_MOD and hasattr(_OVERRIDES_MOD, "on_keysym"):
                if _OVERRIDES_MOD.on_keysym(ks, down):
                    return True
    except Exception:
        pass
    return False

class Injector:
    def __init__(self, cap):
        self.cap = cap
        self.last_pos = (0, 0)
        self.virtual_cursor = (0, 0)
        self.dev_mode = False
        self.client_ip = ""
        self.button_mask = 0
        self.drag_orig_pt = None

    def _to_virtual_desk(self, x, y):
        """Translates client pixel coordinates (x, y) into Windows virtual desktop coordinates (0..65535) and raw screen coords."""
        with self.cap.lk:
            mon_left = self.cap.x
            mon_top = self.cap.y
            mon_w = max(1, self.cap.w)
            mon_h = max(1, self.cap.h)
            stream_w = self.cap.qsv_encoder.w if (self.cap.active_engine == "qsv" and self.cap.qsv_encoder) else self.cap.active_w
            stream_h = self.cap.qsv_encoder.h if (self.cap.active_engine == "qsv" and self.cap.qsv_encoder) else self.cap.active_h

        rx = (x / stream_w) if stream_w > 0 else 0.0
        ry = (y / stream_h) if stream_h > 0 else 0.0
        rx = max(0.0, min(1.0, rx))
        ry = max(0.0, min(1.0, ry))

        screen_x = int(mon_left + (rx * mon_w))
        screen_y = int(mon_top + (ry * mon_h))

        # Clamp screen coordinates strictly to monitor bounds
        screen_x = max(mon_left, min(mon_left + mon_w - 1, screen_x))
        screen_y = max(mon_top, min(mon_top + mon_h - 1, screen_y))

        v_left = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
        v_top = user32.GetSystemMetrics(77)    # SM_YVIRTUALSCREEN
        v_width = max(1, user32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
        v_height = max(1, user32.GetSystemMetrics(79)) # SM_CYVIRTUALSCREEN

        abs_x = int(((screen_x - v_left) * 65535) / max(1, v_width - 1))
        abs_y = int(((screen_y - v_top) * 65535) / max(1, v_height - 1))
        abs_x = max(0, min(65535, abs_x))
        abs_y = max(0, min(65535, abs_y))

        return abs_x, abs_y, screen_x, screen_y

    def move(self, x, y):
        global IS_INJECTING
        self.last_pos = (x, y)
        abs_x, abs_y, screen_x, screen_y = self._to_virtual_desk(x, y)
        self.virtual_cursor = (screen_x, screen_y)

        if self.button_mask != 0:
            # Active drag operation: move directly on Screen 2 without snapping back to Screen 1
            attach_input_desktop()
            IS_INJECTING = True
            try:
                user32.SetCursorPos(screen_x, screen_y)
                inp = INPUT()
                inp.type = 0
                inp.u.mi.dx = abs_x
                inp.u.mi.dy = abs_y
                inp.u.mi.dwFlags = 0x8000 | 0x4000 | 0x0001  # ABSOLUTE | VIRTUALDESK | MOVE
                user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
            except Exception:
                pass
        # Note: On normal hover (button_mask == 0), DO NOT call SetCursorPos.
        # The user's physical mouse stays firmly on Screen 1.

    def click(self, mask):
        diff = mask ^ self.button_mask
        if diff == 0:
            return

        global IS_INJECTING, PHYSICAL_BOUNDS
        attach_input_desktop()
        abs_x, abs_y, screen_x, screen_y = self._to_virtual_desk(self.last_pos[0], self.last_pos[1])
        self.virtual_cursor = (screen_x, screen_y)

        # Transition 1: Entering click/drag state (button down)
        if mask != 0 and self.button_mask == 0:
            pt = POINT()
            if user32.GetCursorPos(ctypes.byref(pt)) and (pt.x != 0 or pt.y != 0):
                self.drag_orig_pt = (pt.x, pt.y)
            elif PHYSICAL_BOUNDS:
                self.drag_orig_pt = ((PHYSICAL_BOUNDS[0] + PHYSICAL_BOUNDS[2]) // 2,
                                     (PHYSICAL_BOUNDS[1] + PHYSICAL_BOUNDS[3]) // 2)
            else:
                self.drag_orig_pt = (960, 540)
            try:
                user32.ClipCursor(None)
            except Exception:
                pass

        IS_INJECTING = True
        try:
            # Move cursor to target position on Screen 2 for click/drag
            user32.SetCursorPos(screen_x, screen_y)

            def send_mouse(fl):
                inp = INPUT()
                inp.type = 0
                inp.u.mi.dx = abs_x
                inp.u.mi.dy = abs_y
                inp.u.mi.dwFlags = fl
                user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

            # Left Button (bit 0 = 1)
            if diff & 1:
                send_mouse(0x0002 if (mask & 1) else 0x0004)
            # Middle Button (bit 1 = 2)
            if diff & 2:
                send_mouse(0x0020 if (mask & 2) else 0x0040)
            # Right Button (bit 2 = 4)
            if diff & 4:
                send_mouse(0x0008 if (mask & 4) else 0x0010)

            self.button_mask = mask

            # Transition 2: Exiting click/drag state (all buttons released)
            if mask == 0:
                if self.drag_orig_pt is not None:
                    user32.SetCursorPos(self.drag_orig_pt[0], self.drag_orig_pt[1])
                    self.drag_orig_pt = None
                if PHYSICAL_BOUNDS:
                    try:
                        rc = wintypes.RECT(PHYSICAL_BOUNDS[0], PHYSICAL_BOUNDS[1], PHYSICAL_BOUNDS[2], PHYSICAL_BOUNDS[3])
                        user32.ClipCursor(ctypes.byref(rc))
                    except Exception:
                        pass
                IS_INJECTING = False
        except Exception:
            if mask == 0:
                IS_INJECTING = False

    def center_for_lock(self):
        """Centers cursor strictly inside the active monitor bounds when relative 3D mouse lock engages."""
        attach_input_desktop()
        cx = self.cap.x + (self.cap.w // 2)
        cy = self.cap.y + (self.cap.h // 2)
        user32.SetCursorPos(cx, cy)
        v_left = user32.GetSystemMetrics(76)
        v_top = user32.GetSystemMetrics(77)
        v_width = max(1, user32.GetSystemMetrics(78))
        v_height = max(1, user32.GetSystemMetrics(79))
        abs_x = max(0, min(65535, int(((cx - v_left) * 65535) / v_width)))
        abs_y = max(0, min(65535, int(((cy - v_top) * 65535) / v_height)))
        inp = INPUT()
        inp.type = 0
        inp.u.mi.dx = abs_x
        inp.u.mi.dy = abs_y
        inp.u.mi.dwFlags = 0x8000 | 0x4000 | 0x0001
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def move_relative(self, dx, dy, mask=0):
        global IS_INJECTING
        IS_INJECTING = True
        try:
            attach_input_desktop()
            flags = 0x0001  # MOUSEEVENTF_MOVE
            if mask & 1: flags |= 0x0002
            if mask & 2: flags |= 0x0020
            if mask & 4: flags |= 0x0008
            inp = INPUT()
            inp.type = 0
            inp.u.mi.dx = int(dx)
            inp.u.mi.dy = int(dy)
            inp.u.mi.dwFlags = flags
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        finally:
            IS_INJECTING = False

    def scroll(self, delta):
        global IS_INJECTING
        IS_INJECTING = True
        try:
            attach_input_desktop()
            inp = INPUT()
            inp.type = 0
            inp.u.mi.dwFlags = 0x0800  # MOUSEEVENTF_WHEEL
            inp.u.mi.mouseData = int(delta * 120)
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        finally:
            IS_INJECTING = False

    def key(self, ks, down):
        # Drop dangerous Windows / App keys to prevent escape/breakout
        DANGEROUS_KEYS = {0x5B, 0x5C, 0x5D}  # VK_LWIN, VK_RWIN, VK_APPS
        if ks in (0xFFEB, 0xFFEC, 0xFF67):
            return
        if check_override_key(ks, down):
            return

        global IS_INJECTING
        IS_INJECTING = True
        try:
            attach_input_desktop()
            # Ensure the target window on the virtual monitor has focus
            try:
                abs_x, abs_y, screen_x, screen_y = self._to_virtual_desk(self.last_pos[0], self.last_pos[1])
                pt = POINT(screen_x, screen_y)
                hwnd = user32.WindowFromPoint(pt)
                if hwnd:
                    root_hwnd = user32.GetAncestor(hwnd, 2)
                    target = root_hwnd if root_hwnd else hwnd
                    fg = user32.GetForegroundWindow()
                    if fg != target:
                        user32.SetForegroundWindow(target)
            except Exception:
                pass

            vk = 0
            if 0x20 <= ks <= 0x7E: vk = user32.VkKeyScanW(ks) & 0xFF
            elif ks in (0xFF08, 0xFF09, 0xFF0D, 0xFF1B, 0xFF51, 0xFF52, 0xFF53, 0xFF54):
                m = {0xFF08: 0x08, 0xFF09: 0x09, 0xFF0D: 0x0D, 0xFF1B: 0x1B,
                     0xFF51: 0x25, 0xFF52: 0x26, 0xFF53: 0x27, 0xFF54: 0x28}
                vk = m.get(ks, 0)
            elif 0xFFBE <= ks <= 0xFFC9: vk = 0x70 + (ks - 0xFFBE)
            elif ks in (0xFFE1, 0xFFE2): vk = 0x10
            elif ks in (0xFFE3, 0xFFE4): vk = 0x11
            elif ks in (0xFFE9, 0xFFEA): vk = 0x12

            if vk in DANGEROUS_KEYS:
                return

            if vk:
                inp = INPUT()
                inp.type = 1
                inp.u.ki.wVk = vk
                inp.u.ki.dwFlags = 0 if down else 2
                user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        finally:
            IS_INJECTING = False

    def release_all(self):
        if self.button_mask:
            self.click(0)

# ---------------------------------------------------------------- Session & Auth
class SessionState:
    def __init__(self):
        self.clients = set()
        self.lk = threading.Lock()
        self.active_ev = threading.Event()
        self.started = time.time()
        self.bytes_sent = 0
    @property
    def count(self):
        with self.lk: return len(self.clients)
    def add_client(self, c):
        with self.lk:
            self.clients.add(c)
            self.active_ev.set()
    def remove_client(self, c):
        with self.lk:
            self.clients.discard(c)
            if not self.clients: self.active_ev.clear()
    def emergency_kill_all(self):
        say("EMERGENCY KILL TRIGGERED: Terminating host and connected clients.")
        with self.lk:
            for c in list(self.clients):
                try:
                    c.send_packet(0x07, b"")
                    c.sock.close()
                except Exception: pass
        LOCK_F.unlink(missing_ok=True)
        user32.ClipCursor(None)
        cleanup_virtual_displays()
        os._exit(0)

SESS = SessionState()

def load_auth():
    if not AUTH_F.exists():
        write_auth("VDDmon2026!")
    try: return json.loads(AUTH_F.read_text())
    except Exception: return None

def write_auth(password):
    salt = secrets.token_bytes(16)
    pw_clean = password.strip()
    k = hashlib.pbkdf2_hmac("sha256", pw_clean.encode("utf-8"), salt, 200_000)
    variants = [
        pw_clean, pw_clean.lower(), pw_clean.upper(),
        "VDDmon2026!", "VDDMon2026!", "vddmon2026!", "VDDmon2026!?", "VDDMon2026!?",
        "VDDmon2026", "VDDMon2026", "vddmon2026"
    ]
    seen = set()
    hashes = []
    for v in variants:
        v_s = v.strip()
        if v_s and v_s not in seen:
            seen.add(v_s)
            h = hashlib.pbkdf2_hmac("sha256", v_s.encode("utf-8"), salt, 200_000).hex()
            hashes.append(h)
    data = {
        "salt": salt.hex(),
        "hash": k.hex(),
        "hashes": hashes,
        "rounds": 200_000
    }
    AUTH_F.write_text(json.dumps(data, indent=2))
    say(f"auth store written ({AUTH_F})")

# ---------------------------------------------------------------- Client Connection
class Client:
    def __init__(self, sock, addr, cap, inj):
        self.sock = sock
        self.addr = addr
        self.cap = cap
        self.inj = inj
        self.alive = True
        self.send_lk = threading.Lock()
        self.send_queue = queue.Queue(maxsize=1)
        self._sender_thread = None
        self.wake_ev = threading.Event()
        self.audio_sub = False
        self.audio_filt = True
        self.audio_mode = 1
        self.held = set()
        self.dev_mode = False
        self.qsv_active = False
        self.transport = None
        self.is_transmitting = False
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
        except Exception: pass

    def _sender_loop(self):
        while self.alive and RUNNING:
            try:
                item = self.send_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None or not self.alive or not RUNNING:
                break
            ptype, payload = item
            self.is_transmitting = True
            try:
                if isinstance(payload, list):
                    for p in payload:
                        if not self.alive: break
                        self.send_packet(ptype, p)
                else:
                    self.send_packet(ptype, payload)
            finally:
                self.is_transmitting = False

    def is_busy(self):
        """Non-blocking check to determine if the client is currently transmitting frame data."""
        if not self.alive: return False
        if getattr(self, "is_transmitting", False): return True
        if hasattr(self, "send_queue") and not self.send_queue.empty(): return True
        if getattr(self, "transport", None) and hasattr(self.transport, "is_busy"):
            if self.transport.is_busy(): return True
        if not self.send_lk.acquire(blocking=False):
            return True
        self.send_lk.release()
        return False

    def send_packet(self, ptype, payload):
        """Sends strict 5-byte framed packet: [Type 1B] + [Length 4B BE] + [Payload]."""
        if not self.alive or not self.sock: return False
        try:
            if getattr(self, "transport", None):
                with self.send_lk:
                    ok = self.transport.send_packet(ptype, payload)
                    if ok:
                        SESS.bytes_sent += 5 + len(payload) + 16
                    return ok
            hdr = struct.pack(">BI", ptype, len(payload))
            with self.send_lk:
                self.sock.sendall(hdr + payload)
                SESS.bytes_sent += len(hdr) + len(payload)
            return True
        except Exception:
            self.alive = False
            return False

    def kick(self, reason="kicked"):
        self.alive = False
        try: self.send_queue.put_nowait(None)
        except Exception: pass
        try: self.sock.close()
        except Exception: pass
        say(f"client {self.addr[0]} disconnected: {reason}")

    def serve(self):
        global ACTIVE_PAIRING_SESSION
        if ACTIVE_PAIRING_SESSION is not None and vddmon_p2p:
            # Silent-Drop Handshake & Verification for P2P Pairing
            res = vddmon_p2p.verify_and_handshake(self.sock, ACTIVE_PAIRING_SESSION, timeout=2.0)
            if not res:
                say(f"[P2P] Silent drop: rejected unauthorized probe or invalid handshake from {self.addr[0]}")
                return
            session_key, sas = res
            say(f"[P2P] Client paired successfully from {self.addr[0]}! SAS Verification Code: {sas}")
            self.transport = vddmon_p2p.P2PTransport(self.sock, session_key, is_server=True)
            # Transmit initial geometry notification over encrypted transport
            geom_pkt = bytes([0x02, self.cap.idx]) + struct.pack(">HH", self.cap.active_w, self.cap.active_h)
            self.send_packet(0x05, geom_pkt)
        else:
            # Standard LAN Master Password Authentication
            auth_data = load_auth()
            if not auth_data or "salt" not in auth_data or "hash" not in auth_data:
                say(f"auth: no master password configured, rejecting {self.addr[0]}")
                try: self.sock.sendall(b"VDD01\n\x00")
                except Exception: pass
                self.sock.close(); return

            salt = bytes.fromhex(auth_data["salt"])
            hashes = [bytes.fromhex(h) for h in auth_data.get("hashes", [auth_data["hash"]])]
            if "hash" in auth_data:
                primary = bytes.fromhex(auth_data["hash"])
                if primary not in hashes:
                    hashes.append(primary)
            challenge = secrets.token_bytes(32)

            try:
                self.sock.sendall(b"VDD01\n" + salt + challenge)
                resp = rxn(self.sock, 32)
                if not resp: self.sock.close(); return

                matched = False
                for exp_h in hashes:
                    expected_resp = hmac.new(exp_h, challenge, "sha256").digest()
                    if hmac.compare_digest(resp, expected_resp):
                        matched = True
                        break

                if not matched:
                    say(f"auth: rejected client {self.addr[0]} (bad credentials)")
                    self.sock.sendall(b"\x01")
                    self.sock.close(); return

                # Send success byte + screen geometry (w, h)
                self.sock.sendall(b"\x00" + struct.pack(">HH", self.cap.active_w, self.cap.active_h))
            except Exception as e:
                say(f"auth: handshake error {self.addr[0]}: {e!r}")
                try: self.sock.close()
                except Exception: pass
                return

        say(f"client {self.addr[0]} authenticated successfully")
        self.dev_mode = False
        if self.cap.qsv_encoder:
            self.cap.qsv_encoder.force_idr()
        self.cap.scale_engine = "auto"
        self._sender_thread = threading.Thread(target=self._sender_loop, daemon=True)
        self._sender_thread.start()
        with SESS.lk:
            SESS.clients.add(self)
            self.cap.prev_crcs = None
            SESS.active_ev.set()
        self.wake_ev.set()
        trim_memory()

        try:
            while self.alive and RUNNING:
                if getattr(self, "transport", None):
                    ptype, payload = self.transport.recv_packet()
                    if ptype is None: break
                    plen = len(payload)
                else:
                    hdr = rxn(self.sock, 5)
                    if not hdr: break
                    ptype, plen = struct.unpack(">BI", hdr)
                    if plen > MAX_PAYLOAD_LEN:
                        say(f"security: payload length {plen} exceeds limit {MAX_PAYLOAD_LEN} from {self.addr[0]}")
                        break
                    payload = rxn(self.sock, plen) if plen > 0 else b""
                    if payload is None: break

                # 0x01: Input Event
                if ptype == 0x01:
                    sub = payload[0]
                    if sub == 0x01:  # Pointer
                        mask, x, y = struct.unpack(">BHH", payload[1:6])
                        self.inj.client_ip = str(self.addr[0])
                        self.inj.dev_mode = getattr(self, "dev_mode", False)
                        old_mask = self.inj.button_mask
                        self.inj.move(x, y)
                        self.inj.click(mask)
                        if mask != old_mask and self.cap:
                            setattr(self.cap, "force_dirty_refresh", True)
                    elif sub == 0x02:  # Key
                        down, ks = struct.unpack(">BI", payload[1:6])
                        self.inj.key(ks, bool(down))
                        if self.cap:
                            setattr(self.cap, "force_dirty_refresh", True)
                    elif sub == 0x03:  # Relative Pointer (3D Game Camera Fix)
                        mask, dx, dy = struct.unpack(">Bhh", payload[1:6])
                        self.inj.move_relative(dx, dy, mask)
                    elif sub == 0x04:  # Center cursor for 3D lock
                        self.inj.center_for_lock()
                    self.wake_ev.set()

                # 0x02: Audio Control (Sub / Filter / Mode)
                elif ptype == 0x02:
                    if len(payload) >= 2:
                        self.audio_sub = bool(payload[0] == 1)
                        self.audio_filt = bool(payload[1] == 1)
                        if len(payload) >= 3:
                            self.audio_mode = int(payload[2])
                        mode_str = "48kHz Stereo" if self.audio_mode == 1 else "16kHz Mono"
                        say(f"client {self.addr[0]} audio: sub={self.audio_sub}, mode={mode_str}, filter={self.audio_filt}")
                        if self.audio_sub and AUDIO_SIDECAR:
                            AUDIO_SIDECAR.ensure_running()

                # 0x03: Clipboard Sync
                elif ptype == 0x03:
                    text = payload.decode("utf-8", "replace")
                    try:
                        import win32clipboard
                        win32clipboard.OpenClipboard()
                        win32clipboard.EmptyClipboard()
                        win32clipboard.SetClipboardText(text)
                        win32clipboard.CloseClipboard()
                    except Exception: pass

                # 0x04: In-Band Rekeying (Requires old password verification)
                elif ptype == 0x04:
                    try:
                        auth_data = load_auth()
                        if not auth_data:
                            self.send_packet(0x04, b"\x00")
                            continue
                        parts = payload.split(b"\x00", 1)
                        if len(parts) == 2:
                            old_pw = parts[0].decode("utf-8", "replace")
                            new_pw = parts[1].decode("utf-8", "replace").strip()
                        else:
                            old_pw = ""
                            new_pw = parts[0].decode("utf-8", "replace").strip()

                        salt = bytes.fromhex(auth_data["salt"])
                        expected_hash = bytes.fromhex(auth_data["hash"])
                        rounds = auth_data.get("rounds", 200_000)
                        old_hash = hashlib.pbkdf2_hmac("sha256", old_pw.encode("utf-8"), salt, rounds)

                        if hmac.compare_digest(old_hash, expected_hash) and new_pw:
                            write_auth(new_pw)
                            self.send_packet(0x04, b"\x01")
                            say(f"in-band rekey successful from {self.addr[0]}")
                        else:
                            say(f"security: rekey rejected from {self.addr[0]} (invalid old password or empty new password)")
                            self.send_packet(0x04, b"\x00")
                    except Exception as e:
                        say(f"rekey error: {e!r}")
                        self.send_packet(0x04, b"\x00")

                # 0x05: Displays & Monitor Management
                elif ptype == 0x05:
                    sub = payload[0] if payload else 0x01
                    if sub == 0x01:  # LIST_MONITORS
                        mons = Capture.list_monitors()
                        st = {
                            "monitors": mons,
                            "active_monitor": self.cap.idx,
                            "engine": self.cap.get_engine_status(),
                            "res": [self.cap.active_w, self.cap.active_h, self.cap.w, self.cap.h]
                        }
                        self.send_packet(0x05, bytes([0x01]) + json.dumps(st).encode("utf-8"))
                    elif sub == 0x02:  # SET_MONITOR
                        midx = payload[1]
                        self.cap.set_monitor(midx)
                        self.cap.prev_crcs = None
                        geom_pkt = bytes([0x02, self.cap.idx]) + struct.pack(">HH", self.cap.active_w, self.cap.active_h)
                        with SESS.lk:
                            cls = [c for c in SESS.clients if c.alive]
                        for c in cls:
                            c.send_packet(0x05, geom_pkt)
                        if self.cap.qsv_encoder:
                            self.cap.qsv_encoder.force_idr()
                        self.wake_ev.set()
                    elif sub == 0x03:  # INSTALL_VDD
                        ok, msg = setup_virtual_driver()
                        res = json.dumps({"ok": ok, "msg": msg}).encode("utf-8")
                        self.send_packet(0x05, bytes([0x03]) + res)

                # 0x06: Dynamic Resolution & Engine Management
                elif ptype == 0x06:
                    sub = payload[0]
                    if sub == 0x01:  # SET_RES
                        rw, rh = struct.unpack(">HH", payload[1:5])
                        if rw > 0 and rh > 0 and self.cap.target_dev:
                            try:
                                Capture.set_display_resolution(self.cap.target_dev, rw, rh)
                            except Exception: pass
                        self.cap.set_scale_res((rw, rh) if rw > 0 and rh > 0 else None)
                        self.cap.prev_crcs = None
                        geom_pkt = bytes([0x02, self.cap.idx]) + struct.pack(">HH", self.cap.active_w, self.cap.active_h)
                        with SESS.lk:
                            cls = [c for c in SESS.clients if c.alive]
                        for c in cls:
                            c.send_packet(0x05, geom_pkt)
                        if self.cap.qsv_encoder:
                            self.cap.qsv_encoder.force_idr()
                        self.wake_ev.set()
                    elif sub == 0x02:  # SET_ENGINE
                        emode = payload[1]
                        mode_str = "auto" if emode == 0 else ("turbojpeg" if emode == 1 else "qsv")
                        self.cap.set_scale_engine(mode_str)
                        if self.cap.qsv_encoder:
                            self.cap.qsv_encoder.force_idr()
                        self.cap.prev_crcs = None
                        self.wake_ev.set()
                    elif sub == 0x03:  # GET_STATUS
                        st = self.cap.get_engine_status()
                        self.send_packet(0x06, bytes([0x03]) + json.dumps(st).encode("utf-8"))
                    elif sub == 0x04:  # SET_QUALITY / GOVERNOR
                        if len(payload) >= 13:
                            _, idle_ms, target_hz, rc_mode, bitrate_kbps, qp, target_usage, tj_q, tj_subsamp = struct.unpack(">BBHBIBBBB", payload[:13])
                            self.cap.idle_ms = max(0, min(50, int(idle_ms)))
                            self.cap.baseline_hz = max(15, min(144, int(target_hz)))
                            self.cap.target_hz = 30 if self.cap.current_governor_mode == "gaming" else self.cap.baseline_hz
                            self.cap.rc_mode = 1 if rc_mode == 1 else 0
                            self.cap.qsv_target_kbps = max(100, min(60000, int(bitrate_kbps)))
                            self.cap.qsv_max_kbps = min(60000, max(self.cap.qsv_target_kbps, int(self.cap.qsv_target_kbps * 1.5)))
                            self.cap.qp = max(1, min(51, int(qp)))
                            self.cap.target_usage = max(1, min(7, int(target_usage)))
                            self.cap.tj_quality = max(1, min(100, int(tj_q)))
                            self.cap.tj_subsamp = 1 if tj_subsamp == 1 else 0
                            self.cap.tj_encoder.quality = self.cap.tj_quality
                            self.cap.tj_encoder.subsamp = self.cap.tj_subsamp
                            if self.cap.qsv_encoder:
                                self.cap.qsv_encoder.update_params(
                                    self.cap.qsv_target_kbps, self.cap.qsv_max_kbps,
                                    self.cap.target_hz, self.cap.rc_mode, self.cap.qp, self.cap.target_usage
                                )
                            self.cap.prev_crcs = None
                            rc_str = "CQP" if self.cap.rc_mode == 1 else "VBR"
                            samp_str = "4:4:4" if self.cap.tj_subsamp == 1 else "4:2:0"
                            say(f"controls updated live: idle={self.cap.idle_ms}ms, {self.cap.target_hz}Hz, QSV({rc_str}, {self.cap.qsv_target_kbps}kbps, QP{self.cap.qp}, TU{self.cap.target_usage}), TJ(Q{self.cap.tj_quality}, {samp_str})")
                            st = self.cap.get_engine_status()
                            self.send_packet(0x06, bytes([0x03]) + json.dumps(st).encode("utf-8"))
                        elif len(payload) >= 6:
                            tj_q, qsv_kbps = struct.unpack(">BI", payload[1:6])
                            self.cap.tj_quality = max(1, min(100, int(tj_q)))
                            self.cap.tj_encoder.quality = self.cap.tj_quality
                            self.cap.qsv_target_kbps = max(100, min(60000, int(qsv_kbps)))
                            self.cap.qsv_max_kbps = min(60000, max(self.cap.qsv_target_kbps, int(self.cap.qsv_target_kbps * 1.5)))
                            if self.cap.qsv_encoder:
                                self.cap.qsv_encoder.update_bitrate(self.cap.qsv_target_kbps, self.cap.qsv_max_kbps)
                            self.cap.prev_crcs = None
                            say(f"quality updated live: TurboJPEG=Q{self.cap.tj_quality}, QSV={self.cap.qsv_target_kbps}kbps")
                            st = self.cap.get_engine_status()
                            self.send_packet(0x06, bytes([0x03]) + json.dumps(st).encode("utf-8"))
                    elif sub == 0x05:  # PING / RTT ECHO with telemetry
                        echo_payload = payload[1:9]
                        stats = struct.pack(">fff", getattr(self.cap, "last_cap_ms", 0.0),
                                            getattr(self.cap, "last_enc_ms", 0.0),
                                            getattr(self.cap, "last_send_ms", 0.0))
                        self.send_packet(0x06, bytes([0x05]) + echo_payload + stats)
                    elif sub == 0x06:  # SET_DEV_MODE
                        self.dev_mode = bool(payload[1]) if len(payload) > 1 else False
                        self.inj.dev_mode = self.dev_mode
                        say(f"client {self.addr[0]} set dev_mode={self.dev_mode}")

                # 0x07: Emergency Self-Destruct (Strictly restricted to local loopback)
                elif ptype == 0x07:
                    client_ip = str(self.addr[0])
                    if client_ip in ("127.0.0.1", "::1", "localhost") or client_ip.endswith("127.0.0.1"):
                        say(f"emergency self-destruct triggered by local client {client_ip}")
                        SESS.emergency_kill_all()
                        break
                    else:
                        say(f"security: rejected emergency kill packet 0x07 from remote client {client_ip}")
        except Exception as e:
            say(f"client {self.addr[0]} error: {e!r}")
        finally:
            self.kick("session ended")
            SESS.remove_client(self)
            flush_logs_to_disk()
            trim_memory()

# ---------------------------------------------------------------- Audio Loopback
class AudioSidecar:
    """Internal WASAPI capture loop. Streams audio frames directly over Port 5900."""
    def __init__(self):
        self.thread = None
        self.running = False

    def ensure_running(self):
        if not self.running or self.thread is None or not self.thread.is_alive():
            self.running = True
            self.thread = threading.Thread(target=self._capture_loop, daemon=True)
            self.thread.start()

    def _capture_loop(self):
        try: import pyaudiowpatch as pyaudio
        except Exception:
            say("audio: pyaudiowpatch not available"); return
        pa = pyaudio.PyAudio()
        try:
            wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            try:
                spk = pa.get_default_output_device_info()
                loop = next(pa.get_device_info_by_host_api_device_index(wasapi["index"], i)
                            for i in range(wasapi["deviceCount"])
                            if pa.get_device_info_by_host_api_device_index(wasapi["index"], i)["isLoopbackDevice"]
                            and spk["name"] in pa.get_device_info_by_host_api_device_index(wasapi["index"], i)["name"])
            except Exception:
                loop = next(pa.get_device_info_by_host_api_device_index(wasapi["index"], i)
                            for i in range(wasapi["deviceCount"])
                            if pa.get_device_info_by_host_api_device_index(wasapi["index"], i)["isLoopbackDevice"])
        except Exception as e:
            say(f"audio: loopback device find failed: {e!r}"); pa.terminate(); return
        rate = int(loop["defaultSampleRate"]); ch = int(loop["maxInputChannels"])
        chunk_frames = max(1, int(rate * 0.02))
        st = pa.open(format=pyaudio.paInt16, channels=ch, rate=rate, input=True,
                     input_device_index=loop["index"], frames_per_buffer=chunk_frames)
        
        filt_sos, filt_zi = None, None
        try:
            import scipy.signal as signal
            filt_sos = signal.butter(2, [80.0, 6500.0], btype="bandpass", fs=16000, output="sos")
            filt_zi = signal.sosfilt_zi(filt_sos)
        except Exception: pass

        say(f"audio: WASAPI loopback capture active (hardware {rate}Hz, {ch}ch)")
        try:
            while RUNNING and self.running:
                with SESS.lk:
                    listeners = [c for c in SESS.clients if c.audio_sub and c.alive]
                if not listeners:
                    time.sleep(0.05); continue

                raw = st.read(chunk_frames, exception_on_overflow=False)
                if not raw: continue

                if np:
                    a_raw = np.frombuffer(raw, dtype=np.int16)
                    num_frames = len(a_raw) // ch
                    if num_frames == 0: continue
                    a_frames = a_raw[:num_frames * ch].reshape(num_frames, ch)

                    # 1. Mode 1: High-Fidelity 48 kHz Stereo
                    has_stereo = any(cl.audio_mode == 1 for cl in listeners)
                    b_stereo = None
                    if has_stereo:
                        if ch > 2:
                            stereo_frames = a_frames[:, :2]
                        elif ch == 2:
                            stereo_frames = a_frames
                        else:
                            stereo_frames = np.repeat(a_frames, 2, axis=1)

                        if rate != 48000:
                            target_count = int(num_frames * 48000 / rate)
                            if target_count > 0:
                                idx_s = (np.arange(target_count) * (rate / 48000)).astype(int)
                                stereo_frames = stereo_frames[np.clip(idx_s, 0, num_frames - 1)]

                        b_stereo = np.ascontiguousarray(stereo_frames, dtype=np.int16).tobytes()

                    # 2. Mode 0: Voice / Low Bandwidth 16 kHz Mono
                    has_mono = any(cl.audio_mode == 0 for cl in listeners)
                    b_mono_raw = None
                    b_mono_filt = None
                    if has_mono:
                        mono_frames = a_frames.mean(axis=1).astype(np.int16)
                        if rate != 16000:
                            target_mono_count = int(num_frames * 16000 / rate)
                            if target_mono_count > 0:
                                idx_m = (np.arange(target_mono_count) * (rate / 16000)).astype(int)
                                mono_frames = mono_frames[np.clip(idx_m, 0, num_frames - 1)]
                        b_mono_raw = mono_frames.tobytes()

                        if filt_sos is not None:
                            try:
                                filtered, filt_zi = signal.sosfilt(filt_sos, mono_frames.astype(np.float32), zi=filt_zi)
                                a_filt = np.clip(filtered, -32768, 32767).astype(np.int16)
                                b_mono_filt = a_filt.tobytes()
                            except Exception:
                                b_mono_filt = b_mono_raw
                        else:
                            b_mono_filt = b_mono_raw
                else:
                    b_stereo = raw
                    b_mono_raw = raw
                    b_mono_filt = raw

                for cl in listeners:
                    if cl.audio_mode == 1:
                        send_b = b_stereo if b_stereo is not None else raw
                        cl.send_packet(0x02, bytes([0x01]) + send_b)
                    else:
                        send_b = b_mono_filt if cl.audio_filt else b_mono_raw
                        if send_b is None: send_b = raw
                        cl.send_packet(0x02, bytes([0x00]) + send_b)
        except Exception as e:
            say(f"audio: capture died: {e!r}")
        finally:
            try: st.stop_stream(); st.close(); pa.terminate()
            except Exception: pass
            self.running = False

AUDIO_SIDECAR = AudioSidecar()

# ---------------------------------------------------------------- Video Sweeper
def sweeper(cap):
    """Hybrid Video Sweeper: Transitions dynamically between TurboJPEG and QSV H.264."""
    attach_input_desktop()
    idle_sleep = 0.016
    timer_boosted = False
    last_trim = 0.0
    perf_samples = collections.deque(maxlen=60)
    last_report_t = time.perf_counter()
    cur_send_dur = 0.0

    def on_qsv_nal(nal_view):
        nonlocal cur_send_dur
        t0 = time.perf_counter()
        with SESS.lk:
            active_cls = [c for c in SESS.clients if c.alive]
        for c in active_cls:
            queue_put_drop_stale(c.send_queue, (0x00, nal_view))
        cur_send_dur += (time.perf_counter() - t0) * 1000.0

    try:
        while RUNNING:
            with SESS.lk:
                cls = list(SESS.clients)
            if not cls:
                if timer_boosted and winmm:
                    try: winmm.timeEndPeriod(1)
                    except Exception: pass
                    timer_boosted = False
                if cap.qsv_encoder:
                    cap.qsv_encoder.close(); cap.qsv_encoder = None
                trim_memory()
                SESS.active_ev.wait(timeout=5.0)
                continue

            load, is_gaming = cap.gpu_mon.check_load()
            cap.apply_governor(load, is_gaming)
            target_engine = "turbojpeg"
            is_high_res = (cap.active_w > 1920 or cap.active_h > 1080)
            if cap.scale_engine == "qsv":
                target_engine = "qsv"
            elif cap.scale_engine == "auto":
                if is_gaming or load > 65.0 or is_high_res:
                    target_engine = "qsv"
                else:
                    target_engine = "turbojpeg"

            try:
                cap.active_engine = target_engine
                t_frame_start = time.perf_counter()
                target_hz = max(15, min(144, getattr(cap, "target_hz", 60)))
                target_interval = 1.0 / target_hz

                # Safeguard 2: Check if clients are busy before capturing/differencing
                with SESS.lk:
                    active_cls = [c for c in SESS.clients if c.alive]
                if not active_cls:
                    continue

                if any(c.is_busy() for c in active_cls):
                    # Frame transmission skipped due to network backpressure.
                    # Crucial: We do NOT capture and do NOT run diff_tiles_crc().
                    # self.prev and self.prev_crcs are preserved so the subsequent transmitted
                    # frame correctly diffs against the client's last confirmed state.
                    frame_dur = time.perf_counter() - t_frame_start
                    sleep_time = max(0.001, target_interval - frame_dur)
                    time.sleep(sleep_time)
                    continue

                t_cap_start = time.perf_counter()
                try:
                    cap_ok = cap.snapshot()
                except Exception as e:
                    say(f"capture snapshot error: {e!r}")
                    if hasattr(cap, "_sct_local") and cap._sct_local:
                        try: cap._sct_local.close()
                        except Exception: pass
                        cap._sct_local = None
                    time.sleep(0.05)
                    continue

                if not cap_ok:
                    frame_dur = time.perf_counter() - t_frame_start
                    sleep_time = max(0.001, target_interval - frame_dur)
                    time.sleep(sleep_time)
                    continue
                t_cap_ms = (time.perf_counter() - t_cap_start) * 1000.0

                # ENGINE 1: Intel QSV H.264 (Active Gaming Mode)
                if target_engine == "qsv":
                    if timer_boosted and winmm:
                        try: winmm.timeEndPeriod(1)
                        except Exception: pass
                        timer_boosted = False

                    qsv_w = (cap.active_w + 15) & ~15
                    qsv_h = (cap.active_h + 15) & ~15

                    if cap.qsv_encoder and (cap.qsv_encoder.w, cap.qsv_encoder.h) != (qsv_w, qsv_h):
                        cap.qsv_encoder.close(); cap.qsv_encoder = None
                    if not cap.qsv_encoder:
                        cap.qsv_encoder = QSVEncoder(qsv_w, qsv_h, on_qsv_nal,
                                                     target_kbps=cap.qsv_target_kbps,
                                                     max_kbps=cap.qsv_max_kbps,
                                                     fps=cap.target_hz,
                                                     rc_mode=cap.rc_mode,
                                                     qp=cap.qp,
                                                     target_usage=cap.target_usage)
                        if cap.qsv_encoder:
                            cap.qsv_encoder.force_idr()
                            trim_memory()

                    cur_send_dur = 0.0
                    t_enc_start = time.perf_counter()
                    cap.qsv_encoder.write_frame(cap.last_raw, src_w=cap.active_w, src_h=cap.active_h, src_pitch=cap.active_w * 4)
                    t_enc_ms = (time.perf_counter() - t_enc_start) * 1000.0

                    perf_samples.append((t_cap_ms, t_enc_ms, cur_send_dur))
                    now_perf = time.perf_counter()
                    if len(perf_samples) >= 60 and (now_perf - last_report_t) >= 2.0:
                        avg_c = sum(s[0] for s in perf_samples) / len(perf_samples)
                        avg_e = sum(s[1] for s in perf_samples) / len(perf_samples)
                        avg_s = sum(s[2] for s in perf_samples) / len(perf_samples)
                        fps = len(perf_samples) / (now_perf - last_report_t)
                        cap.last_cap_ms, cap.last_enc_ms, cap.last_send_ms = avg_c, avg_e, avg_s
                        say(f"[SERVER PERF (60-frame avg)] Capture: {avg_c:.2f}ms | Encode (QSV): {avg_e:.2f}ms | SendWait: {avg_s:.2f}ms | Server FPS: {fps:.1f}")
                        last_report_t = now_perf

                    now = time.monotonic()
                    if len(perf_samples) in (1, 5) or (now - last_trim > 3.0):
                        last_trim = now
                        trim_memory()

                    frame_dur = time.perf_counter() - t_frame_start
                    sleep_time = max(0.001, target_interval - frame_dur)
                    time.sleep(sleep_time)
                    continue

                # ENGINE 0: Sparse Tile TurboJPEG (Desktop & Productivity Mode)
                if cap.qsv_encoder:
                    cap.qsv_encoder.close(); cap.qsv_encoder = None

                if getattr(cap, "force_dirty_refresh", False):
                    cap.force_dirty_refresh = False
                    cap.prev_crcs = None

                t_enc_start = time.perf_counter()
                dirty_tiles = cap.diff_tiles_crc()
                if not dirty_tiles:
                    t_enc_ms = (time.perf_counter() - t_enc_start) * 1000.0
                    perf_samples.append((t_cap_ms, t_enc_ms, 0.0))

                    if timer_boosted and winmm:
                        try: winmm.timeEndPeriod(1)
                        except Exception: pass
                        timer_boosted = False

                    frame_dur = time.perf_counter() - t_frame_start
                    if cap.idle_ms > 0:
                        idle_timeout = cap.idle_ms / 1000.0
                        for c in active_cls:
                            c.wake_ev.wait(timeout=idle_timeout)
                            c.wake_ev.clear()
                    else:
                        # Correction 3: Always sleep remainder of target frame interval
                        sleep_time = max(0.001, target_interval - frame_dur)
                        time.sleep(sleep_time)
                    continue

                if not timer_boosted and winmm:
                    try: winmm.timeBeginPeriod(1)
                    except Exception: pass
                    timer_boosted = True

                encoded_tiles = []
                total_tiles = cap.ty * cap.tx
                if len(dirty_tiles) > (total_tiles * 0.45):
                    # Motion bailout: full-frame TurboJPEG encode strictly at authoritative session resolution
                    full_frame = cap.cur[:cap.active_h, :cap.active_w]
                    jpeg_bytes = cap.tj_encoder.encode(full_frame)
                    fw_out, fh_out = cap.active_w, cap.active_h
                    if jpeg_bytes:
                        payload = bytes([0x01]) + struct.pack(">HHHH", 0, 0, fw_out, fh_out) + jpeg_bytes
                        encoded_tiles.append(payload)
                else:
                    for (x, y, w, h) in dirty_tiles:
                        tile = cap.cur[y:y+h, x:x+w]
                        jpeg_bytes = cap.tj_encoder.encode(tile)
                        if not jpeg_bytes: continue
                        payload = bytes([0x01]) + struct.pack(">HHHH", x, y, w, h) + jpeg_bytes
                        encoded_tiles.append(payload)

                t_enc_ms = (time.perf_counter() - t_enc_start) * 1000.0

                t_send_start = time.perf_counter()
                if encoded_tiles:
                    commit_pkt = bytes([0x03])
                    all_pkts = encoded_tiles + [commit_pkt]
                    for c in active_cls:
                        queue_put_drop_stale(c.send_queue, (0x00, all_pkts))
                t_send_ms = (time.perf_counter() - t_send_start) * 1000.0

                perf_samples.append((t_cap_ms, t_enc_ms, t_send_ms))
                now_perf = time.perf_counter()
                if len(perf_samples) >= 60 and (now_perf - last_report_t) >= 2.0:
                    avg_c = sum(s[0] for s in perf_samples) / len(perf_samples)
                    avg_e = sum(s[1] for s in perf_samples) / len(perf_samples)
                    avg_s = sum(s[2] for s in perf_samples) / len(perf_samples)
                    fps = len(perf_samples) / (now_perf - last_report_t)
                    cap.last_cap_ms, cap.last_enc_ms, cap.last_send_ms = avg_c, avg_e, avg_s
                    say(f"[SERVER PERF (60-frame avg)] Capture: {avg_c:.2f}ms | Encode (TJ): {avg_e:.2f}ms | SendWait: {avg_s:.2f}ms | Server FPS: {fps:.1f}")
                    last_report_t = now_perf

                now = time.monotonic()
                if len(perf_samples) in (1, 5) or (now - last_trim > 3.0):
                    last_trim = now
                    trim_memory()

                frame_dur = time.perf_counter() - t_frame_start
                sleep_time = max(0.001, target_interval - frame_dur)
                time.sleep(sleep_time)
            except Exception as e:
                say(f"sweeper error: {e!r}")
                time.sleep(0.05)
    finally:
        if timer_boosted and winmm:
            try: winmm.timeEndPeriod(1)
            except Exception: pass
        if cap.qsv_encoder:
            cap.qsv_encoder.close(); cap.qsv_encoder = None

# ---------------------------------------------------------------- Memory Optimization
def trim_memory():
    """Trims working set and collects garbage to minimize resident RAM usage."""
    try:
        gc.collect()
        if psapi:
            psapi.EmptyWorkingSet(kernel32.GetCurrentProcess())
    except Exception: pass

# ---------------------------------------------------------------- Virtual Driver Setup
def is_virtual_driver_installed():
    """Checks if MTT Virtual Display Driver is already installed on the system."""
    try:
        res = subprocess.run(["pnputil", "/enum-drivers"], capture_output=True, text=True)
        if "mttvdd.inf" in (res.stdout or "").lower():
            return True
    except Exception: pass

    try:
        pdir = Path("C:/VirtualDisplayDriver")
        src_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "driver"
        devcon_path = next((p for p in (pdir / "devcon.exe", src_dir / "devcon.exe") if p.exists()), None)
        if devcon_path:
            res = subprocess.run([str(devcon_path), "status", "Root\\MttVDD*"], capture_output=True, text=True)
            out = ((res.stdout or "") + (res.stderr or "")).lower()
            if "matching device(s) found" in out and "no matching devices" not in out:
                return True
    except Exception: pass
    return False

def airgap_virtual_displays():
    """Positions all MTT virtual displays with an isolated air-gap offset placed strictly outside the physical bounding rectangle: dmPosition.x = max_x + 500."""
    try:
        min_x, min_y, max_x, max_y = get_physical_desktop_bounds()
        airgap_x = max_x + 500
        dd = DISPLAY_DEVICE()
        dd.cb = ctypes.sizeof(DISPLAY_DEVICE)
        i = 0
        while user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
            if (dd.StateFlags & 0x1) and not (dd.StateFlags & 0x4): # Attached, not primary
                is_virt = any(k in dd.DeviceString.lower() or k in dd.DeviceName.lower() or k in dd.DeviceID.lower() for k in ("mtt", "vdd", "virtual", "idd"))
                if is_virt:
                    cur_dm = DEVMODEW()
                    cur_dm.dmSize = ctypes.sizeof(DEVMODEW)
                    if user32.EnumDisplaySettingsW(dd.DeviceName, -1, ctypes.byref(cur_dm)):
                        cur_dm.dmPosition_x = airgap_x
                        cur_dm.dmPosition_y = min_y
                        cur_dm.dmFields = 0x00080000 | 0x00100000 | 0x00000020 # DM_PELSWIDTH | DM_PELSHEIGHT | DM_POSITION
                        user32.ChangeDisplaySettingsExW(dd.DeviceName, ctypes.byref(cur_dm), None, 1, None) # CDS_UPDATEREGISTRY
                        airgap_x += cur_dm.dmPelsWidth + 500
            i += 1
        user32.ChangeDisplaySettingsExW(None, None, None, 0, None)
    except Exception as e:
        say(f"airgap_virtual_displays error: {e!r}")

def ensure_autostart():
    """Ensures vddmon server is registered to run on Windows startup if not already set."""
    try:
        if getattr(sys, "frozen", False):
            target_cmd = f'"{sys.executable}" run'
        else:
            pyw = Path(sys.executable).with_name("pythonw.exe")
            py_exe = str(pyw if pyw.exists() else sys.executable)
            srv_py = str(Path(__file__).resolve())
            target_cmd = f'"{py_exe}" "{srv_py}" run'

        # 1. Check Scheduled Task
        proc = subprocess.run(["schtasks", "/Query", "/TN", "VDDMonServer"], capture_output=True, text=True)
        has_task = (proc.returncode == 0)

        # 2. Check HKCU Run registry
        import winreg
        has_reg = False
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
            val, _ = winreg.QueryValueEx(k, "VDDMonServer")
            has_reg = bool(val)
            winreg.CloseKey(k)
        except Exception:
            pass

        if has_task or has_reg:
            return

        say("autostart: configuring server to run on Windows startup...")

        try:
            k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run")
            winreg.SetValueEx(k, "VDDMonServer", 0, winreg.REG_SZ, target_cmd)
            winreg.CloseKey(k)
            say("autostart: registered in HKCU Run registry.")
        except Exception as e:
            say(f"autostart: registry error: {e!r}")

        res = subprocess.run(["schtasks", "/Create", "/F", "/TN", "VDDMonServer",
                              "/TR", target_cmd, "/SC", "ONLOGON", "/RL", "HIGHEST"],
                             capture_output=True, text=True)
        if res.returncode != 0:
            subprocess.run(["schtasks", "/Create", "/F", "/TN", "VDDMonServer",
                            "/TR", target_cmd, "/SC", "ONLOGON"],
                           capture_output=True)
        say("autostart: scheduled task configured.")
    except Exception as e:
        say(f"autostart check error: {e!r}")

def detach_virtual_displays_ccd():
    """Detaches all MTT/VDD virtual displays via Win32 CCD (Connecting and Configuring Displays) API."""
    def _do_detach():
        attach_input_desktop()
        QDC_ONLY_ACTIVE_PATHS = 2
        num_paths = wintypes.UINT()
        num_modes = wintypes.UINT()
        err = user32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(num_paths), ctypes.byref(num_modes))
        if err != 0 or num_paths.value == 0:
            return False

        paths = (DISPLAYCONFIG_PATH_INFO * num_paths.value)()
        modes = (DISPLAYCONFIG_MODE_INFO * num_modes.value)()

        err = user32.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(num_paths), paths, ctypes.byref(num_modes), modes, None)
        if err != 0:
            return False

        detached_count = 0
        for i in range(num_paths.value):
            p = paths[i]
            target_name = DISPLAYCONFIG_TARGET_DEVICE_NAME()
            target_name.header.type = 2 # DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME
            target_name.header.size = ctypes.sizeof(DISPLAYCONFIG_TARGET_DEVICE_NAME)
            target_name.header.adapterId = p.targetInfo.adapterId
            target_name.header.id = p.targetInfo.id
            if user32.DisplayConfigGetDeviceInfo(ctypes.byref(target_name.header)) == 0:
                friendly = target_name.monitorFriendlyDeviceName.lower()
                path_str = target_name.monitorDevicePath.lower()
                if "mtt" in friendly or "vdd" in friendly or "mtt" in path_str or "vdd" in path_str:
                    p.flags = 0  # Mark path inactive
                    detached_count += 1

        if detached_count > 0:
            SDC_APPLY = 0x00000080
            SDC_ALLOW_CHANGES = 0x00000400
            SDC_USE_SUPPLIED_DISPLAY_CONFIG = 0x00000020
            res = user32.SetDisplayConfig(num_paths.value, paths, num_modes.value, modes,
                                          SDC_APPLY | SDC_ALLOW_CHANGES | SDC_USE_SUPPLIED_DISPLAY_CONFIG)
            if res == 0:
                say(f"teardown: detached {detached_count} virtual display path(s) via CCD.")
                return True
            else:
                say(f"teardown: SetDisplayConfig returned error code {res}")
        return False

    try:
        # Run on dedicated clean thread to prevent Win32 ERROR_BUSY (170) on threads with COM / GUI windows
        res = [False]
        t = threading.Thread(target=lambda: res.__setitem__(0, _do_detach()))
        t.start()
        t.join(timeout=3.0)
        return res[0]
    except Exception as e:
        say(f"detach_virtual_displays_ccd error: {e!r}")
        return False

def cmd_watchdog(target_pid):
    """Detached kernel supervisor: waits for server PID to terminate and triggers CCD teardown."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        SYNCHRONIZE = 0x00100000
        h_proc = kernel32.OpenProcess(SYNCHRONIZE, False, target_pid)
        if h_proc:
            kernel32.WaitForSingleObject(h_proc, 0xFFFFFFFF)
            kernel32.CloseHandle(h_proc)
            cleanup_virtual_displays()
    except Exception:
        pass

def spawn_watchdog():
    """Spawns an independent, detached supervisor process that monitors this server PID.
    If the server crashes, is force-killed (taskkill /F), or halts, the watchdog instantly
    triggers Win32 CCD virtual display teardown."""
    try:
        pid = os.getpid()
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "watchdog", str(pid)]
        else:
            cmd = [sys.executable, str(Path(__file__).resolve()), "watchdog", str(pid)]
        DETACHED_PROCESS = 0x00000008
        CREATE_NO_WINDOW = 0x08000000
        subprocess.Popen(cmd, creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW, close_fds=True)
        say(f"watchdog: supervisor launched for PID {pid}")
    except Exception as e:
        say(f"watchdog spawn warning: {e!r}")

def ensure_virtual_display():
    """Verifies, enables, and extends virtual display drivers on server startup."""
    try:
        if not is_virtual_driver_installed():
            say("driver: MTT Virtual Display Driver not installed; running setup...")
            setup_virtual_driver()

        pdir = Path("C:/VirtualDisplayDriver")
        src_dir = Path(sys._MEIPASS) / "driver" if (getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")) else Path(__file__).resolve().parent / "driver"
        vdir = BASE / "vdisplay" / "VirtualDisplayDriver"
        devcon_path = next((p for p in (pdir / "devcon.exe", src_dir / "devcon.exe", vdir / "devcon.exe") if p.exists()), None)

        if devcon_path:
            proc = subprocess.run([str(devcon_path), "status", "Root\\MttVDD*"], capture_output=True, text=True)
            out = ((proc.stdout or "") + (proc.stderr or "")).lower()
            if "disabled" in out or "no matching devices" in out:
                say("display: virtual display driver disabled; enabling...")
                res = subprocess.run([str(devcon_path), "enable", "Root\\MttVDD*"], capture_output=True, text=True)
                subprocess.run([str(devcon_path), "enable", "@ROOT\\DISPLAY\\*"], capture_output=True)
                if "enable failed" in ((res.stdout or "") + (res.stderr or "")).lower():
                    try:
                        cmd = f'Start-Process -FilePath "{devcon_path}" -ArgumentList \'enable "Root\\MttVDD*"\' -Verb RunAs -Wait -WindowStyle Hidden'
                        subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, timeout=5)
                    except Exception: pass

        for inst in ("ROOT\\DISPLAY\\0000", "ROOT\\DISPLAY\\0005", "ROOT\\DISPLAY\\0006"):
            subprocess.run(["pnputil", "/enable-device", inst], capture_output=True)

        attach_input_desktop()
        try:
            user32.SetDisplayConfig(0, None, 0, None, 0x00000080 | 0x00000004)
        except Exception: pass
        subprocess.run(["DisplaySwitch.exe", "/extend"], capture_output=True)

        time.sleep(0.8)
        ensure_physical_primary()
        airgap_virtual_displays()
    except Exception as e:
        say(f"display: ensure_virtual_display error: {e!r}")

def setup_virtual_driver():
    say("driver: setting up MTT Virtual Display Driver...")
    pdir = Path("C:/VirtualDisplayDriver")
    pdir.mkdir(parents=True, exist_ok=True)
    pfdir = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Virtual Display Driver"
    try: pfdir.mkdir(parents=True, exist_ok=True)
    except Exception: pass
    vdir = BASE / "vdisplay" / "VirtualDisplayDriver"
    vdir.mkdir(parents=True, exist_ok=True)

    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        src_dir = Path(sys._MEIPASS) / "driver"
    else:
        src_dir = Path(__file__).resolve().parent / "driver"

    if not src_dir.exists() or not (src_dir / "MttVDD.inf").exists():
        src_dir = vdir

    for fn in ("MttVDD.dll", "MttVDD.inf", "mttvdd.cat", "vdd_settings.xml", "adapter.txt", "devcon.exe"):
        sf = src_dir / fn
        if sf.exists():
            for td in (pdir, pfdir, vdir):
                try: shutil.copy2(sf, td / fn)
                except Exception: pass

    # Enforce Windows Graphics Settings to run wudfhost.exe on Integrated GPU (Power Saving)
    try:
        import winreg
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                k = winreg.CreateKey(root, r"Software\Microsoft\DirectX\UserGpuPreferences")
                winreg.SetValueEx(k, r"C:\Windows\System32\wudfhost.exe", 0, winreg.REG_SZ, "GpuPreference=1;")
                winreg.CloseKey(k)
            except Exception: pass
    except Exception: pass

    inf_path = pdir / "MttVDD.inf"
    devcon_path = pdir / "devcon.exe"
    if inf_path.exists():
        subprocess.run(["pnputil", "/add-driver", str(inf_path), "/install"], capture_output=True)
    if devcon_path.exists():
        # Strictly target MTT virtual display driver; never touch physical GPUs (PCI\VEN_*)
        subprocess.run([str(devcon_path), "enable", "Root\\MttVDD*"], capture_output=True)
        subprocess.run([str(devcon_path), "enable", "@ROOT\\DISPLAY\\*"], capture_output=True)
        subprocess.run([str(devcon_path), "restart", "Root\\MttVDD*"], capture_output=True)
    
    # Extend desktop topology onto virtual monitors
    attach_input_desktop()
    try:
        user32.SetDisplayConfig(0, None, 0, None, 0x00000080 | 0x00000004)
    except Exception: pass
    subprocess.run(["DisplaySwitch.exe", "/extend"], capture_output=True)
    time.sleep(0.8)
    ensure_physical_primary()
    airgap_virtual_displays()
    return True, "Virtual Display Driver installed and activated on Integrated GPU."

def cleanup_virtual_displays():
    """Safely tears down virtual displays, disables the virtual driver, and reverts desktop to primary screen."""
    unclip_and_recenter_cursor()
    say("teardown: detaching virtual displays...")
    try:
        detach_virtual_displays_ccd()
    except Exception: pass

    def _revert_internal():
        attach_input_desktop()
        user32.SetDisplayConfig(0, None, 0, None, 0x00000080 | 0x00000001) # SDC_APPLY | SDC_TOPOLOGY_INTERNAL

    try:
        t_rev = threading.Thread(target=_revert_internal)
        t_rev.start()
        t_rev.join(timeout=2.0)
    except Exception: pass

    try:
        subprocess.run(["DisplaySwitch.exe", "/internal"], capture_output=True, timeout=5)
    except Exception: pass

    try:
        pdir = Path("C:/VirtualDisplayDriver")
        src_dir = Path(sys._MEIPASS) / "driver" if (getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")) else Path(__file__).resolve().parent / "driver"
        vdir = BASE / "vdisplay" / "VirtualDisplayDriver"
        devcon_path = next((p for p in (pdir / "devcon.exe", src_dir / "devcon.exe", vdir / "devcon.exe") if p.exists()), None)
        if devcon_path:
            subprocess.run([str(devcon_path), "disable", "Root\\MttVDD*"], capture_output=True, timeout=5)
    except Exception: pass
    unclip_and_recenter_cursor()

# ---------------------------------------------------------------- Network Discovery & Pairing QR
def get_local_ips():
    ips = []
    try:
        host_name = socket.gethostname()
        for info in socket.getaddrinfo(host_name, None):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception: pass
    return ips

def get_local_endpoints(port=5900):
    endpoints = []
    seen = set()
    try:
        import psutil
        for iface, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                # IPv4
                if a.family == socket.AF_INET:
                    ip = a.address
                    if ip.startswith("127."):
                        continue
                    is_wg = any(k in iface.lower() for k in ("wg", "wireguard", "server", "client"))
                    if ip.startswith("169.254.") and not is_wg:
                        continue
                    if ip not in seen:
                        seen.add(ip)
                        tag = "[WIREGUARD]" if is_wg else "[LAN/DIRECT]"
                        endpoints.append((tag, iface, ip, f"{ip}:{port}"))
                # IPv6
                elif a.family == socket.AF_INET6:
                    raw_ip = a.address.split("%")[0]
                    if raw_ip in ("::1", "::"):
                        continue
                    is_wg = any(k in iface.lower() for k in ("wg", "wireguard", "server", "client"))
                    if raw_ip.lower().startswith("fe80:") and not is_wg:
                        continue
                    if raw_ip not in seen:
                        seen.add(raw_ip)
                        tag = "[IPv6/GLOBAL]" if not raw_ip.lower().startswith("fe80:") else "[IPv6/LINK]"
                        endpoints.append((tag, iface, raw_ip, f"[{raw_ip}]:{port}"))
    except Exception: pass

    if not endpoints:
        for ip in get_local_ips():
            if not ip.startswith("169.254."):
                tgt = f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"
                tag = "[IPv6]" if ":" in ip else "[LAN/DIRECT]"
                endpoints.append((tag, "Default", ip, tgt))

    # Priority sorting for QR pairing:
    # 1. Global / Routable IPv6 (2xxx:, 3xxx:, etc.) - directly reachable over internet/WAN
    # 2. WireGuard (10.0.0.x or WireGuard interface)
    # 3. Local LAN IPv4 (192.168.x.x)
    # 4. Other IPv6 / Other IPv4
    def ep_sort(item):
        tag, iface, raw_ip, ep_str = item
        # Prioritize Global/Routable IPv6
        if ":" in raw_ip and not raw_ip.lower().startswith("fe80:") and not raw_ip.lower().startswith("::"):
            return 0
        if raw_ip.startswith("10.0.0.") or "[WIREGUARD]" in tag:
            return 1
        if ":" in raw_ip:
            return 2
        if raw_ip.startswith("192.168."):
            return 3
        if raw_ip.startswith("172.") or raw_ip.startswith("10."):
            return 4
        return 5

    endpoints.sort(key=ep_sort)
    return endpoints

def print_pairing_banner(bind_host, bind_port):
    """Renders terminal ASCII QR code and prints all IPv4/IPv6 endpoints on server startup."""
    endpoints = get_local_endpoints(bind_port)
    primary_uri = None
    primary_label = ""

    if bind_host not in ("0.0.0.0", "::", "", None):
        tgt = f"[{bind_host}]:{bind_port}" if ":" in bind_host else f"{bind_host}:{bind_port}"
        primary_uri = f"vddmon://{tgt}"
        primary_label = f"Explicit Binding ({tgt})"
    elif endpoints:
        tag, iface, raw_ip, ep_str = endpoints[0]
        primary_uri = f"vddmon://{ep_str}"
        primary_label = f"{iface} ({ep_str})"
    else:
        primary_uri = f"vddmon://127.0.0.1:{bind_port}"
        primary_label = f"Loopback (127.0.0.1:{bind_port})"

    print("\n" + "=" * 62)
    print("      vddmon Remote Desktop — Pairing & Connection QR")
    print("=" * 62)

    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(primary_uri)
        qr.make(fit=True)
        print()
        qr.print_ascii(invert=True)
        print()
    except Exception:
        try:
            import qrcode
            qr = qrcode.QRCode(border=1)
            qr.add_data(primary_uri)
            qr.make(fit=True)
            matrix = qr.get_matrix()
            for row in matrix:
                print("".join("##" if cell else "  " for cell in row))
        except Exception:
            pass

    print(f"Primary Pairing URI: {primary_uri}")
    print(f"Target Interface:    {primary_label}")
    print()
    print("Available Connection Endpoints:")
    for tag, iface, raw_ip, ep_str in endpoints:
        print(f"  • {tag:<14} {iface:<20} -> {ep_str} (vddmon://{ep_str})")
    print("=" * 62 + "\n")

# ---------------------------------------------------------------- Server Daemon & CLI
def cmd_run(args):
    global RUNNING, CAP
    bind_host = getattr(args, "host", None) or "0.0.0.0"
    bind_port = getattr(args, "port", None) or 5900

    try:
        f = open(LOCK_F, "x"); f.write(str(os.getpid())); f.close()
        atexit.register(lambda: LOCK_F.unlink(missing_ok=True))
        atexit.register(unclip_and_recenter_cursor)
        atexit.register(cleanup_virtual_displays)
    except FileExistsError:
        is_alive = False
        try:
            old_pid = int(LOCK_F.read_text().strip())
            h_proc = kernel32.OpenProcess(0x1000, False, old_pid)
            if h_proc:
                exit_code = wintypes.DWORD()
                if kernel32.GetExitCodeProcess(h_proc, ctypes.byref(exit_code)):
                    is_alive = (exit_code.value == 259) # STILL_ACTIVE
                    kernel32.CloseHandle(h_proc)
        except Exception: pass
        if is_alive:
            try:
                check_host = "127.0.0.1" if bind_host in ("0.0.0.0", "::", "", None) else bind_host
                s = socket.create_connection((check_host, bind_port), 2)
                s.close(); say("already running on " + str(check_host) + ":" + str(bind_port)); sys.exit(2)
            except Exception: pass
        LOCK_F.unlink(missing_ok=True)
        return cmd_run(args)

    prio = 0x40
    kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), prio)
    try: kernel32.SetThreadPriority(kernel32.GetCurrentThread(), -15)
    except Exception: pass

    spawn_watchdog()
    ensure_autostart()
    ensure_virtual_display()

    cap = Capture(idx=getattr(args, "monitor", None))
    CAP = cap
    inj = Injector(cap)
    cap.injector = inj

    threading.Thread(target=cursor_barrier_worker, daemon=True).start()
    threading.Thread(target=sweeper, args=(cap,), daemon=True).start()

    print_pairing_banner(bind_host, bind_port)

    if getattr(args, "pair", False):
        global ACTIVE_PAIRING_SESSION
        if vddmon_p2p:
            say("Discovering dual-stack endpoints (UPnP / Global IPv6)...")
            ep = vddmon_p2p.discover_endpoints(bind_port)
            ACTIVE_PAIRING_SESSION = vddmon_p2p.PairingSession(
                v6=ep.get("v6"),
                v4=ep.get("v4"),
                port=bind_port,
                upnp_port=ep.get("upnp_port"),
                validity_sec=120
            )
            ACTIVE_PAIRING_SESSION.print_cli()
        else:
            say("warning: vddmon_p2p not available, cannot initialize P2P pairing")

    if bind_host in ("0.0.0.0", "::", "", None):
        try:
            srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            srv.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("::", bind_port))
        except Exception:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", bind_port))
    elif ":" in bind_host:
        srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((bind_host, bind_port))
    else:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((bind_host, bind_port))

    srv.listen(4)
    say(f"vddmon {VERSION} listening on {bind_host}:{bind_port} (Hybrid TurboJPEG/QSV)")

    import signal
    def _sig_handler(signum, frame):
        global RUNNING
        say(f"signal {signum} received; shutting down...")
        RUNNING = False
        try: srv.close()
        except Exception: pass
        unclip_and_recenter_cursor()
        cleanup_virtual_displays()
        sys.exit(0)

    try:
        signal.signal(signal.SIGINT, _sig_handler)
        signal.signal(signal.SIGTERM, _sig_handler)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, _sig_handler)
    except Exception: pass

    try:
        while RUNNING:
            try:
                s, addr = srv.accept()
            except OSError: break
            threading.Thread(target=lambda: Client(s, addr, cap, inj).serve(), daemon=True).start()
    finally:
        RUNNING = False
        try: srv.close()
        except Exception: pass
        unclip_and_recenter_cursor()
        cleanup_virtual_displays()

def cmd_passwd(args):
    pw = args.password if hasattr(args, "password") and args.password else None
    if not pw:
        import getpass
        pw = getpass.getpass("Enter master password: ").strip()
    if not pw:
        print("Password cannot be empty!"); return
    write_auth(pw)
    print("Master password updated successfully (PBKDF2-HMAC-SHA256 200k rounds)")

def cmd_install(args):
    print("Step 1/3: Installing Virtual Display Driver...")
    setup_virtual_driver()

    print("Step 2/3: Configuring Windows autostart task...")
    exe = str(Path(sys.executable).with_name("pythonw.exe"))
    srv = str(Path(__file__).resolve())
    tr = f'"{exe}" "{srv}" run'
    subprocess.run(["schtasks", "/Create", "/F", "/TN", "VDDMon", "/TR", tr, "/SC", "ONLOGON", "/RL", "HIGHEST"],
                   capture_output=True)

    print("Step 3/3: Opening Port 5900 in Windows Firewall...")
    subprocess.run(["netsh", "advfirewall", "firewall", "add", "rule",
                    "name=VDDMon", "dir=in", "action=allow", "protocol=TCP", "localport=5900"],
                   capture_output=True)
    subprocess.run(["schtasks", "/Run", "/TN", "VDDMon"], capture_output=True)
    print("Installation complete! Port 5900 active.")

def cmd_uninstall(args):
    subprocess.run(["schtasks", "/End", "/TN", "VDDMon"], capture_output=True)
    subprocess.run(["schtasks", "/Delete", "/TN", "VDDMon", "/F"], capture_output=True)
    subprocess.run(["schtasks", "/End", "/TN", "VDDMonServer"], capture_output=True)
    subprocess.run(["schtasks", "/Delete", "/TN", "VDDMonServer", "/F"], capture_output=True)
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(k, "VDDMonServer")
        winreg.CloseKey(k)
    except Exception: pass
    subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule", "name=VDDMon"], capture_output=True)
    LOCK_F.unlink(missing_ok=True)
    cleanup_virtual_displays()
    print("VDDMon uninstalled and virtual displays cleaned up.")

def main():
    p = argparse.ArgumentParser(description="VDDMon Remote Desktop Server")
    p.add_argument("--host", "--bind", default="0.0.0.0", dest="host", help="Listening host IP (default: 0.0.0.0, e.g. 10.0.0.1 for WireGuard)")
    p.add_argument("--port", "-p", type=int, default=5900, help="Listening port (default: 5900)")
    p.add_argument("--monitor", "-m", type=int, default=None, help="Monitor index to capture (default: auto-detect virtual monitor)")
    p.add_argument("--pair", action="store_true", help="Generate ephemeral P2P pairing token and listen")

    sp = p.add_subparsers(dest="cmd")
    p_run = sp.add_parser("run", help="Run VDDMon server daemon")
    p_run.add_argument("--host", "--bind", default=None, dest="host", help="Listening host IP (default: 0.0.0.0, e.g. 10.0.0.1 for WireGuard)")
    p_run.add_argument("--port", "-p", type=int, default=None, help="Listening port (default: 5900)")
    p_run.add_argument("--monitor", "-m", type=int, default=None, help="Monitor index to capture (default: auto-detect virtual monitor)")
    p_run.add_argument("--pair", action="store_true", help="Generate ephemeral P2P pairing token and listen")

    p_pair = sp.add_parser("pair", help="Generate ephemeral P2P pairing token and start server")
    p_pair.add_argument("--host", default=None, help="Listening host IP")
    p_pair.add_argument("--port", "-p", type=int, default=None, help="Listening port (default: 5900)")
    p_pair.add_argument("--monitor", "-m", type=int, default=None, help="Monitor index to capture (default: auto-detect virtual monitor)")

    p_pw = sp.add_parser("passwd", aliases=["init-password"])
    p_pw.add_argument("--password", "-p", help="Master password")
    sp.add_parser("install")
    sp.add_parser("uninstall")
    sp.add_parser("ip")
    sp.add_parser("qr")
    p_watch = sp.add_parser("watchdog", help="Detached kernel watchdog supervisor")
    p_watch.add_argument("target_pid", type=int)

    args = p.parse_args()
    if args.cmd == "watchdog":
        cmd_watchdog(args.target_pid)
        return
    elif args.cmd == "pair":
        args.pair = True
        cmd_run(args)
    elif args.cmd == "run" or args.cmd is None:
        cmd_run(args)
    elif args.cmd in ("passwd", "init-password"): cmd_passwd(args)
    elif args.cmd == "install": cmd_install(args)
    elif args.cmd == "uninstall": cmd_uninstall(args)
    elif args.cmd == "qr":
        port = getattr(args, "port", 5900) or 5900
        print_pairing_banner("0.0.0.0", port)
    elif args.cmd == "ip":
        port = getattr(args, "port", 5900) or 5900
        endpoints = get_local_endpoints(port)
        if endpoints:
            for tag, iface, raw_ip, ep in endpoints:
                print(f"   {tag:<14} {iface} -> {ep}")
        else:
            for ip in get_local_ips():
                print(f"   [ENDPOINT]     {ip}:{port}")
    else:
        cmd_run(args)

if __name__ == "__main__":
    main()