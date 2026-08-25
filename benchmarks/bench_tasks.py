"""
Does any of this actually help an agent complete tasks?

Everything else in `benchmarks/` measures perception *cost*. Cost is a proxy.
The metric that matters for a GUI agent is whether it finishes the job, and a
cheaper perception path that quietly degraded accuracy would be worse than no
optimisation at all. Nothing measured that until this file existed.

Each task drives the real MCP tool surface end to end and is verified against
the application's own state -- through UI Automation, or through the window
title, which the OS reports directly. Never against OCR: checking OCR with OCR
would only prove it agrees with itself. See `subjects.py`.

The corpus deliberately spans surfaces of increasing difficulty, because
Calculator alone is the easiest case Windows has and a claim resting on it is
fragile:

    Calculator      XAML, fully exposed to accessibility
    File Explorer   native Win32 list view
    Chrome          web content, where OCR and accessibility disagree most
    VS Code         Electron -- an entire IDE exposing ~18 elements

Every task runs several times per configuration. A single sample cannot
distinguish "this configuration is worse" from "that click did not register",
and an earlier version of this file reported both as the same thing. Even
three samples proved too few: one sweep reported 24/36 and the next 36/36
with no change to the code under test. Raise the count when a result is
load-bearing.

Safety is a design constraint, not an afterthought. Subjects are stateless or
run against throwaway profiles and purpose-created folders, so no real document,
tab, login or unsaved buffer is reachable. Notepad was rejected outright after
launching it restored a document with unsaved changes. Every task closes what it
opened.

Run:  python benchmarks/bench_tasks.py
      OSWRIGHT_BENCH_REPEATS=5 python benchmarks/bench_tasks.py
      OSWRIGHT_BENCH_SUBJECTS=Calculator,Chrome python benchmarks/bench_tasks.py
"""

import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import subjects as S  # noqa: E402

import oswright.mcp_server as server  # noqa: E402

IMAGE_TOKENS_PER_PIXEL = 1 / 750

REPEATS = int(os.environ.get("OSWRIGHT_BENCH_REPEATS", "3"))

#: Restrict the corpus, e.g. while iterating on one subject.
ONLY = [
    s.strip()
    for s in os.environ.get("OSWRIGHT_BENCH_SUBJECTS", "").split(",")
    if s.strip()
]


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------

@dataclass
class Meter:
    """Counts what each tool call costs the agent."""

    steps: int = 0
    tokens: int = 0
    rungs: list = field(default_factory=list)

    def record(self, result):
        """Count a tool call and return its result unchanged."""
        self.steps += 1
        parts = result if isinstance(result, list) else [result]
        for part in parts:
            if isinstance(part, str):
                self.tokens += max(1, len(part) // 4)
                try:
                    payload = json.loads(part)
                except (ValueError, TypeError):
                    continue
                if isinstance(payload, dict) and "rung" in payload:
                    self.rungs.append(payload["rung"])
            elif getattr(part, "data", None):
                # Roughly what a vision model bills for a 1920x1080 frame.
                self.tokens += int(1920 * 1080 * IMAGE_TOKENS_PER_PIXEL)
        return result


@dataclass
class TaskResult:
    task: str
    subject: str
    config: str
    success: bool
    steps: int
    seconds: float
    tokens: int
    detail: str = ""


@dataclass
class Task:
    """A task, the application it runs against, and what it exercises."""

    name: str
    subject_factory: type
    body: object
    exercises: str = ""


def run_task(task, meter):
    """
    Launch, run, and always clean up.

    Cleanup lives here rather than in each task body so that a task cannot
    forget it, and so an exception mid-task still closes the window.
    """
    subject = task.subject_factory()
    if not subject.available():
        return None, f"{subject.name} is not installed on this machine"

    window = subject.launch()
    if window is None:
        return False, f"{subject.name} did not open"
    try:
        return task.body(meter, subject, window)
    finally:
        subject.cleanup(window)


def click_by_text(meter, subject, window, label):
    """
    Drive one click through the resolution cascade and report where it landed.

    Returns (ok, note). The note records the rung and coordinate, which turns
    "the answer was wrong" into "rung 0 returned a coordinate outside the
    window" -- a diagnosis rather than an observation.
    """
    out = meter.record(server.click_element(
        text=label, window_title=subject.window_hint(window)
    ))
    payload = json.loads(out[0])
    if "error" in payload:
        return False, f"could not click {label!r}: {payload['error'][:60]}"
    spot = payload.get("clicked") or {}
    time.sleep(subject.click_settle_s)
    return True, f"{label}@r{payload.get('rung')}({spot.get('x')},{spot.get('y')})"


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------

def arithmetic_by_label(meter, subject, window):
    """
    Compute 7 x 8 by finding buttons through the resolution cascade.

    A wrong element or a stale coordinate shows up here as a wrong answer
    rather than as a slower benchmark, which is the point.
    """
    trace = []
    for label in ("Seven", "Multiply by", "Eight", "Equals"):
        ok, note = click_by_text(meter, subject, window, label)
        if not ok:
            return False, note
        trace.append(note)

    shown = subject.ground_truth(window)
    if shown == "56":
        return True, ""
    return False, (
        f"display showed {shown!r}, expected '56'; "
        f"window={window.left},{window.top} {window.width}x{window.height}; "
        + " ".join(trace)
    )


def arithmetic_by_accessibility(meter, subject, window):
    """The same sum driven straight through the accessibility tree, as a control."""
    for aid in ("num7Button", "multiplyButton", "num8Button", "equalButton"):
        meter.record(server.click_ui_element(
            automation_id=aid, window_title=subject.window_hint(window)
        ))
        time.sleep(subject.click_settle_s)

    shown = subject.ground_truth(window)
    return shown == "56", f"display showed {shown!r}, expected '56'"


def read_result_back(meter, subject, window):
    """Add two numbers, then read the answer back off the screen."""
    for aid in ("num4Button", "plusButton", "num5Button", "equalButton"):
        meter.record(server.click_ui_element(
            automation_id=aid, window_title=subject.window_hint(window)
        ))
        time.sleep(subject.click_settle_s)

    truth = subject.ground_truth(window)
    if truth != "9":
        return False, f"Calculator itself shows {truth!r}; the task setup failed"

    # Find the answer through perception, which is what an agent must do when
    # an application does not expose its state.
    found = json.loads(meter.record(server.find_element(text="9")))
    return bool(found.get("found")), (
        f"perception {'found' if found.get('found') else 'missed'} the result"
    )


def select_file_in_explorer(meter, subject, window):
    """
    Find a file by the name on screen and select it.

    A real Win32 list view, and a label the benchmark cannot hardcode: Explorer
    hides known extensions depending on a per-machine setting, so the target is
    resolved from the live window before the agent is asked to find it.

    The task only ever selects. Nothing is opened, renamed or deleted.
    """
    label = subject.target_label(window)
    if not label:
        return False, "the fixture file is not listed; setup failed"

    ok, note = click_by_text(meter, subject, window, label)
    if not ok:
        return False, note

    selected = subject.ground_truth(window)
    if selected == label:
        return True, ""
    return False, f"Explorer reports {selected!r} selected, expected {label!r}; {note}"


def click_button_in_browser(meter, subject, window):
    """
    Find a word rendered by a web page and click it.

    Web content is where OCR and accessibility disagree most. The page reports
    the outcome by changing its own title, so the grade arrives through the
    window title rather than through anything the perception layer produced.
    """
    ok, note = click_by_text(meter, subject, window, subject.TARGET)
    if not ok:
        return False, note

    title = subject.ground_truth(window)
    if f"picked {subject.TARGET}" in (title or ""):
        return True, ""
    return False, (
        f"page title is {title!r}, expected it to name {subject.TARGET!r}; {note}"
    )


def open_file_in_vscode(meter, subject, window):
    """
    Find a filename in an Electron sidebar and open it.

    This is the case that decides the architectural argument. VS Code renders
    its own interface, so the filenames a human reads are largely absent from
    the accessibility tree -- an accessibility-only agent is blind here, and a
    pixel reader is not. Graded through the window title, which VS Code sets to
    the open file.
    """
    ok, note = click_by_text(meter, subject, window, subject.TARGET)
    if not ok:
        return False, note

    title = subject.ground_truth(window)
    if subject.TARGET in (title or ""):
        return True, ""
    return False, f"title is {title!r}, expected it to name {subject.TARGET!r}; {note}"


TASKS = [
    Task("arithmetic by label", S.Calculator, arithmetic_by_label,
         "the cascade, on a fully accessible XAML surface"),
    Task("arithmetic via accessibility", S.Calculator, arithmetic_by_accessibility,
         "control: the accessibility tree alone"),
    Task("read the result back", S.Calculator, read_result_back,
         "reading state the application does not hand over"),
    Task("select a file", S.Explorer, select_file_in_explorer,
         "a native Win32 list view, with a runtime-resolved label"),
    Task("click a word on a page", S.Chrome, click_button_in_browser,
         "web content, where OCR and accessibility disagree"),
    Task("open a file from the sidebar", S.VSCode, open_file_in_vscode,
         "Electron, where the accessibility tree goes blind"),
]


# --------------------------------------------------------------------------
# Configurations
# --------------------------------------------------------------------------

CONFIGS = {
    "v0.4-style (screenshot, no memory)": dict(
        observation_mode="screenshot", atlas=False, speculate=False),
    "delta only": dict(observation_mode="delta", atlas=False, speculate=False),
    "delta + memory": dict(observation_mode="delta", atlas=True, speculate=False),
    "delta + memory + prediction": dict(
        observation_mode="delta", atlas=True, speculate=True),
}


def apply(config):
    server._observation_mode = config["observation_mode"]
    server._atlas_enabled = config["atlas"]
    server._speculate_enabled = config["speculate"]
    # Force per-config state to be rebuilt rather than carried over. Closing
    # the old model matters: it holds a borrowed Desktop Duplication, and
    # Windows grants only one per process, so dropping it without releasing
    # leaks the handle that the next configuration needs.
    if server._model is not None:
        try:
            server._model.close()
        except Exception:
            pass
    server._model = None
    server._transitions = None


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def selected_tasks():
    if not ONLY:
        return TASKS
    return [t for t in TASKS if t.subject_factory.name in ONLY]


def main():
    tasks = selected_tasks()
    print("Task success, which is what perception cost was only ever a proxy for.")
    print(f"{REPEATS} runs per task per configuration, "
          f"{len({t.subject_factory.name for t in tasks})} applications.")
    print("=" * 78)

    results = []
    skipped = {}

    for config_name, config in CONFIGS.items():
        apply(config)
        print(f"\n### {config_name}")

        for task in tasks:
            runs = []
            for attempt in range(REPEATS):
                meter = Meter()
                started = time.perf_counter()
                try:
                    success, detail = run_task(task, meter)
                except Exception as e:
                    success, detail = False, f"{type(e).__name__}: {e}"
                elapsed = time.perf_counter() - started

                if success is None:  # subject unavailable here
                    skipped[task.name] = detail
                    break

                row = TaskResult(
                    task=task.name, subject=task.subject_factory.name,
                    config=config_name, success=success, steps=meter.steps,
                    seconds=elapsed, tokens=meter.tokens, detail=detail,
                )
                results.append(row)
                runs.append(row)
                if not success:
                    print(f"      run {attempt + 1} failed: {detail}")

            if not runs:
                continue
            passed = sum(r.success for r in runs)
            mark = "PASS" if passed == len(runs) else ("FLAKY" if passed else "FAIL")
            print(f"  {mark:<5} {task.subject_factory.name:<14} {task.name:<30} "
                  f"{passed}/{len(runs)}  "
                  f"{statistics.median(r.seconds for r in runs):5.1f}s  "
                  f"{int(statistics.median(r.tokens for r in runs)):>6,} tokens")

    if not results:
        print("\nNothing ran.")
        return

    print("\n" + "=" * 78)
    print(f"\n{'configuration':<36} {'passed':>9} {'median s':>9} {'tokens':>10}")
    print("-" * 68)
    for config_name in CONFIGS:
        rows = [r for r in results if r.config == config_name]
        if not rows:
            continue
        passed = sum(r.success for r in rows)
        print(f"{config_name:<36} {passed:>5}/{len(rows):<3} "
              f"{statistics.median(r.seconds for r in rows):>9.1f} "
              f"{sum(r.tokens for r in rows):>10,}")

    # Where each configuration wins or loses is the useful view once the corpus
    # spans more than one kind of application.
    names = [t.subject_factory.name for t in tasks
             if any(r.subject == t.subject_factory.name for r in results)]
    seen_order = list(dict.fromkeys(names))
    print(f"\n{'configuration':<36}" + "".join(f"{n:>16}" for n in seen_order))
    print("-" * (36 + 16 * len(seen_order)))
    for config_name in CONFIGS:
        cells = []
        for name in seen_order:
            rows = [r for r in results
                    if r.config == config_name and r.subject == name]
            cells.append(f"{sum(r.success for r in rows)}/{len(rows)}" if rows else "-")
        if any(c != "-" for c in cells):
            print(f"{config_name:<36}" + "".join(f"{c:>16}" for c in cells))

    total = sum(r.success for r in results)
    print(f"\n{total}/{len(results)} task runs succeeded")

    if skipped:
        print("\nNot run here:")
        for name, why in skipped.items():
            print(f"  {name}: {why}")

    if total < len(results):
        print("\nFailures, which are the useful part:")
        seen = set()
        for r in results:
            if not r.success and (r.config, r.task) not in seen:
                seen.add((r.config, r.task))
                print(f"  {r.config} / {r.task}: {r.detail}")


if __name__ == "__main__":
    main()
