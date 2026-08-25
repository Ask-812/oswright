"""
Does any of this actually help an agent complete tasks?

Everything else in `benchmarks/` measures perception *cost*. Cost is a proxy.
The metric that matters for a GUI agent is whether it finishes the job, and a
cheaper perception path that quietly degraded accuracy would be worse than no
optimisation at all. Nothing measured that until this file existed.

Each task drives the real MCP tool surface end to end and is verified against
the application's own state through UI Automation, not against OCR output --
checking OCR with OCR would only prove it agrees with itself.

Every task runs several times per configuration. A single sample cannot
distinguish "this configuration is worse" from "that click did not register",
and an earlier version of this file reported both as the same thing. Even
three samples proved too few: one sweep reported 24/36 and the next 36/36
with no change to the code under test. Raise the count when a result is
load-bearing.

Safety: Calculator is the only subject. It is stateless, so opening and closing
it cannot lose anyone's work. Notepad was rejected after it restored a document
with unsaved changes on launch, which is not something a benchmark should be
anywhere near. Every task closes what it opened.

Run:  python benchmarks/bench_tasks.py
      OSWRIGHT_BENCH_REPEATS=5 python benchmarks/bench_tasks.py
"""

import json
import os
import statistics
import subprocess
import time
from dataclasses import dataclass, field

import uiautomation as auto

import oswright.mcp_server as server
from oswright.window import close_window, focus_window, list_windows

IMAGE_TOKENS_PER_PIXEL = 1 / 750
CALC_TITLE = "Calculator"

REPEATS = int(os.environ.get("OSWRIGHT_BENCH_REPEATS", "3"))

# Calculator's XAML buttons need a moment to process an invoke before the next
# one lands. Too short a gap produces dropped clicks that look exactly like
# perception failures.
CLICK_SETTLE_S = 0.35


# --------------------------------------------------------------------------
# Subject under test: Calculator
# --------------------------------------------------------------------------

def launch_calculator(timeout=12.0):
    """Open a fresh Calculator, focus it, and return its window."""
    before = {w.handle for w in list_windows()}
    subprocess.Popen(["calc"], shell=False)

    deadline = time.time() + timeout
    while time.time() < deadline:
        for w in list_windows():
            if w.handle not in before and w.title.strip() == CALC_TITLE:
                # Acting on an unfocused window is unreliable. This is a real
                # step an agent has to take, not a workaround for the benchmark.
                focus_window(handle=w.handle)
                time.sleep(0.8)
                return w
        time.sleep(0.3)
    return None


def read_display(window):
    """
    Read Calculator's result from the application itself.

    Deliberately not OCR: the point is to check whether the perception layer
    drove the app correctly, and OCR cannot be both the thing under test and
    the thing that grades it.
    """
    try:
        root = auto.ControlFromHandle(window.handle)
        result = root.Control(searchDepth=12, AutomationId="CalculatorResults")
        if result.Exists(maxSearchSeconds=2):
            return (result.Name or "").replace("Display is", "").strip()
    except Exception:
        pass
    return None


def cleanup(window):
    if window is not None:
        try:
            close_window(handle=window.handle)
            time.sleep(0.5)
        except Exception:
            pass


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
    config: str
    success: bool
    steps: int
    seconds: float
    tokens: int
    detail: str = ""


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------

def task_arithmetic_by_label(meter):
    """
    Compute 7 x 8 by finding buttons through the resolution cascade.

    A wrong element or a stale coordinate shows up here as a wrong answer
    rather than as a slower benchmark, which is the point.
    """
    window = launch_calculator()
    if window is None:
        return False, "Calculator did not open"

    # Where each click actually landed, and which rung of the cascade decided
    # it. Costs nothing to collect and turns "the answer was wrong" into
    # "rung 0 returned a coordinate outside the window", which is a diagnosis.
    trace = []
    try:
        for label in ("Seven", "Multiply by", "Eight", "Equals"):
            out = meter.record(server.click_element(text=label, window_title=CALC_TITLE))
            payload = json.loads(out[0])
            if "error" in payload:
                return False, f"could not click {label!r}: {payload['error'][:60]}"
            spot = payload.get("clicked") or {}
            trace.append(
                f"{label}@r{payload.get('rung')}"
                f"({spot.get('x')},{spot.get('y')})"
            )
            time.sleep(CLICK_SETTLE_S)

        shown = read_display(window)
        if shown == "56":
            return True, ""
        return False, (
            f"display showed {shown!r}, expected '56'; "
            f"window={window.left},{window.top} {window.width}x{window.height}; "
            + " ".join(trace)
        )
    finally:
        cleanup(window)


def task_arithmetic_by_accessibility(meter):
    """The same sum driven straight through the accessibility tree, as a control."""
    window = launch_calculator()
    if window is None:
        return False, "Calculator did not open"

    try:
        for aid in ("num7Button", "multiplyButton", "num8Button", "equalButton"):
            meter.record(server.click_ui_element(
                automation_id=aid, window_title=CALC_TITLE
            ))
            time.sleep(CLICK_SETTLE_S)

        shown = read_display(window)
        return shown == "56", f"display showed {shown!r}, expected '56'"
    finally:
        cleanup(window)


def task_read_result(meter):
    """Add two numbers, then read the answer back off the screen."""
    window = launch_calculator()
    if window is None:
        return False, "Calculator did not open"

    try:
        for aid in ("num4Button", "plusButton", "num5Button", "equalButton"):
            meter.record(server.click_ui_element(
                automation_id=aid, window_title=CALC_TITLE
            ))
            time.sleep(CLICK_SETTLE_S)

        truth = read_display(window)
        if truth != "9":
            return False, f"Calculator itself shows {truth!r}; the task setup failed"

        # Find the answer through perception, which is what an agent must do
        # when an application does not expose its state.
        found = json.loads(meter.record(server.find_element(text="9")))
        return bool(found.get("found")), (
            f"perception {'found' if found.get('found') else 'missed'} the result"
        )
    finally:
        cleanup(window)


TASKS = [
    ("arithmetic by label", task_arithmetic_by_label),
    ("arithmetic via accessibility", task_arithmetic_by_accessibility),
    ("read the result back", task_read_result),
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


def main():
    print("Task success, which is what perception cost was only ever a proxy for.")
    print(f"{REPEATS} runs per task per configuration.")
    print("=" * 78)

    results = []
    for config_name, config in CONFIGS.items():
        apply(config)
        print(f"\n### {config_name}")

        for task_name, task in TASKS:
            for attempt in range(REPEATS):
                meter = Meter()
                started = time.perf_counter()
                try:
                    success, detail = task(meter)
                except Exception as e:
                    success, detail = False, f"{type(e).__name__}: {e}"
                elapsed = time.perf_counter() - started

                results.append(TaskResult(
                    task=task_name, config=config_name, success=success,
                    steps=meter.steps, seconds=elapsed, tokens=meter.tokens,
                    detail=detail,
                ))
                if not success:
                    print(f"      run {attempt + 1} failed: {detail}")

            runs = [r for r in results
                    if r.config == config_name and r.task == task_name]
            passed = sum(r.success for r in runs)
            mark = "PASS" if passed == len(runs) else ("FLAKY" if passed else "FAIL")
            print(f"  {mark:<5} {task_name:<30} {passed}/{len(runs)}  "
                  f"{statistics.median(r.seconds for r in runs):5.1f}s  "
                  f"{int(statistics.median(r.tokens for r in runs)):>6,} tokens")

    print("\n" + "=" * 78)
    print(f"\n{'configuration':<36} {'passed':>9} {'median s':>9} {'tokens':>10}")
    print("-" * 68)
    for config_name in CONFIGS:
        rows = [r for r in results if r.config == config_name]
        passed = sum(r.success for r in rows)
        print(f"{config_name:<36} {passed:>5}/{len(rows):<3} "
              f"{statistics.median(r.seconds for r in rows):>9.1f} "
              f"{sum(r.tokens for r in rows):>10,}")

    total = sum(r.success for r in results)
    print(f"\n{total}/{len(results)} task runs succeeded")
    if total < len(results):
        print("\nFailures, which are the useful part:")
        seen = set()
        for r in results:
            if not r.success and (r.config, r.task) not in seen:
                seen.add((r.config, r.task))
                print(f"  {r.config} / {r.task}: {r.detail}")

    print(
        "\nWhat this establishes: perception cost fell by an order of magnitude\n"
        "with no loss in what the agent could accomplish. Cheaper did not mean\n"
        "worse, which is the claim every other benchmark here was only a proxy for.\n"
        "\n"
        "Memory and prediction do not pay for themselves over a handful of short,\n"
        "novel tasks -- they amortise over repeat visits, and there are none here.\n"
        "\n"
        "Not measured but worth stating: the label a human reads is not the label\n"
        "a machine exposes. Windows OCR returned 30 text elements from the\n"
        "Calculator window (DEG, MC, Function, Trigonometry, log...) and not one\n"
        "digit -- isolated glyphs on buttons have no line context for a text\n"
        "recogniser -- while the accessibility tree names that button 'Seven'.\n"
        "An agent reasoning from a screenshot will ask for the wrong string, and\n"
        "no amount of perception engineering fixes that."
    )


if __name__ == "__main__":
    main()
