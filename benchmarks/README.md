# Benchmarks

Every performance claim in the README and in `docs/ENGINEERING_LOG.md` comes
from one of these. They run against the live desktop, so results depend on
what is on screen — run them yourself rather than trusting the numbers below.

```bash
python benchmarks/bench_change.py     # how much the screen actually changes
python benchmarks/bench_methods.py    # cost of each perception method
python benchmarks/bench_pipeline.py   # v0.4.0 path vs v0.5.0 path, end to end
python benchmarks/bench_atlas.py      # cost of a return visit to a known screen
python benchmarks/bench_settle.py     # how long screens really take to respond
python benchmarks/bench_speculate.py  # predicting an action instead of observing it
python benchmarks/bench_tasks.py      # does any of it help complete tasks?
```

`bench_tasks.py` opens and closes Calculator repeatedly (~6 minutes) and takes
focus each time, so run it when the machine is free.

Reference machine: HP EliteBook 840 G8, Intel Iris Xe, 16 GB RAM, 1920×1080 at
125% scaling, Windows 11, Python 3.13. The *ratios* transfer; the absolute
milliseconds do not.

## `bench_change.py` — the premise

Establishes that re-reading the whole screen is wasted work.

```
changed pixels per frame        median  0.012%   mean 4.297%   max 93.150%
changed 64px tiles per frame    median  0.417%   mean 8.587%   max 100.000%
=> analysing only dirty tiles is ~240x less work
```

The mean is ~350× the median: almost every observation is of a nearly-static
screen, punctuated by rare full repaints. Tuning for the mean would optimise a
case that essentially never occurs.

## `bench_methods.py` — the cost table

Orders the resolution cascade.

```
method                                    median ms   result
screen capture (mss)                          48.48   1920x1080
OCR, full screen                             197.33   256 elements
OCR, quarter of screen                        71.57   92 elements
OCR, sixteenth of screen                      27.73   25 elements
capture 256x176 region                        17.45   2.2% of screen area
UIA tree walk (foreground window)            479.79   358 elements
UIA TextPattern read                         685.74   157 text ranges
UIA FindText (exact string)                  492.61   0 hits
compositor change poll (no pixels)             0.14   DXGI dirty rects
```

Two counter-intuitive results:

- **`capture 256x176 region` costs the same as any other region.** `mss` has a
  fixed ~16.6 ms per-grab cost, so capturing only the dirty parts of the screen
  is *slower* than one full grab. Capture stays whole-frame; only analysis is
  regional.
- **The accessibility tree can be slower than OCR.** 480 ms here, and on Chrome
  537 ms while returning no page text at all. It is not the free lunch it is
  usually described as.

## `bench_pipeline.py` — end to end

```
                                med ms   total ms  med tokens  total tokens
v0.4.0 full OCR + shot           212.2       3139       2,764        38,696
v0.5.0 incremental delta          32.8       1520          49         3,930

latency : 6.5x faster (median per step)
tokens  : 10x fewer

lookup of known text:
  v0.4.0 (capture + full OCR) :    317.9 ms
  v0.5.0 (cascade rung 0)     :    0.055 ms   5,801x cheaper
```

## `bench_atlas.py` — the cost of a return visit

```
cold: full screen read                   125.2 ms
warm: recognise + verify                   1.41 ms
recall hit rate: 5/5
warm start is 89x cheaper than reading the screen

rejections (all should be None):
  blank screen            : None
  inverted screen         : None
  same pixels, other app  : None
```

The rejections matter more than the speedup. An atlas that returns a stale
layout makes the agent click somewhere arbitrary, so verification must fail
closed — note that the inverted screen is *recognised* by the layout signature
(inverting does not move edges) and then *rejected* by the pixel check. That
split is the design: the signature filters, verification guarantees.

## `bench_tasks.py` — does any of it help?

Every other benchmark here measures perception *cost*, which is a proxy. This
one measures whether the agent finishes the job, across six configurations and
four applications of increasing difficulty:

    Calculator      XAML, fully exposed to accessibility
    File Explorer   native Win32 list view
    Chrome          web content, where OCR and accessibility disagree most
    VS Code         Electron — an entire IDE exposing ~18 elements

Tasks drive the real MCP tool surface and are graded against **each
application's own state** — UI Automation for Calculator and Explorer, the
window title for Chrome and VS Code, where the fixture page reports what was
clicked by setting `document.title`. Never OCR: grading OCR with OCR would only
prove it agrees with itself.

Each task runs several times per configuration, because a single sample cannot
distinguish "this configuration is worse" from "that click did not register".
Set `OSWRIGHT_BENCH_REPEATS` to raise the count when a result is load-bearing;
three proved too few (see below). `OSWRIGHT_BENCH_SUBJECTS` restricts the corpus
while iterating.

Every subject is stateless or runs against a throwaway profile and a
purpose-created folder, so no real document, tab, login or unsaved buffer is
reachable. Notepad is excluded outright: launching it restored a document with
unsaved changes.

VS Code is reported separately below — it declines to open a new window when
another instance is already running, and a task that never launched is recorded
as "not run" rather than as a failure.

**Result, across four applications:**

| configuration | Calculator | File Explorer | Chrome | tokens |
|---|---|---|---|---|
| v0.4-style (full screenshot) | 9/9 | 3/3 | 3/3 | 118,858 |
| delta only | 9/9 | 3/3 | 3/3 | **5,252** |
| delta + memory | 9/9 | 3/3 | 3/3 | 5,099 |
| delta + memory + prediction | 9/9 | 3/3 | 3/3 | 7,981 |

**Accuracy is identical across every configuration and token cost falls 23×** —
cheaper did not mean worse, which is what every other benchmark here was only a
proxy for.

**Ablations, which measure the architecture rather than asserting it:**

| configuration | Calculator | File Explorer | Chrome |
|---|---|---|---|
| full cascade | **9/9** | **3/3** | **3/3** |
| accessibility only | 9/9 | **0/3** | **0/3** |
| pixels only | **6/9** | 3/3 | 3/3 |

Accessibility-only is the posture other Windows GUI agents take. It is perfect
on XAML and scores zero on a Win32 list view and on web content. Pixels-only
fails Calculator's buttons, because the button a human reads as `7` is *named*
`Seven` and Windows OCR returns no digits from Calculator at all. Only the
cascade passes everywhere.

**VS Code**, measured in a separate run while no other instance was open:
3/3 for each of the three main configurations, and **0/3 for accessibility
only**. The direct probe is more useful than the task score, and reproduces in
seconds: UI Automation returns **18 elements** for the whole window —
`Minimize`, `Maximize`, `Restore`, `Close`, and one node named **`Chrome Legacy
Window`** containing the entire interface — while OCR reads 94 from the same
frame, including every filename in the sidebar. An accessibility-only agent is
not merely slower there; it cannot see the application at all.

Read the tables with two caveats. **Wall-clock barely moves** because these
tasks are dominated by application start-up and the deliberate settle between
clicks — perception is a small share of the total, and its latency win is the
one in `bench_pipeline.py`. And **memory and prediction do not pay for
themselves here**; they amortise over repeat visits, and a handful of short
novel tasks contains none.

Findings from building it, which are worth as much as the tables:

- **The benchmark became part of the screen it measured.** Its own console
  output contained the labels it was searching for, and the agent clicked them —
  `Eight` at x=1587, outside a Calculator window ending at x=1185. That exposed
  a real defect: `window_title` constrained only the accessibility rungs, so the
  pixel rungs could act on a different application entirely.
- **OCR is not a stable identity.** `bravo_notes.txt` came back as
  `bravo notes.b(t`. Approximate matching now runs as a fallback, and refuses
  when two candidates score alike.
- **Explorer hides file extensions**, per machine, so the on-screen label is
  resolved from the live window rather than assumed from the fixture.
- **It caught a bug 221 tests could not.** Desktop Duplication was silently
  disabled for half the system, because Windows grants one per process and
  oswright built two (see `ENGINEERING_LOG.md` §2.13).
- **Three repeats were not enough.** One sweep reported 24/36 with a task
  failing in all four configurations — a systematic-looking signal that turned
  out to be environmental; the next identical sweep reported 36/36. Reproduce a
  finding before explaining it.
- **Windows OCR cannot see isolated digits.** It returned 30 text elements from
  the Calculator window — `DEG`, `MC`, `Function`, `Trigonometry`, `log` — and
  **not one digit**. Text recognisers are trained on words and lines; a lone
  glyph on a button has no line context to belong to. This is why the cascade
  routes very short queries to the accessibility tree first.
- **The label a human reads is not the label a machine exposes.** The button a
  person sees as "7" is named `Seven` in the accessibility tree. An agent
  reasoning from a screenshot asks for the wrong string, and no amount of
  perception engineering fixes that — it needs a synonym layer or an agent told
  to use accessible names.


## `bench_head_to_head.py` -- against Windows-MCP

Every other benchmark here compares oswright to earlier versions of itself,
which cannot answer the only question a person choosing a tool asks.

Same tasks, four scenarios, each graded by the application itself. Neither tool
grades itself. Windows-MCP runs at its own defaults.

| | Calculator | Explorer | Chrome | Chrome, 2 steps | passed | tokens |
|---|---|---|---|---|---|---|
| oswright | 5/5 | 4/5 | 5/5 | 5/5 | 19/20 | **832** |
| Windows-MCP, snapshot per action | 5/5 | 5/5 | 5/5 | 5/5 | **20/20** | 14,053 |
| Windows-MCP, snapshot once | 5/5 | 5/5 | 5/5 | 5/5 | **20/20** | 8,214 |

**Windows-MCP was more reliable; oswright was 16.9x cheaper.** oswright dropped
one click in twenty on a window that had just opened. Reporting that the other
way round would be the easiest lie in this repository.

The cost difference is mechanical, not a matter of tuning. Windows-MCP returns
the screen to the agent -- `Snapshot` renders the accessibility tree as
`(x,y) button "Seven" [action: click]` -- and takes coordinates back through
`Click`, so a description of the screen is charged to the model's context on
every action. oswright takes the text and returns the outcome.

The reliability gap may be caused by the speed: oswright resolves and clicks in
~100 ms, sometimes before a freshly-focused window is ready for input, where a
slower loop gives the application time it never had to ask for. Adding a
pre-action settle made no measurable difference over ten trials, so this is
recorded as an open question rather than patched on a hunch.

### Why there is a two-step scenario

Every other task here is short enough that a tool can read the screen once and
reuse those coordinates for every click. That is a real advantage and also a
special case, and measuring only such tasks quietly favours designs that cache
aggressively -- including the cheap Windows-MCP configuration this file reports.

In `Chrome, 2 steps` the first click replaces the controls and moves them 325 px
down the page. The snapshot-once arm **had to re-read the screen** there, which
is reported in the output. On tasks whose interface moves, its cheap number does
not exist: its real cost is the per-action one.

**A result in their favour:** their accessibility traversal reads Chrome's page
content, which oswright's own accessibility rung does not. The ablation finding
above is therefore about *oswright's* UIA rung, not about accessibility APIs in
general.

**Setup** (Windows-MCP needs Python 3.14; `uv` installs it alongside, not over,
your existing interpreter):

```
uv python install 3.14
git clone https://github.com/CursorTouch/Windows-MCP.git %TEMP%/Windows-MCP
cd %TEMP%/Windows-MCP
uv venv --python 3.14 .venv
uv pip install --python .venv/Scripts/python.exe -e .
```

Check the connection first with `python benchmarks/probe_windows_mcp.py`, which
lists their tool surface and confirms stdio needs no credentials.

**Fairness rules, fixed before the first run:** same applications and targets;
graded by the application, not by either tool; identical token accounting;
Windows-MCP at its own defaults, with `use_vision=True` offered as a retry
whenever its tree cannot describe a target, rather than scoring that as a miss;
failures on either side reported.

That last rule earned its place immediately. The first run recorded Windows-MCP
failing with the display showing `9,999` -- because I had passed `label=N` to
`Click`, having read its schema and not its output format. Their labels are for
annotated screenshots; the default tree gives coordinates. My adapter drove the
wrong buttons and the harness blamed them. When you build the apparatus that
measures a competitor, every bug in it defaults to their disadvantage.

**What this does not establish:** three short tasks on one laptop. Nothing about
long multi-step work, recovery, or the product surface Windows-MCP has and
oswright does not.
