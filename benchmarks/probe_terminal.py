"""
Can oswright drive a terminal and a text-mode interface?

Everything measured so far has been a graphical application: XAML buttons, a
Win32 list view, web content, an Electron sidebar. A terminal is a different
class of surface, and a TUI running inside one is different again -- no
accessibility objects for the content, no DOM, just glyphs on a grid that the
application repaints as it pleases.

This probes the question in stages rather than asserting an answer, and stops
before doing anything irreversible.

Safety, which is the whole reason this file is careful:

  oswright's keyboard tools send input to whatever currently has focus. They do
  not, and cannot, target a window. So every keystroke here is preceded by a
  check that the intended window is genuinely in the foreground -- because if
  focus has moved, the alternative is typing into whatever the machine's owner
  happens to be doing.

  Windows also refuses SetForegroundWindow to a process that does not hold the
  foreground lock, which means "focus the window then type" can silently fail
  at the first half and still execute the second. That is not hypothetical: it
  happened on the first run of this probe.

Run:  python benchmarks/probe_terminal.py
"""

import ctypes
import subprocess
import sys
import time

from oswright.capture import ScreenCapture
from oswright.detect import OCREngine
from oswright.window import focus_window, list_windows

#: The session driving this probe. Nothing here may ever type into it.
FORBIDDEN = ("Deep Dive", "GitHub Copilot")


def foreground_title() -> str:
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def foreground_handle() -> int:
    return ctypes.windll.user32.GetForegroundWindow()


def safe_to_type(handle: int) -> tuple[bool, str]:
    """
    Is it safe to send keystrokes right now?

    True only when the intended window is the foreground window. Anything else
    -- focus lost, a dialog stole it, the foreground lock refused us -- means
    the keystrokes would go somewhere they were never meant to.
    """
    current = foreground_handle()
    title = foreground_title()
    if current != handle:
        return False, f"foreground is {title!r}, not the target window"
    if any(f in title for f in FORBIDDEN):
        return False, f"refusing to type into {title!r}"
    return True, title


def read_window(window, save=None):
    """OCR just the region a window occupies."""
    cap = ScreenCapture()
    img = cap.screenshot()
    cap.close()
    box = (
        max(0, window.left), max(0, window.top),
        min(img.width, window.left + window.width),
        min(img.height, window.top + window.height),
    )
    crop = img.crop(box)
    if save:
        crop.save(save)
    return [(m.text or "") for m in OCREngine().read_all(crop)], crop


def open_terminal():
    """A fresh terminal, tracked by handle so nothing else can be mistaken for it."""
    before = {w.handle for w in list_windows()}
    subprocess.Popen(
        ["wt.exe", "-w", "-1", "powershell", "-NoLogo", "-NoExit"], shell=False
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        for w in list_windows():
            if w.handle in before:
                continue
            if any(f in (w.title or "") for f in FORBIDDEN):
                continue
            if "PowerShell" in (w.title or "") or "powershell" in (w.title or ""):
                time.sleep(1.5)
                return w
        time.sleep(0.4)
    return None


def main():
    print("Stage 1: open a terminal")
    term = open_terminal()
    if term is None:
        print("  FAILED: no terminal window appeared")
        return 1
    print(f"  opened: {term.title!r} at {term.left},{term.top} "
          f"{term.width}x{term.height}")

    print("\nStage 2: can it take the foreground?")
    focus_window(handle=term.handle)
    time.sleep(1.2)
    ok, why = safe_to_type(term.handle)
    print(f"  {'yes' if ok else 'NO -- ' + why}")

    print("\nStage 3: can OCR read what the terminal shows?")
    texts, _ = read_window(term, save=r"C:\Users\Shree\term_stage3.png")
    print(f"  {len(texts)} strings read from the window region")
    print(f"  sample: {[t for t in texts if len(t) > 3][:10]}")

    print("\nStage 4: can it read text the terminal printed?")
    if ok:
        from oswright.input import Keyboard

        Keyboard.type_text("Write-Host 'OSWRIGHT_PROBE_TOKEN_7742'\n", delay=0.01)
        time.sleep(1.5)
        texts, _ = read_window(term, save=r"C:\Users\Shree\term_stage4.png")
        hit = [t for t in texts if "7742" in t or "PROBE" in t.upper()]
        print(f"  typed a marker; OCR {'FOUND it: ' + repr(hit[:2]) if hit else 'MISSED it'}")
    else:
        print("  skipped: not safe to type")

    print("\nStage 5: what does the accessibility tree offer for a terminal?")
    try:
        import uiautomation as auto

        root = auto.ControlFromHandle(term.handle)
        n = 0
        names = []

        def walk(c, d=0):
            nonlocal n
            if d > 6 or n > 200:
                return
            for ch in c.GetChildren():
                n += 1
                if ch.Name:
                    names.append((ch.ControlTypeName, ch.Name[:48]))
                walk(ch, d + 1)

        walk(root)
        print(f"  {n} elements")
        for t, nm in names[:8]:
            print(f"    {t:<20} {nm!r}")

        # A terminal that supports screen readers exposes its buffer as text.
        try:
            pattern = root.GetTextPattern()
            doc = pattern.DocumentRange.GetText(400)
            print(f"  TextPattern: available, {len(doc)} chars")
            print(f"    starts: {doc.strip()[:120]!r}")
            print(f"    contains the marker: {'7742' in doc}")
        except Exception as e:
            print(f"  TextPattern: unavailable ({type(e).__name__})")
    except Exception as e:
        print(f"  UIA failed: {type(e).__name__}: {e}")

    print("\nTerminal left open for inspection; close it manually.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
