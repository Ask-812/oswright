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

#: 7 x 8 = 56. Accessible names, which is what both tools are asked for.
BUTTONS = ["Seven", "Multiply by", "Eight", "Equals"]
EXPECTED = "56"


def tokens_of(text: str) -> int:
    return max(1, len(text) // 4)


# --------------------------------------------------------------------------
# oswright
# --------------------------------------------------------------------------

def run_oswright(subject, window):
    """Drive the task through oswright's cascade. Returns (ok, tokens, steps)."""
    tokens = steps = 0
    for label in BUTTONS:
        out = server.click_element(text=label, window_title=subject.window_hint(window))
        steps += 1
        for part in out if isinstance(out, list) else [out]:
            if isinstance(part, str):
                tokens += tokens_of(part)
            elif getattr(part, "data", None):
                tokens += int(1920 * 1080 * IMAGE_TOKENS_PER_PIXEL)
        time.sleep(subject.click_settle_s)
    return tokens, steps


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


async def run_windows_mcp(session, subject, window, snapshot_every_action: bool):
    """
    Drive the same task through Windows-MCP.

    Two loops, because the fair comparison is against its best case as well as
    its prescribed one:

      snapshot_every_action=True   Snapshot before each click. This is what its
                                   tool descriptions prescribe, and what an
                                   agent must do when the screen may have moved.

      snapshot_every_action=False  Snapshot once, then reuse the coordinates for
                                   all four clicks. Cheapest possible use of the
                                   tool, and only safe because Calculator does
                                   not move between clicks.
    """
    tokens = steps = 0
    snapshot = None

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

    for name in BUTTONS:
        if snapshot is None or snapshot_every_action:
            snapshot = await call("Snapshot", {})
        where = find_target(snapshot, name)
        if where is None:
            return tokens, steps, f"Snapshot did not locate {name!r}"
        await call("Click", {"loc": where})
        time.sleep(subject.click_settle_s)

    return tokens, steps, None


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

    print("oswright against Windows-MCP: 7 x 8 on Calculator, graded by Calculator.")
    print(f"{REPEATS} runs each.")
    print("=" * 74)

    rows = {
        "oswright": [],
        "Windows-MCP (per action)": [],
        "Windows-MCP (snapshot once)": [],
    }

    # --- oswright ---
    for _ in range(REPEATS):
        subject = S.Calculator()
        window = subject.launch()
        if window is None:
            continue
        try:
            started = time.perf_counter()
            tokens, steps = run_oswright(subject, window)
            elapsed = time.perf_counter() - started
            shown = subject.ground_truth(window)
            rows["oswright"].append(
                (shown == EXPECTED, tokens, steps, elapsed, shown)
            )
        finally:
            subject.cleanup(window)

    # --- Windows-MCP ---
    params = StdioServerParameters(
        command=python,
        args=["-m", "windows_mcp", "serve", "--transport", "stdio"],
        cwd=WINDOWS_MCP_ROOT,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for name, every in (
                ("Windows-MCP (per action)", True),
                ("Windows-MCP (snapshot once)", False),
            ):
                for _ in range(REPEATS):
                    subject = S.Calculator()
                    window = subject.launch()
                    if window is None:
                        continue
                    try:
                        started = time.perf_counter()
                        tokens, steps, error = await run_windows_mcp(
                            session, subject, window, every
                        )
                        elapsed = time.perf_counter() - started
                        shown = subject.ground_truth(window)
                        rows[name].append(
                            (shown == EXPECTED and not error, tokens, steps,
                             elapsed, error or shown)
                        )
                    finally:
                        subject.cleanup(window)

    print(f"\n{'tool':<30}{'passed':>8}{'tokens':>10}{'steps':>7}{'seconds':>9}")
    print("-" * 74)
    summary = {}
    for name, runs in rows.items():
        if not runs:
            print(f"{name:<30}{'no runs':>8}")
            continue
        passed = sum(1 for r in runs if r[0])
        tok = int(statistics.median(r[1] for r in runs))
        steps = int(statistics.median(r[2] for r in runs))
        secs = statistics.median(r[3] for r in runs)
        summary[name] = (passed, len(runs), tok)
        print(f"{name:<30}{passed:>3}/{len(runs):<4}{tok:>10,}{steps:>7}{secs:>9.1f}")

    for name, runs in rows.items():
        bad = [r for r in runs if not r[0]]
        if bad:
            print(f"\n{name} failures:")
            for r in bad:
                print(f"  {r[4]}")

    mine = summary.get("oswright")
    best = summary.get("Windows-MCP (snapshot once)")
    naive = summary.get("Windows-MCP (per action)")
    if mine and best and mine[2]:
        print(
            f"\nAgainst its prescribed loop, oswright carries "
            f"{naive[2] / mine[2]:.1f}x less context; against its cheapest "
            f"possible use, {best[2] / mine[2]:.1f}x less."
        )
        print(
            "\nThe difference is structural rather than a tuning win. Windows-MCP\n"
            "returns the screen to the agent and takes coordinates back, so the\n"
            "description of the screen is charged to the model's context. oswright\n"
            "resolves the text inside click_element and returns only what changed.\n"
            "\n"
            "Snapshotting once is only safe because Calculator does not move between\n"
            "clicks. An agent that assumes that in general acts on stale coordinates,\n"
            "which is the same unsoundness oswright had at rung 0 and had to fix.\n"
            "\n"
            "This is one task on one application. It says nothing about robustness\n"
            "across many applications, nor about the product surface Windows-MCP has\n"
            "and oswright does not: OAuth, analytics, a watchdog, an installer."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
