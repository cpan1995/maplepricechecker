"""Locate the MapleStory window and screenshot its client area."""
import ctypes
from ctypes import wintypes

import mss
from PIL import Image

user32 = ctypes.windll.user32


def make_dpi_aware():
    # Physical pixels everywhere, so screenshot coords == overlay coords.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        user32.SetProcessDPIAware()


def find_window(title):
    """First visible top-level window titled exactly `title`.

    Exact, so a browser tab like "... MapleStory Classic | NiaMeowDB" doesn't match."""
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.strip().lower() == title.lower():
                found.append(hwnd)
                return False
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def client_rect(hwnd):
    """(left, top, width, height) of the window's client area in screen coords."""
    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    origin = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    return origin.x, origin.y, rect.right, rect.bottom


def grab(rect):
    left, top, width, height = rect
    with mss.mss() as sct:
        shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def primary_monitor_rect():
    with mss.mss() as sct:
        m = sct.monitors[1]
    return m["left"], m["top"], m["width"], m["height"]
