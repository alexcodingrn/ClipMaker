"""
ClipMaker's window helpers. Only uses built-in Python, so it loads instantly:
the loading screen can pop up before the heavy video stuff is ready.
"""
import os
import socket
import webbrowser
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
PORT = 7860


def open_window(url):
    """Open ClipMaker in its own app window using a clean browser profile (no extensions),
    so Chrome extensions can't interfere with video playback."""
    import subprocess
    profile = APP_DIR / "browser-profile"
    candidates = []
    for base in (os.environ.get("PROGRAMFILES", r"C:\Program Files"), os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", "")):
        if base:
            candidates += [Path(base) / "Google/Chrome/Application/chrome.exe",
                           Path(base) / "Microsoft/Edge/Application/msedge.exe"]
    for exe in candidates:
        if exe.exists():
            try:
                subprocess.Popen([str(exe), f"--app={url}", f"--user-data-dir={profile}", "--window-size=1500,950",
                                  "--no-first-run", "--no-default-browser-check", "--disable-extensions"])
                return
            except Exception:
                pass
    webbrowser.open(url)


def focus_window():
    """If a ClipMaker window is already open, bring it to the front instead of opening another one."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if not u.IsWindowVisible(hwnd):
            return True
        title = ctypes.create_unicode_buffer(256)
        u.GetWindowTextW(hwnd, title, 256)
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cls, 256)
        if title.value.startswith("ClipMaker") and cls.value.startswith("Chrome_WidgetWin"):
            found.append(hwnd)
            return False
        return True

    u.EnumWindows(each, 0)
    if not found:
        return False
    hwnd = found[0]
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, 9)          # restore if minimized
    else:
        u.ShowWindow(hwnd, 5)
    u.keybd_event(0x12, 0, 0, 0)       # tap Alt so Windows lets us bring it forward
    u.keybd_event(0x12, 0, 2, 0)
    u.SetForegroundWindow(hwnd)
    return True


def already_running():
    with socket.socket() as sk:
        sk.settimeout(0.5)
        return sk.connect_ex(("127.0.0.1", PORT)) == 0
