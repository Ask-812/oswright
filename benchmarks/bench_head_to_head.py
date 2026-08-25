"""
oswright against Windows-MCP, on the same task, graded the same way.

Every other benchmark here compares oswright to earlier versions of itself,
which cannot answer the only question that matters to someone choosing a tool:
is this better than what already exists?

The two take genuinely different positions, and the difference is mechanical
rather than a matter of tuning:

  Windows-MCP  the agent calls Snapshot to get a numbered list of UI elements,
               reads it, and then calls Click(label=N). Perception is a separate
               step the model pays for on every action.

  oswright     the agent calls click_element(text="Seven"). Perception happens
               inside the call, through the resolution cascade, and only the
               outcome is returned.

So the comparison is not "whose OCR is faster". It is whether an agent has to
carry a description of the screen in its context in order to act.

Fairness rules, fixed before the first run:

 1. Both drive the same application, the same four buttons, in the same order.
 2. Both are graded by Calculator's own UI Automation display, never by either
    tool's output. Neither tool grades itself.
 3. Tokens are counted identically: every character of text either server
    returns to the agent, divided by four. Images are charged at the rate a
    vision model bills a 1920x1080 frame.
 4. Windows-MCP runs with its own defaults. Its Snapshot defaults to the
    accessibility tree with no screenshot, which is the configuration its
    documentation describes, and it is not crippled to flatter oswright.
 5. Failures on either side are reported, including oswright's.

What this does not measure: robustness across many applications, long tasks,
recovery, or anything about Windows-MCP's product surface -- OAuth, analytics,
a watchdog, a scheduled-task installer -- none of which oswright has.

Setup:
    uv python install 3.14
    git clone https://github.com/CursorTouch/Windows-MCP.git %TEMP%/Windows-MCP
    cd %TEMP%/Windows-MCP && uv venv --python 3.14 .venv
    uv pip install --python .venv/Scripts/python.exe -e .

Run:
    python benchmarks/bench_head_to_head.py
"""

import asyncio
import os
import re
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import subjects as S  # noqa: E402

import oswright.mcp_server as server  # noqa: E402

WINDOWS_MCP_ROOT = os.environ.get(
    "WINDOWS_MCP_ROOT", os.path.join(os.environ.get("TEMP", "."), "Windows-MCP")
)

IMAGE_TOKENS_PER_PIXEL = 1 / 750
REPEATS = int(os.environ.get("OSWRIGHT_BENCH_REPEATS", "3"))


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------
#
# One application is not a comparison. These span the three surfaces that
# behave differently: a fully accessible XAML app, a native Win32 list view,
# and web content. Each names the controls to click and how the *application*
# reports success, so neither tool is ever asked to grade itself.

class Scenario:
    def __init__(self, name, subject_factory, targets, check):
        self.name = name
        self.subject_factory = subject_factory
        self._targets = targets
        self.check = check

    def targets(self, subject, window):
        """The labels to click, resolved against the live window if needed."""
        return self._targets(subject, window) if callable(self._targets) else self._targets


SCENARIOS = [
    Scenario(
        "Calculator: 7 x 8",
        S.Calculator,
        ["Seven", "Multiply by", "Eight", "Equals"],
        lambda subject, window: subject.ground_truth(window) == "56",
    ),
    Scenario(
        "Explorer: select a file",
        S.Explorer,
        lambda subject, window: [subject.target_label(window)],
        lambda subject, window: subject.ground_truth(window) == subject.TARGET_STEM
        or (subject.ground_truth(window) or "").startswith(subject.TARGET_STEM),
    ),
    Scenario(
        "Chrome: click a word",
        S.Chrome,
        lambda subject, window: [subject.TARGET],
        lambda subject, window: f"picked {subject.TARGET}"
        in (subject.ground_truth(window) or ""),
    ),
]


def tokens_of(text: str) -> int:
    return max(1, len(text) // 4)


# --------------------------------------------------------------------------
# oswright
# --------------------------------------------------------------------------

def run_oswright(subject, window, targets):
    """Drive the task through oswright's cascade. Returns (tokens, steps, error)."""
    tokens = steps = 0
    for label in targets:
        if not label:
            return tokens, steps, "the fixture was not on screen; setup failed"
        out = server.click_element(text=label, window_title=subject.window_hint(window))
        steps += 1
        for part in out if isinstance(out, list) else [out]:
            if isinstance(part, str):
                tokens += tokens_of(part)
                if '"error"' in part:
                    return tokens, steps, f"could not click {label!r}"
            elif getattr(part, "data", None):
                tokens += int(1920 * 1080 * IMAGE_TOKENS_PER_PIXEL)
        time.sleep(subject.click_settle_s)
    return tokens, steps, None


# --------------------------------------------------------------------------
# Windows-MCP
# --------------------------------------------------------------------------

#: Their snapshot renders each control as `(x,y) button "Name"  [action: click]`
#: and Click takes those coordinates. An earlier version of this file passed
#: `label=N` instead, drove the wrong buttons, and recorded the resulting `9,999`
#: as a Windows-MCP failure -- which would have been my adapter's mistake
#: reported as their defect. The name is matched *quoted*, because an unquoted
#: search for "Eight" also matches a table header.
_COORD = re.compile(r"\((\d+)\s*,\s*(\d+)\)")


def find_target(snapshot: str, name: str):
    """The coordinates Windows-MCP reports for a named control, if any."""
    needle = name.lower()
    for line in snapshot.replace("\\n", "\n").splitlines():
        low = line.lower()
        if f'"{needle}"' not in low and f'\\"{needle}\\"' not in low:
            continue
        match = _COORD.search(line)
        if match:
            return [int(match.group(1)), int(match.group(2))]
    return None


async def run_windows_mcp(session, subject, window, targets, snapshot_every_action):
    """
    Drive the same task through Windows-MCP.

    Two loops, because the fair comparison is against its best case as well as
    its prescribed one:

      snapshot_every_action=True   Snapshot before each click. This is what its
                                   tool descriptions prescribe, and what an
                                   agent must do when the screen may have moved.

      snapshot_every_action=False  Snapshot once, then reuse the coordinates for
                                   all the clicks. Cheapest possible use of the
                                   tool, and only safe when the interface does
                                   not move between clicks.

    If its default accessibility-tree snapshot cannot find the target, the same
    snapshot is retried with `use_vision=True`. That is its documented escape
    hatch for surfaces the tree does not describe, and not offering it would be
    testing a crippled configuration.
    """
    tokens = steps = 0
    snapshot = None
    used_vision = False

    async def call(tool, args):
        nonlocal tokens, steps
        result = await session.call_tool(tool, args)
        steps += 1
        text = ""
        for chunk in result.content:
            if getattr(chunk, "text", None):
                text += chunk.text
                tokens += tokens_of(chunk.text)
            elif getattr(chunk, "data", None):
                tokens += int(1920 * 1080 * IMAGE_TOKENS_PER_PIXEL)
        return text

    for name in targets:
        if not name:
            return tokens, steps, "the fixture was not on screen; setup failed", False
        if snapshot is None or snapshot_every_action:
            snapshot = await call("Snapshot", {})

        where = find_target(snapshot, name)
        if where is None:
            snapshot = await call("Snapshot", {"use_vision": True})
            used_vision = True
            where = find_target(snapshot, name)
        if where is None:
            return (
                tokens, steps,
                f"Snapshot did not locate {name!r}, with or without vision",
                used_vision,
            )

        await call("Click", {"loc": where})
        time.sleep(subject.click_settle_s)

    return tokens, steps, None, used_vision


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------

async def main():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    python = os.path.join(WINDOWS_MCP_ROOT, ".venv", "Scripts", "python.exe")
    if not os.path.exists(python):
        print(f"Windows-MCP not installed at {WINDOWS_MCP_ROOT}; see the docstring.")
        return 1

    server._observation_mode = "delta"
    server._atlas_enabled = True
    server._speculate_enabled = False

    print("oswright against Windows-MCP, graded by each application itself.")
    print(f"{REPEATS} runs per scenario.")
    print("=" * 78)

    arms = [
        "oswright",
        "Windows-MCP (per action)",
        "Windows-MCP (snapshot once)",
    ]
    results = {name: [] for name in arms}
    vision_needed = set()

    params = StdioServerParameters(
        command=python,
        args=["-m", "windows_mcp", "serve", "--transport", "stdio"],
        cwd=WINDOWS_MCP_ROOT,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            for scenario in SCENARIOS:
                if not scenario.subject_factory().available():
                    print(f"\n### {scenario.name}: not available here")
                    continue
                print(f"\n### {scenario.name}")

                for arm in arms:
                    runs = []
                    for _ in range(REPEATS):
                        subject = scenario.subject_factory()
                        window = subject.launch()
                        if window is None:
                            continue
                        try:
                            targets = scenario.targets(subject, window)
                            started = time.perf_counter()
                            if arm == "oswright":
                                tokens, steps, error = run_oswright(
                                    subject, window, targets
                                )
                            else:
                                tokens, steps, error, vision = await run_windows_mcp(
                                    session, subject, window, targets,
                                    arm.endswith("(per action)"),
                                )
                                if vision:
                                    vision_needed.add(scenario.name)
                            elapsed = time.perf_counter() - started
                            ok = not error and scenario.check(subject, window)
                            runs.append((ok, tokens, steps, elapsed, error))
                        finally:
                            subject.cleanup(window)

                    if not runs:
                        print(f"  {arm:<30} did not run")
                        continue
                    passed = sum(1 for r in runs if r[0])
                    tok = int(statistics.median(r[1] for r in runs))
                    secs = statistics.median(r[3] for r in runs)
                    results[arm].append((scenario.name, passed, len(runs), tok))
                    note = next((r[4] for r in runs if r[4]), "") or ""
                    print(f"  {arm:<30} {passed}/{len(runs)}  {tok:>7,} tokens  "
                          f"{secs:>5.1f}s  {note[:36]}")

    print("\n" + "=" * 78)

    def grid(title, cell):
        heads = [s.name.split(":")[0] for s in SCENARIOS]
        print(f"\n{title:<30}" + "".join(f"{h:>16}" for h in heads))
        print("-" * (30 + 16 * len(heads)))
        for arm in arms:
            cells = []
            for scenario in SCENARIOS:
                row = next((r for r in results[arm] if r[0] == scenario.name), None)
                cells.append(cell(row) if row else "-")
            print(f"{arm:<30}" + "".join(f"{c:>16}" for c in cells))

    grid("passed", lambda r: f"{r[1]}/{r[2]}")
    grid("tokens", lambda r: f"{r[3]:,}")

    mine = sum(r[3] for r in results["oswright"])
    naive = sum(r[3] for r in results["Windows-MCP (per action)"])
    best = sum(r[3] for r in results["Windows-MCP (snapshot once)"])
    if mine:
        print(
            f"\nTotal tokens across scenarios: oswright {mine:,}, Windows-MCP "
            f"{naive:,} as prescribed and {best:,} at its cheapest "
            f"-- {naive / mine:.1f}x and {best / mine:.1f}x."
        )

    if vision_needed:
        print(
            "\nWindows-MCP needed use_vision=True on: "
            + ", ".join(sorted(vision_needed))
            + "\nIts accessibility-tree snapshot could not describe the target, so the"
            "\nrun was retried with a screenshot rather than scored as a miss."
        )

    print(
        "\nThe difference is mechanical rather than a matter of tuning. Windows-MCP"
        "\nreturns the screen to the agent and takes coordinates back, so a"
        "\ndescription of the screen is charged to the model's context on every"
        "\naction. oswright takes the text and returns the outcome."
        "\n"
        "\nSnapshotting once is only safe when the interface does not move between"
        "\nclicks. An agent that assumes that in general acts on stale coordinates,"
        "\nwhich is the same unsoundness oswright had at rung 0 and had to fix."
        "\n"
        "\nThree applications on one laptop. Nothing here about long multi-step work,"
        "\nrecovery, or the product surface Windows-MCP has and oswright does not."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
