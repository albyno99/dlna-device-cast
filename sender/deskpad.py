"""
deskpad.py - uses DeskPad to make the TV an extended display, only while casting.

DeskPad (brew install --cask deskpad) creates a virtual monitor that macOS treats as an extended
display; that display is what gets cast. DeskPad is started hidden when casting begins and quit
when it ends, so it is not present during normal use.
"""

import ctypes
import ctypes.util
import os
import subprocess
import sys
import time

# Preferred location is inside the project (brew install --cask --appdir=tools.noindex deskpad);
# /Applications is the fallback for a regular install.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = next((p for p in (os.path.join(ROOT, "tools.noindex", "DeskPad.app"), "/Applications/DeskPad.app")
            if os.path.isdir(p)), os.path.join(ROOT, "tools.noindex", "DeskPad.app"))
# Must start with "Capture screen": the menu bar app lists only those entries.
EXTEND_NAME = "Capture screen: extended display (DeskPad)"
VENDOR, MODEL = 0x3456, 0x1234   # fixed identifiers of the DeskPad virtual monitor


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, file=sys.stderr, flush=True)


def installed():
    return os.path.isdir(APP)


def wants_extend(screen_spec):
    return "extended display" in (screen_spec or "").lower()


def _cg():
    cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
    cg.CGGetActiveDisplayList.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                                          ctypes.POINTER(ctypes.c_uint32)]
    for f in ("CGDisplayVendorNumber", "CGDisplayModelNumber", "CGDisplayPixelsWide", "CGDisplayPixelsHigh"):
        getattr(cg, f).restype = ctypes.c_uint32
        getattr(cg, f).argtypes = [ctypes.c_uint32]
    return cg


def display_index():
    """(index, width, height) of the DeskPad monitor as "Capture screen N" (ffmpeg order), or None."""
    try:
        cg = _cg()
        ids = (ctypes.c_uint32 * 16)()
        n = ctypes.c_uint32()
        cg.CGGetActiveDisplayList(16, ids, ctypes.byref(n))
        for i in range(n.value):
            if cg.CGDisplayVendorNumber(ids[i]) == VENDOR and cg.CGDisplayModelNumber(ids[i]) == MODEL:
                return i, cg.CGDisplayPixelsWide(ids[i]), cg.CGDisplayPixelsHigh(ids[i])
    except Exception as e:  # noqa: BLE001
        log(f"(could not read the display list: {e})")
    return None


def running():
    return subprocess.run(["pgrep", "-x", "DeskPad"], capture_output=True).returncode == 0


def start(timeout=10):
    """Starts DeskPad hidden and waits for its monitor. Returns the screen index or None."""
    if not installed():
        log("DeskPad is not installed (brew install --cask --appdir=tools.noindex deskpad); using the main display")
        return None
    found = display_index()
    if not found:
        log("starting DeskPad to use the TV as an extended display")
        subprocess.run(["open", "-g", "-j", "-a", APP], capture_output=True)
        end = time.time() + timeout
        while time.time() < end and not found:
            time.sleep(0.3)
            found = display_index()
    if not found:
        log("the DeskPad monitor did not appear; using the main display")
        return None
    idx, w, h = found
    log(f"extended display ready: Capture screen {idx}, {w}x{h}")
    if (w, h) != (1920, 1080):
        log("  tip: set 'DeskPad Display' to 1920x1080 in System Settings > Displays")
    return idx


def stop():
    """Quits DeskPad: the virtual monitor disappears and windows move back to the built-in displays."""
    if not running():
        return
    subprocess.run(["osascript", "-e", 'quit app "DeskPad"'], capture_output=True, timeout=10)
    end = time.time() + 5
    while time.time() < end and running():
        time.sleep(0.2)
    if running():
        subprocess.run(["pkill", "-x", "DeskPad"], capture_output=True)
    log("DeskPad quit; extended display removed")
