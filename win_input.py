"""Thin ctypes wrappers for the Win32 calls the auto-typer needs.

Keystrokes go through SendInput with KEYEVENTF_UNICODE, which delivers any
UTF-16 code unit regardless of keyboard layout -- no clipboard involved.
Enter, Tab and Backspace are sent as real virtual keys so the editor's own key
bindings handle them. The only clipboard call here is the read-only
GetClipboardSequenceNumber, used to prove the clipboard was never changed.
"""

from __future__ import annotations

import ctypes
import struct
import sys
from ctypes import wintypes

if sys.platform != "win32":
    raise ImportError("win_input needs Windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)
winmm = ctypes.WinDLL("winmm")

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MAPVK_VK_TO_VSC = 0
GA_ROOT = 2

VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
_MODIFIERS = (("Shift", 0x10), ("Ctrl", 0x11), ("Alt", 0x12), ("Windows", 0x5B), ("Windows", 0x5C))
_VIRTUAL_KEYS = {"\n": VK_RETURN, "\t": VK_TAB}

INJECTED_TAG = 0x41545950  # "ATYP" in dwExtraInfo marks our synthetic events

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    # MOUSEINPUT is the largest member; it must be present for sizeof(INPUT) to be right.
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
user32.GetAncestor.restype = wintypes.HWND
user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    event = INPUT(type=INPUT_KEYBOARD)
    event.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=INJECTED_TAG)
    return event


def _virtual_key(vk: int) -> list[INPUT]:
    scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
    return [_key(vk, scan, 0), _key(vk, scan, KEYEVENTF_KEYUP)]


def _unicode(ch: str) -> list[INPUT]:
    """Down/up pairs for each UTF-16 code unit (two units for emoji outside the BMP)."""
    events = []
    for (unit,) in struct.iter_unpack("<H", ch.encode("utf-16-le", "surrogatepass")):
        events.append(_key(0, unit, KEYEVENTF_UNICODE))
        events.append(_key(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    return events


def _send(events: list[INPUT]) -> None:
    array = (INPUT * len(events))(*events)
    sent = user32.SendInput(len(events), array, ctypes.sizeof(INPUT))
    if sent != len(events):
        err = ctypes.get_last_error()
        raise OSError(err, f"SendInput inserted {sent} of {len(events)} events: {ctypes.FormatError(err).strip()}")


def type_text(text: str) -> None:
    """Type `text` (normally one grapheme cluster) in a single, uninterruptible SendInput call."""
    events: list[INPUT] = []
    for ch in text:
        vk = _VIRTUAL_KEYS.get(ch)
        events += _virtual_key(vk) if vk else _unicode(ch)
    _send(events)


def press_backspace() -> None:
    _send(_virtual_key(VK_BACK))


def press_escape() -> None:
    """Used by the self-test to simulate the user pressing Escape."""
    _send(_virtual_key(VK_ESCAPE))


def foreground_window() -> int:
    return user32.GetForegroundWindow() or 0


def toplevel_window(hwnd: int) -> int:
    return user32.GetAncestor(hwnd, GA_ROOT) or 0


def key_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def held_modifiers() -> list[str]:
    """Physically held modifier keys, which would turn a typed letter into a shortcut."""
    names: list[str] = []
    for name, vk in _MODIFIERS:
        if key_down(vk) and name not in names:
            names.append(name)
    return names


def clipboard_sequence_number() -> int:
    """Read-only counter that Windows bumps whenever the clipboard contents change."""
    return user32.GetClipboardSequenceNumber()


def begin_timer_resolution() -> bool:
    """Ask for 1 ms timer ticks so short delays are accurate; undo with end_timer_resolution."""
    try:
        return winmm.timeBeginPeriod(1) == 0
    except (AttributeError, OSError):
        return False


def end_timer_resolution(active: bool) -> None:
    if active:
        winmm.timeEndPeriod(1)


def enable_dpi_awareness() -> None:
    """Crisp text on scaled displays. Must run before the Tk root window is created."""
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(1)  # system-DPI aware, like IDLE
    except (AttributeError, OSError):
        try:
            user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass
