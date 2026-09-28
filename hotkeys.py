"""Hotkey strings: from a key press in a capture button, for display, and in
pynput's syntax - one place for both capture buttons (Settings, onboarding)
and the listeners in main.py.

Config form: "alt+r", "cmd+shift+space", "f9", "mouse:middle". Modifiers are
ctrl, alt (Option on a Mac), shift and cmd (Command on a Mac). "win" is what
the Windows capture stores for the Windows key; pynput treats it as <cmd>.

On macOS Qt reports the Command key as ControlModifier and the Control key as
MetaModifier. Capture undoes that swap, so "cmd" really means Command there.
"""
import contextlib
import logging
import re
import sys
import threading

logger = logging.getLogger("transcribe")

IS_MAC = sys.platform == "darwin"

MODIFIERS = ("ctrl", "alt", "shift", "cmd", "win", "super")
_MAC_ORDER = ("ctrl", "alt", "shift", "cmd")            # ⌃⌥⇧⌘, Apple's order
_MAC_SYMBOL = {"ctrl": "⌃", "alt": "⌥", "shift": "⇧", "cmd": "⌘", "win": "⌘", "super": "⌘"}
_MAC_KEY = {"space": "Space", "enter": "Return", "tab": "Tab", "esc": "Esc",
            "backspace": "Delete", "delete": "Fwd Delete", "insert": "Insert",
            "home": "Home", "end": "End", "page up": "Page Up", "page down": "Page Down",
            "up": "↑", "down": "↓", "left": "←", "right": "→"}
_WIN_CAPS = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win", "super": "Super"}
_MOUSE = {"middle": "Mouse Middle", "left": "Mouse Left", "right": "Mouse Right",
          "x1": "Mouse Back", "x2": "Mouse Forward"}
# Stored name -> pynput's <name> for keys that aren't a single character.
_PYNPUT_KEY = {"space": "space", "tab": "tab", "enter": "enter", "esc": "esc",
               "backspace": "backspace", "delete": "delete", "insert": "insert",
               "home": "home", "end": "end", "page up": "page_up", "page down": "page_down",
               "up": "up", "down": "down", "left": "left", "right": "right"}
_FKEY = re.compile(r"^f([1-9]|1[0-9]|20)$")


def parts(hotkey):
    return [p.strip().lower() for p in (hotkey or "").split("+") if p.strip()]


def is_function_key(name):
    return bool(_FKEY.match(name or ""))


def is_bare_typing_key(hotkey):
    """True for a hotkey that is just a typing or navigation key ("space",
    "r", "enter") - it would fire while the user types. Function keys and
    mouse buttons are fine on their own."""
    hk = (hotkey or "").strip().lower()
    if not hk or hk.startswith("mouse:"):
        return False
    ps = parts(hk)
    return not any(p in MODIFIERS for p in ps) and not all(is_function_key(p) for p in ps)


def combine(key_name, ctrl=False, alt=False, shift=False, meta=False, mac=IS_MAC):
    """(hotkey, problem) for a captured key: ``key_name`` as stored ("r",
    "space", "f9"), and Qt's modifier flags - on macOS ``ctrl`` is Command
    and ``meta`` is Control. ``problem`` is None or "needs_modifier"."""
    if mac:
        mods = [m for m, on in (("ctrl", meta), ("alt", alt), ("shift", shift), ("cmd", ctrl)) if on]
    else:
        mods = [m for m, on in (("ctrl", ctrl), ("alt", alt), ("shift", shift), ("win", meta)) if on]
    if not mods and not is_function_key(key_name):
        return None, "needs_modifier"
    return "+".join(mods + [key_name]), None


def from_key_event(event, mac=IS_MAC):
    """(hotkey, problem) for a key press in a capture button. ``problem`` is
    None, "modifier_only" (keep waiting), "unsupported" or "needs_modifier"."""
    from PySide6.QtCore import Qt
    key = event.key()
    if key in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta, Qt.Key_CapsLock):
        return None, "modifier_only"
    names = {
        Qt.Key_Space: "space", Qt.Key_Tab: "tab", Qt.Key_Enter: "enter",
        Qt.Key_Return: "enter", Qt.Key_Escape: "esc", Qt.Key_Backspace: "backspace",
        Qt.Key_Delete: "delete", Qt.Key_Insert: "insert", Qt.Key_Home: "home",
        Qt.Key_End: "end", Qt.Key_PageUp: "page up", Qt.Key_PageDown: "page down",
        Qt.Key_Up: "up", Qt.Key_Down: "down", Qt.Key_Left: "left", Qt.Key_Right: "right",
    }
    for i in range(12):
        names[getattr(Qt, f"Key_F{i + 1}")] = f"f{i + 1}"
    k = int(key.value) if hasattr(key, "value") else int(key)
    if key in names:
        name = names[key]
    elif 48 <= k <= 57 or 65 <= k <= 90:           # digits, letters
        name = chr(k).lower()
    else:
        return None, "unsupported"
    m = event.modifiers()
    return combine(name, ctrl=bool(m & Qt.ControlModifier), alt=bool(m & Qt.AltModifier),
                   shift=bool(m & Qt.ShiftModifier), meta=bool(m & Qt.MetaModifier), mac=mac)


def needs_modifier_message(mac=IS_MAC):
    if mac:
        return ("Use at least one modifier (⌘ Command, ⌥ Option, ⌃ Control or ⇧ Shift), "
                "a function key (F1-F12), or a mouse button.\n\nA single typing key can't "
                "be a hotkey because it would trigger while you type.")
    return ("Use at least one modifier (Ctrl, Alt, Shift, or Win), a Function key "
            "(F1-F12), or a mouse button.\n\nA single typing key can't be a hotkey "
            "because it would trigger while you type.")


def display(hotkey, mac=IS_MAC):
    """How a hotkey reads to the user: "Alt + R" on Windows, "⌥ R" on a Mac."""
    hk = (hotkey or "").strip()
    if hk.lower().startswith("mouse:"):
        return _MOUSE.get(hk.split(":", 1)[1].lower(), hk)
    if not mac:
        return " + ".join(_WIN_CAPS.get(p, p.upper() if len(p) == 1 else p.capitalize())
                          for p in hk.split("+"))
    ps = parts(hk)
    mods = {("cmd" if p in ("win", "super") else p) for p in ps if p in MODIFIERS}
    keys = [p for p in ps if p not in MODIFIERS]
    symbols = "".join(_MAC_SYMBOL[m] for m in _MAC_ORDER if m in mods)
    label = " + ".join(_MAC_KEY.get(k, k.upper() if len(k) == 1 else k.capitalize()) for k in keys)
    return f"{symbols} {label}".strip()


def to_pynput(hotkey):
    """"cmd+shift+space" -> "<cmd>+<shift>+<space>" for pynput.GlobalHotKeys,
    which only accepts single characters and <named> keys."""
    out = []
    for p in parts(hotkey):
        if p in ("ctrl", "control"):
            out.append("<ctrl>")
        elif p in ("alt", "option"):
            out.append("<alt>")
        elif p == "shift":
            out.append("<shift>")
        elif p in ("cmd", "command", "win", "super", "windows"):
            out.append("<cmd>")
        elif len(p) == 1:
            out.append(p)
        elif p in _PYNPUT_KEY:
            out.append(f"<{_PYNPUT_KEY[p]}>")
        else:
            out.append(f"<{p.replace(' ', '_')}>")       # f1.. and the like
    return "+".join(out)


_layout = None
_layout_lock = threading.Lock()


def prepare_pynput_listeners():
    """macOS: pynput's keyboard listener reads the keyboard layout (HIToolbox
    TIS calls) on its own background thread when it starts. Recent macOS only
    allows those calls on the main thread and can kill the whole app when
    they happen elsewhere - most likely while the user is typing in the app,
    e.g. picking a new hotkey. Read the layout here, on the main thread, and
    hand listener threads that copy. Call it before starting listeners; each
    call refreshes the copy (the user may have switched layouts). No-op off
    macOS."""
    global _layout
    if not IS_MAC or threading.current_thread() is not threading.main_thread():
        return False
    try:
        from pynput._util import darwin as util
        from pynput.keyboard import _darwin as kb
        with util.keycode_context() as ctx:
            with _layout_lock:
                _layout = ctx
        if not getattr(kb, "_transcribe_patched", False):
            real = util.keycode_context

            @contextlib.contextmanager
            def _main_thread_layout():
                if threading.current_thread() is threading.main_thread() or _layout is None:
                    with real() as fresh:
                        yield fresh
                else:
                    with _layout_lock:
                        cached = _layout
                    yield cached

            kb.keycode_context = _main_thread_layout
            kb._transcribe_patched = True
        return True
    except Exception as e:
        logger.warning("pynput macOS layout prep failed: %s", e)
        return False
