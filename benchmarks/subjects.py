"""
Applications a benchmark task can be run against.

Everything measured so far ran against Calculator: stateless, XAML, and fully
exposed to UI Automation. It is the easiest surface Windows has, and a claim
resting on it alone is fragile. This module adds the hard cases -- a real Win32
list view, a browser, and an Electron app -- behind one interface, so a task can
be written once and run anywhere.

Two rules hold for every subject here.

**Ground truth must come from a channel the agent is not using.** A task is
graded on the application's own state, never on OCR output, because OCR cannot
be both the thing under test and the thing that grades it. Calculator and
Explorer are graded through UI Automation. Chrome and VS Code are graded through
the *window title*, which the OS reports directly -- that is what makes it
possible to grade web and Electron content without trusting the perception layer
to mark its own work.

**A benchmark must not be able to lose anyone's work.** Every subject records
`safe_because`, and anything that could touch a real document is excluded.
Notepad was rejected outright after launching it restored a document with
unsaved changes. Chrome and VS Code run against throwaway profile directories,
so they cannot see real tabs, logins, extensions or unsaved buffers.
"""

import ctypes
import json
import os
import shutil
import subprocess
import tempfile
import time
from typing import Optional

import uiautomation as auto

from oswright.window import close_window, focus_window, list_windows

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
VSCODE_CANDIDATES = [
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
    r"C:\Program Files\Microsoft VS Code\Code.exe",
]


def _first_existing(paths):
    for path in paths:
        if path and os.path.exists(path):
            return path
    return None


def current_title(handle) -> str:
    """
    Read a window's title straight from the OS.

    Deliberately not cached and deliberately not perception: for Chrome and
    VS Code this *is* the ground truth, so it has to come from a source the
    thing under test cannot influence.
    """
    length = ctypes.windll.user32.GetWindowTextLengthW(handle)
    buffer = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(handle, buffer, length + 1)
    return buffer.value


def wait_for_window(before, matches, timeout=15.0, settle=0.8):
    """
    Wait for a new top-level window that satisfies `matches`.

    `before` is the set of handles that existed prior to launching, so a window
    the user already had open can never be mistaken for the one under test --
    which also means the benchmark can never drive somebody else's document.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for window in list_windows():
            if window.handle in before:
                continue
            if matches(window.title or ""):
                # Acting on an unfocused window is unreliable. This is a real
                # step an agent has to take, not a benchmark workaround.
                focus_window(handle=window.handle)
                time.sleep(settle)
                return window
        time.sleep(0.25)
    return None


class Subject:
    """
    One application under test.

    Subclasses provide `launch`, `ground_truth` and the class attributes below.
    `cleanup` is inherited unless a subject needs to release more than a window.
    """

    name = "subject"
    safe_because = "unspecified"

    #: Seconds to wait after each click for the application to react. Slower
    #: applications need longer, and getting this wrong produces dropped clicks
    #: that look exactly like perception failures.
    click_settle_s = 0.35

    def available(self) -> bool:
        """Whether this subject can run on this machine at all."""
        return True

    def launch(self):
        raise NotImplementedError

    def target_label(self, window) -> Optional[str]:
        """
        The on-screen label a task should ask the agent to find.

        Usually a fixed string, but some applications render a label that
        depends on machine settings rather than on what the benchmark wrote to
        disk, so this is resolved against the live window.
        """
        return getattr(self, "TARGET", None)

    def window_hint(self, window) -> str:
        """
        A title fragment that keeps matching this window for the whole task.

        It cannot simply be the launch title: Chrome and VS Code report their
        result *by changing the title*, so a tool call filtering on the original
        string would stop finding the window halfway through the task.
        """
        return getattr(self, "WINDOW_HINT", None) or (window.title or "")

    def ground_truth(self, window) -> Optional[str]:
        """The application's own account of its state. Never OCR."""
        raise NotImplementedError

    def cleanup(self, window) -> None:
        if window is not None:
            try:
                close_window(handle=window.handle)
                time.sleep(0.4)
            except Exception:
                pass


# --------------------------------------------------------------------------
# Calculator -- the easy case, kept as the control
# --------------------------------------------------------------------------

class Calculator(Subject):
    name = "Calculator"
    safe_because = "stateless; opening and closing it cannot lose anyone's work"
    title = "Calculator"
    WINDOW_HINT = "Calculator"

    def launch(self):
        before = {w.handle for w in list_windows()}
        subprocess.Popen(["calc"], shell=False)
        return wait_for_window(before, lambda t: t.strip() == self.title)

    def ground_truth(self, window):
        """Read the result from Calculator itself, through UI Automation."""
        try:
            root = auto.ControlFromHandle(window.handle)
            result = root.Control(searchDepth=12, AutomationId="CalculatorResults")
            if result.Exists(maxSearchSeconds=2):
                return (result.Name or "").replace("Display is", "").strip()
        except Exception:
            pass
        return None


# --------------------------------------------------------------------------
# File Explorer -- a real Win32 list view
# --------------------------------------------------------------------------

class Explorer(Subject):
    """
    A folder of purpose-created files, opened in Explorer.

    Tasks here only ever *select* an item. Nothing is opened, renamed or
    deleted, and the folder is created by this class in the temp directory, so
    there is no path by which a real file can be touched.
    """

    name = "File Explorer"
    safe_because = (
        "navigates only a folder this benchmark created under the temp "
        "directory; tasks select items and never open, rename or delete them"
    )
    click_settle_s = 0.45

    #: Distinctive enough that a perception miss cannot be a lucky substring
    #: match, and ordinary enough to be a realistic filename.
    FILES = ["quarterly_report.txt", "meeting_notes.txt", "budget_draft.txt"]

    #: Explorer hides known extensions by default, so the label on screen is
    #: `meeting_notes`, not `meeting_notes.txt` -- and whether it does depends
    #: on a per-machine setting. The stem is what is stable; the displayed
    #: label is resolved from the live window by `target_label`.
    TARGET_STEM = "meeting_notes"

    def __init__(self):
        self._folder = None

    def available(self) -> bool:
        return os.path.exists(r"C:\Windows\explorer.exe")

    def _make_folder(self):
        self._folder = tempfile.mkdtemp(prefix="oswright_bench_")
        for name in self.FILES:
            with open(os.path.join(self._folder, name), "w", encoding="utf-8") as fh:
                fh.write("benchmark fixture; safe to delete\n")
        return self._folder

    def launch(self):
        folder = self._make_folder()
        leaf = os.path.basename(folder)
        before = {w.handle for w in list_windows()}
        subprocess.Popen(["explorer.exe", folder], shell=False)
        # Explorer reuses one process, so the window cannot be tracked through
        # the Popen handle; it has to be found by title.
        return wait_for_window(before, lambda t: leaf in t, timeout=15.0, settle=1.2)

    @staticmethod
    def _items_view(window):
        """
        Find the file list, specifically.

        A search by control type alone returns the *tab* strip, which sits
        shallower in the tree -- so the file list has to be asked for by name.
        """
        try:
            root = auto.ControlFromHandle(window.handle)
            view = root.ListControl(searchDepth=16, Name="Items View")
            if view.Exists(maxSearchSeconds=3):
                return view
        except Exception:
            pass
        return None

    def _items(self, window):
        view = self._items_view(window)
        if view is None:
            return []
        # The header row is a child of the list but is not a file.
        return [c for c in view.GetChildren() if c.Name and c.Name != "Header"]

    def target_label(self, window):
        """Whatever Explorer actually shows for the target file on this machine."""
        for item in self._items(window):
            if item.Name.startswith(self.TARGET_STEM):
                return item.Name
        return None

    def ground_truth(self, window):
        """Which item Explorer itself reports as selected."""
        view = self._items_view(window)
        if view is None:
            return None
        for item in self._items(window):
            try:
                if item.GetSelectionItemPattern().IsSelected:
                    return item.Name
            except Exception:
                continue
        return ""  # the view exists and nothing is selected

    def cleanup(self, window):
        super().cleanup(window)
        if self._folder and os.path.isdir(self._folder):
            # Only ever a directory this class created, under the temp dir.
            shutil.rmtree(self._folder, ignore_errors=True)
        self._folder = None


# --------------------------------------------------------------------------
# Chrome -- web content, where OCR and accessibility disagree most
# --------------------------------------------------------------------------

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>oswright bench ready</title>
<style>
 body { font-family: Segoe UI, sans-serif; background: #fff; margin: 40px; }
 button { font-size: 20px; padding: 18px 30px; margin: 12px;
          background: #f3f3f3; border: 1px solid #999; cursor: pointer; }
 h1 { font-size: 26px; }
</style></head>
<body>
<h1>Perception benchmark fixture</h1>
<p>A local file. No network, no accounts, nothing to lose.</p>
<div>
  <button onclick="document.title='oswright bench picked Marigold'">Marigold</button>
  <button onclick="document.title='oswright bench picked Cormorant'">Cormorant</button>
  <button onclick="document.title='oswright bench picked Zeppelin'">Zeppelin</button>
</div>
</body></html>
"""


class Chrome(Subject):
    """
    A local page in a throwaway Chrome profile.

    The page reports what was clicked by changing `document.title`, so the
    result arrives through the window title -- a channel the perception layer
    has no hand in. That is what makes it possible to grade web content without
    grading OCR with OCR.

    `--user-data-dir` pointing at a temp directory is what makes this safe: the
    browser starts with no history, no logins, no extensions and no open tabs
    belonging to anyone.
    """

    name = "Chrome"
    safe_because = (
        "throwaway --user-data-dir, so no real profile, tabs, history or "
        "logins are reachable; the page is a local file with no network access"
    )
    click_settle_s = 0.5

    #: Overridden by the multi-step variant below.
    PAGE = PAGE

    #: Words chosen to be unambiguous to OCR and absent from browser chrome.
    TARGET = "Cormorant"

    #: Every page title this fixture sets starts with this, so the window stays
    #: findable after the click has changed the title.
    WINDOW_HINT = "oswright bench"

    def __init__(self):
        self._profile = None
        self._page = None
        self._proc = None

    def available(self) -> bool:
        return _first_existing(CHROME_CANDIDATES) is not None

    def launch(self):
        self._profile = tempfile.mkdtemp(prefix="oswright_chrome_")
        handle, self._page = tempfile.mkstemp(suffix=".html", prefix="oswright_page_")
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(self.PAGE)

        before = {w.handle for w in list_windows()}
        self._proc = subprocess.Popen([
            _first_existing(CHROME_CANDIDATES),
            f"--user-data-dir={self._profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--new-window",
            f"file:///{self._page.replace(os.sep, '/')}",
        ], shell=False)
        return wait_for_window(
            before, lambda t: "oswright bench" in t, timeout=30.0, settle=1.5
        )

    def ground_truth(self, window):
        """What the page says was clicked, via the window title."""
        return current_title(window.handle)

    def cleanup(self, window):
        super().cleanup(window)
        if self._proc is not None:
            try:
                # Only the process this class started, addressed by its own
                # handle -- never by name.
                self._proc.terminate()
                self._proc.wait(timeout=8)
            except Exception:
                pass
            self._proc = None
        time.sleep(0.4)
        for path in (self._page, self._profile):
            if not path:
                continue
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                elif os.path.exists(path):
                    os.unlink(path)
            except Exception:
                pass
        self._page = self._profile = None


# --------------------------------------------------------------------------
# Chrome, two steps -- where a cached screen stops being true
# --------------------------------------------------------------------------

TWO_STEP_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>oswright bench ready</title>
<style>
 body { font-family: Segoe UI, sans-serif; background: #fff; margin: 40px; }
 button { font-size: 20px; padding: 18px 30px; margin: 12px;
          background: #f3f3f3; border: 1px solid #999; cursor: pointer; }
 h1 { font-size: 26px; }
 #pad { height: 260px; }
</style></head>
<body>
<h1>Perception benchmark fixture, two steps</h1>
<p>A local file. No network, no accounts, nothing to lose.</p>
<div id="stage"></div>
<script>
// Step two deliberately renders in a different place and with different words,
// so coordinates read before the first click cannot be reused for the second.
// That is the point of this fixture: it is the cheapest possible reproduction
// of an interface that moves while an agent is working on it.
function stageTwo() {
  document.title = 'oswright bench stage two';
  document.getElementById('stage').innerHTML =
    '<div id="pad"></div>' +
    '<button onclick="finish()">Kestrel</button>' +
    '<button>Petrichor</button>' +
    '<button>Zamboni</button>';
}
function finish() {
  document.title = 'oswright bench picked Cormorant then Kestrel';
}
document.getElementById('stage').innerHTML =
  '<button>Marigold</button>' +
  '<button onclick="stageTwo()">Cormorant</button>' +
  '<button>Zeppelin</button>';
</script>
</body></html>
"""


class ChromeTwoStep(Chrome):
    """
    The same browser, with an interface that changes under the agent.

    Every other task here is short enough that a tool can read the screen once
    and reuse those coordinates for every click. That is a real advantage and it
    is also a special case, so measuring only such tasks quietly favours designs
    that cache aggressively -- including the cheap Windows-MCP configuration
    this benchmark reports.

    Here the first click replaces the controls and pushes them 260 px down the
    page. Coordinates read before it are stale afterwards, and a tool that
    reuses them clicks blank space. Nothing about that is a trick: it is what
    every wizard, dialog and navigation does.
    """

    name = "Chrome (two steps)"
    PAGE = TWO_STEP_PAGE
    TARGETS = ["Cormorant", "Kestrel"]

    def succeeded(self, window) -> bool:
        return "picked Cormorant then Kestrel" in (self.ground_truth(window) or "")


# --------------------------------------------------------------------------
# VS Code -- the Electron blind spot
# --------------------------------------------------------------------------

class VSCode(Subject):
    """
    A throwaway VS Code window opened on a folder of fixture files.

    This is the case that decides the argument. An Electron application renders
    its own UI, so the sidebar filenames a human can plainly read are largely
    absent from the accessibility tree -- an earlier probe found an entire IDE
    exposing 18 elements. An accessibility-only agent is blind here; a pixel
    reader is not.

    Graded through the window title, which VS Code sets to the open file. That
    keeps the grader outside both perception paths.

    `--user-data-dir` and `--extensions-dir` on temp directories guarantee a
    clean instance: no real workspace, no extensions, and crucially no restored
    editors with unsaved changes.

    Requires that no other VS Code is already running. When one is, the launcher
    hands the arguments to the existing instance and exits without opening a
    window, and the task reports "not run" rather than a failure -- an
    application that never opened has not measured perception either way.
    """

    name = "VS Code"
    safe_because = (
        "throwaway --user-data-dir and --extensions-dir, opened on a folder "
        "this benchmark created, so no real workspace or unsaved buffer is "
        "reachable"
    )
    click_settle_s = 0.8

    FILES = ["alpha_notes.txt", "bravo_notes.txt", "charlie_notes.txt"]
    TARGET = "bravo_notes.txt"
    WINDOW_HINT = "Visual Studio Code"

    #: A fresh profile otherwise opens on a Welcome tab offering to sign in to
    #: GitHub Copilot, which covers the file sidebar entirely -- the first run
    #: of this subject failed for that reason and not for any perception
    #: reason. Seeding settings is what makes the window show actual files.
    SETTINGS = {
        "workbench.startupEditor": "none",
        "workbench.tips.enabled": False,
        "workbench.welcomePage.walkthroughs.openOnInstall": False,
        "window.restoreWindows": "none",
        "telemetry.telemetryLevel": "off",
        "update.mode": "none",
        "chat.commandCenter.enabled": False,
        "explorer.compactFolders": False,
        "workbench.colorTheme": "Default Dark Modern",
    }

    def __init__(self):
        self._folder = None
        self._data = None
        self._ext = None
        self._proc = None

    def available(self) -> bool:
        return _first_existing(VSCODE_CANDIDATES) is not None

    def _seed_settings(self):
        user_dir = os.path.join(self._data, "User")
        os.makedirs(user_dir, exist_ok=True)
        with open(os.path.join(user_dir, "settings.json"), "w", encoding="utf-8") as fh:
            json.dump(self.SETTINGS, fh, indent=2)

    def launch(self):
        self._folder = tempfile.mkdtemp(prefix="oswright_code_")
        self._data = tempfile.mkdtemp(prefix="oswright_codedata_")
        self._ext = tempfile.mkdtemp(prefix="oswright_codeext_")
        for name in self.FILES:
            with open(os.path.join(self._folder, name), "w", encoding="utf-8") as fh:
                fh.write("benchmark fixture; safe to delete\n")
        self._seed_settings()

        before = {w.handle for w in list_windows()}
        self._proc = subprocess.Popen([
            _first_existing(VSCODE_CANDIDATES),
            "--user-data-dir", self._data,
            "--extensions-dir", self._ext,
            "--disable-extensions",
            # Without this, VS Code opens a modal asking whether the folder's
            # authors are trusted, which blocks every task behind it.
            "--disable-workspace-trust",
            "--skip-release-notes",
            "--new-window",
            self._folder,
        ], shell=False)
        # Electron start-up is slow, and slower still on a cold profile.
        window = wait_for_window(
            before,
            lambda t: "Visual Studio Code" in t,
            timeout=45.0,
            settle=4.0,
        )
        if window is not None:
            self._show_sidebar(window)
        return window

    @staticmethod
    def _show_sidebar(window):
        """
        Get the window into the state the task is actually about.

        Modern VS Code force-shows a "Sign in to use GitHub Copilot" modal on a
        fresh profile, and no settings key found so far suppresses it. It covers
        the file list completely, which is why the first runs of this subject
        failed for a reason that had nothing to do with perception.

        The modal appears at an unpredictable point during start-up, so a single
        Escape races it -- measured, one trial in three dismissed nothing because
        the modal had not appeared yet. Escape is therefore sent repeatedly
        across the window in which it can appear.

        Deliberately not polled with OCR. Using the perception layer to decide
        when its own test is ready would let a perception failure present itself
        as a setup delay, and the benchmark would be grading itself.

        This is setup, in the same category as launching the application. What
        is measured afterwards is whether the agent can find a filename that is
        genuinely on screen.
        """
        try:
            focus_window(handle=window.handle)
            time.sleep(0.5)
            for _ in range(7):
                auto.SendKeys("{Esc}", waitTime=0.1)
                time.sleep(0.7)
            auto.SendKeys("{Ctrl}{Shift}e", waitTime=0.2)  # focus the file tree
            time.sleep(1.0)
        except Exception:
            pass

    def ground_truth(self, window):
        """Which file VS Code reports as open, via the window title."""
        return current_title(window.handle)

    def cleanup(self, window):
        super().cleanup(window)
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=10)
            except Exception:
                pass
            self._proc = None
        time.sleep(0.6)
        for path in (self._folder, self._data, self._ext):
            if path and os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        self._folder = self._data = self._ext = None


# --------------------------------------------------------------------------
# Excluded on purpose
# --------------------------------------------------------------------------
#
# Notepad. Launching it restored a document belonging to the machine's owner,
# with unsaved changes. A benchmark has no business anywhere near that, and the
# exclusion is recorded here rather than merely remembered.
