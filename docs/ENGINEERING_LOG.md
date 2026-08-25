# OSWright Engineering Log

A record of what was built, why, and what the evidence was. Written to be read
before explaining this project to someone who will push back on it.

Every performance number here is reproducible via `benchmarks/`. Numbers were
measured on a single machine (HP EliteBook 840 G8, Intel Iris Xe, 16 GB RAM,
1920×1080 at 125% scaling, Windows 11, Python 3.13) and will differ elsewhere;
the *ratios* are the durable part, not the absolute milliseconds.

---

## The problem

OSWright is an MCP server that lets an LLM drive a desktop — the desktop
equivalent of Playwright MCP. An agent needs to answer two questions over and
over: *what is on screen?* and *where do I click for X?*

The obvious implementation, and the one nearly every published GUI agent uses,
is: screenshot → OCR the whole thing → hand the model an image → repeat. That
is what v0.1–v0.4 did.

It is slow, it is compute-heavy, and it is extremely expensive in tokens.

---

## Part 1 — Correctness audit (v0.4.0)

Before optimising anything, the existing code had to be trustworthy. Several
findings were install-blocking.

### 1.1 `pip install oswright` produced a server that could not start

**Evidence.** Installing the built wheel into a clean virtualenv and importing
it. My development machine had `mcp` 1.29 pinned by history; a fresh install
resolved `mcp[cli]>=1.0` to `mcp` 2.1.0, which **removed
`mcp.server.fastmcp`** — the module every one of the 36 tools was built on.

**Decision.** Pin `mcp[cli]>=1.0,<2`.

**The lesson worth stating out loud:** an unbounded upper version bound on a
dependency whose *major* version you depend on is a latent outage. It cannot be
caught by any test that runs in the development environment, because the
development environment is exactly where the old version is already installed.
Only a clean-room install finds it.

### 1.2 Windows OCR silently never activated

`pyproject.toml` declared an extra named `winocr`, which installs the PyPI
package `winocr`, which provides `winsdk.*`. The code imports `winrt.*`. Those
are different projections of the same Windows API. A clean install therefore
fell back to EasyOCR — the slow path — while appearing to work.

Worse, three more packages were required at runtime but never imported
directly, so nothing in the source hinted at them:

| Package | Needed by |
|---|---|
| `winrt-Windows.Foundation` | the `IAsyncOperation` returned by `recognize_async` |
| `winrt-Windows.Foundation.Collections` | the `IVectorView` of OCR result lines |
| `winrt-Windows.Storage.Streams` | the `IBuffer` used by `copy_from_buffer` |

These were found by iterating clean installs until OCR actually ran — three
separate `ModuleNotFoundError`s, each only reachable after fixing the previous.

### 1.3 EasyOCR pulled ~2.5 GB of PyTorch onto Windows, where it is unused

**Decision.** Make dependencies platform-conditional: Windows gets the built-in
OCR engine plus UI Automation; Linux/macOS get EasyOCR because it is their only
backend. A Windows install went from ~2.5 GB to a few MB.

### 1.4 Cached OCR results were mutated in place

`Locator._resolve` added region offsets directly to the `ElementMatch` objects
it got back — but those objects are handed out by the OCR cache. On a cache hit
the same object is returned again, so the offset is applied **a second time**.
Coordinates drifted further on every repeated lookup.

**Decision.** `ElementMatch.offset()` returns a translated copy; the dataclass
is frozen. Fixing the symptom (clearing the cache) would have left the sharp
edge in place for the next caller.

### 1.5 A DPI bug that depended on import order

On this machine (125% scaling), `GetWindowRect` reported **1536×864** while
screenshots were **1920×1080**. Window coordinates and screenshot pixels were
in different units, so clicks derived from window geometry landed wrong.

The reason it had gone unnoticed: `mss` calls `SetProcessDPIAware()` when it
initialises. So the coordinate system the process used **depended on whether a
screenshot had been taken yet**.

```
before importing mss : GetSystemMetrics -> 1536 x 864   (logical)
after  importing mss : GetSystemMetrics -> 1920 x 1080  (physical)
```

**Decision.** `oswright/_dpi.py` declares per-monitor-v2 DPI awareness at
import, before any window is created. Everything is physical pixels from the
start, regardless of import order.

### 1.6 The security fix that broke the feature

v0.3.0 hardened `launch_app` against command injection with a blocklist of
shell metacharacters. The blocklist included `\`, so **every absolute Windows
path was rejected** — `C:\Windows\notepad.exe` was refused as dangerous.

**Decision.** Delete the blocklist. With `shell=False` no shell ever
interprets the string, so metacharacters cannot inject anything; the blocklist
was defending against a threat that the execution model had already eliminated,
at the cost of the feature. Replaced with correct argv parsing
(`shlex.split(posix=False)` on Windows, which preserves backslashes) plus an
explicit `args` list that needs no parsing at all.

### 1.7 Other findings

64-bit ctypes handle truncation (missing `restype`, so `HWND`/`HANDLE` returns
were cut to 32 bits); stuck modifier keys when an exception occurred mid-combo;
`mss` shared across threads though it is not thread-safe and the MCP server
runs tools in a thread pool; emoji corrupted by `type_text` because
`ord(char)` was truncated into a 16-bit field instead of being sent as a UTF-16
surrogate pair; `--timeout`, `--host` and `--port` having no effect at all.

A subtle one: setting `restype = HWND` made `list_windows()` return **zero**
windows. ctypes maps a NULL `c_void_p` return to Python `None`, so the existing
`GetWindow(...) != 0` check became `None != 0` → true → every window skipped.
Caught immediately because a smoke test asserted a non-empty result.

---

## Part 2 — Perception redesign (v0.5.0)

### 2.1 The measurement that drove everything

Before designing anything, measure how much the screen actually changes between
observations.

```
changed pixels per frame        median  0.012%   mean 4.297%   max 93.150%
changed 64px tiles per frame    median  0.417%   mean 8.587%   max 100.000%
```

**The median observation changes 0.012% of pixels.** Re-reading the whole
screen is doing roughly 240× more work than the change warrants.

Note the mean is 350× the median. The distribution is extremely skewed: almost
all observations are of a nearly-static screen, punctuated by rare full
repaints (a window opening, a scroll). A design tuned to the mean would be
tuned for a case that almost never happens.

### 2.2 Cost of every perception method

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

Two results here contradict conventional advice, and both changed the design:

**The accessibility tree is not automatically cheap.** The standard
recommendation — including from a literature survey I commissioned for this
work — is to make the accessibility tree the primary perception path. On real
applications it is often *slower than OCR*:

| Target | UIA elements found | Walk time |
|---|---|---|
| Native window | 31–47 | 40 ms |
| Chrome, GitHub page | 306, **no page text** | **537 ms** |
| VS Code (Electron) | **18** | 80 ms |

An entire IDE exposes 18 elements. This is precisely where the competing
project [Windows-MCP](https://github.com/CursorTouch/Windows-MCP) positions
itself — "Vision Optional", accessibility-tree only — and it is where that
approach goes blind. Neither pixels nor accessibility wins everywhere, which is
the argument for a cascade rather than a choice.

**OCR cost is roughly linear in area** (197 → 72 → 28 ms for 1×, ¼, 1⁄16).
So restricting *analysis* to changed regions should pay off proportionally.

### 2.3 Incremental perception

Keep a persistent model of on-screen text; update only what moved.

First implementation: **4.4× faster, 91.9% recall.** The missing 8% needed
explaining before the design could be trusted.

**Control experiment** (`test_ocr_stability.py`, since folded into the
benchmarks): is OCR even self-consistent?

```
identical frame, OCR run 3x        -> 100.0% agreement  (deterministic)
two captures 0.4s apart            ->  92.7% agreement  (screen really changed)
full-frame OCR vs OCR of a crop    ->  90.9% agreement  (!!)
```

The third row is the important one. Full-frame OCR and OCR of a *crop of the
same area* disagree, and the disagreements are segmentation:

```
full frame reports : 'Placer'        'Bookmarks'
crop reports       : 'Plac' + 'er'   'B' + 'ookmarks'
```

**Cutting the image changes how the OCR engine groups glyphs into words.**
This is an inherent property of region-based OCR, not a bug — and it means
region-based OCR can never be *identical* to full-frame OCR by construction.

Two design consequences:
1. Dirty regions are padded **wider than they are tall** (96 px vs 24 px), so
   horizontal text lines stay intact. Vertical padding is cheap because text
   lines are short vertically.
2. The "missing" recall was mostly this effect plus genuine screen change, not
   model leakage. Worth knowing before chasing a phantom bug.

**The correctness invariant.** The first version leaked text at region edges:
a dirty rectangle that clipped a text box mid-word deleted the tracked element
and re-detected only the visible fragment. The fix is a stated invariant —
*anything invalidated must be fully rescanned* — implemented by growing each
dirty region until it wholly contains every element it touches, to a fixed
point. Recall went 88.8% → 91.9%, and more importantly the failure mode became
impossible rather than merely rare.

Result after also cheapening the tile hash (subsample by 4): **11.7× median.**

### 2.4 The dead end: capturing only dirty regions

The natural next step is to capture only the changed regions rather than the
whole frame. Measured:

```
full screen 1920x1080      :  33.46 ms
region  960x540  (25.0%)   :  16.59 ms
region  480x270  ( 6.2%)   :  16.61 ms
region  256x176  ( 2.2%)   :  16.58 ms
region  128x64   ( 0.4%)   :  16.76 ms

4 separate dirty regions   :  66.39 ms   vs 33.46 ms full  -> 0.5x
```

**`mss` has a fixed ~16.6 ms cost per grab regardless of region size**, so four
small grabs cost *twice* a single full-screen grab. The optimisation is not
merely small, it is negative.

Capture stays whole-frame. Only *analysis* is regional. This is why the design
says "incremental perception" and not "incremental capture".

### 2.5 Asking the compositor instead of computing the answer

The Windows compositor already knows which pixels changed — it must, in order
to present efficiently — and exposes it through
`IDXGIOutputDuplication::GetFrameDirtyRects`.

Crucially, `AcquireNextFrame` reports whether anything changed **without
transferring any pixels**:

```
compositor change poll :   0.14 ms
full screen capture    :  48.48 ms      ~350x
```

So an idle observation can be settled for a fraction of a millisecond instead
of capturing a frame and then discovering it was identical. Measured live: **8
of 15 observations skipped entirely**, at ~0.6 ms each versus ~60 ms.

Implementing this meant hand-writing the DXGI COM interfaces via `comtypes`
(`IDXGIObject` → `IDXGIOutputDuplication` → `IDXGIOutput1` → `IDXGIAdapter` →
`IDXGIDevice`), since no Python binding exposes dirty rects. Two instructive
failures on the way:

1. **Access violation.** `D3D11CreateDevice` had no declared `argtypes`, so
   ctypes truncated every pointer to a C int. Same root cause as §1.7 — on
   64-bit Windows, undeclared ctypes signatures are a memory-safety bug, not a
   style issue. Also, I had raw-cast between unrelated COM interfaces; COM
   requires `QueryInterface`.
2. **Everything reported "unavailable".** `comtypes` raises `COMError`, which
   is **not** an `OSError`. `DXGI_ERROR_WAIT_TIMEOUT` — which arrives on every
   single idle poll and is the *normal, expected* signal for "nothing changed"
   — was being caught by the generic handler and treated as a fatal error.

**The deliberate limitation.** The compositor is used *only* as a fast
negative. When it says something changed, the regions still come from hashing
the captured frame. The two are measured over slightly different intervals —
the capture happens after the poll — so compositor rectangles can under-report
relative to the pixels actually captured. Under-reporting means a changed
region never gets re-read, which is exactly the silent text loss the invariant
in §2.3 exists to prevent. Hashing is measured against the very frame being
analysed, so it cannot disagree with it.

This trades a further ~7 ms of hashing for a guarantee. That is the right trade
for a tool whose failure mode is "the agent clicks the wrong thing".

### 2.6 The resolution cascade

Element lookup tries rungs in increasing cost order and stops at the first that
answers, so cost tracks how *novel* the request is rather than how large the
screen is.

| Rung | Method | Cost | Why it is where it is |
|---|---|---|---|
| 0 | Already in the screen model | **0.055 ms** | No I/O at all |
| 1 | Rescan what changed | ~33 ms | The common case after acting |
| 2 | Accessibility tree | ~40–480 ms | Knows a Button *is* a button |
| 3 | UIA `TextRange.FindText` | ~490 ms | Exact; immune to font/DPI/antialiasing |
| 4 | Full-screen OCR | ~200 ms | Last resort |

Rung 3 sits below rung 4 despite being *more expensive* because ordering is by
"try this first", and exactness is worth more than 300 ms when the cheap rungs
have already failed. It is the rung you trust, not the rung that is fast.

Rung 3 is also the answer to a question worth raising: **OCR solves the wrong
problem.** OCR is open-vocabulary recognition — "what does this say?" — but an
agent almost always already knows the string and needs "*where* does it say
Save?". That is localisation of a known string, a far easier problem. UIA's
`FindText` does exactly this against the application's own text buffer. A
literature survey found **no prior art for this reframing in GUI agents**.

---

### 2.7 Reading pixels from the frame we already hold

Once §2.5 was in place, screen capture was the largest remaining cost per
observation. But the compositor path already *acquires a frame* — the pixels
were sitting on the GPU while a second, independent `mss` grab fetched the same
image again.

`IDXGIOutputDuplication::MapDesktopSurface` would give direct CPU access with
almost no code, but it only works when the desktop image lives in system
memory. Checked on this machine:

```
DesktopImageInSystemMemory : False
MapDesktopSurface          : DXGI_ERROR_UNSUPPORTED (0x887A0004)
```

So the full path is required: `QueryInterface` the acquired resource to
`ID3D11Texture2D`, create a CPU-readable staging texture, `CopyResource` into
it on the GPU, `Map` it, and read BGRA rows honouring `RowPitch` (which is
wider than the image, so the buffer cannot be handed to PIL unmodified).

That means `ID3D11Device::CreateTexture2D` (vtable slot 2) and
`ID3D11DeviceContext::Map`/`Unmap`/`CopyResource` (slots 7, 8 and **40**, after
the four `ID3D11DeviceChild` slots). COM dispatches by vtable offset, so every
preceding method must still occupy its slot — forty-odd placeholders. These are
generated in a loop rather than hand-written, because one silently misplaced
entry is memory corruption rather than an error.

**Result: 1.5–2.3× faster than `mss`**, measured across runs (39.8 ms vs
92.6 ms in one, 54.9 ms vs 83.9 ms in another). Verified correct by diffing
against an `mss` frame of the same screen: **0.003% of pixels differed**, which
is live screen change between the two captures.

**A limitation worth stating.** A frame only exists if the compositor actually
presented one. On a fully idle screen `poll()` reports "nothing changed"
without acquiring anything, so `capture()` returns None and the caller falls
back. This is harmless — an unchanged screen does not need re-reading — but it
means the DXGI capture path helps exactly on the observations that do work, and
never on the ones that skip.

**A correctness guard.** Desktop Duplication output 0 is the *primary monitor*.
`mss` monitor 0 is the *entire virtual desktop*. On a multi-monitor setup those
are different images, and silently substituting one for the other would put
every derived coordinate in the wrong place. `capture_frame()` therefore takes
an `expected_size` and refuses to return a frame that does not match.

### 2.8 Why the numbers move so much

Absolute timings in this log vary by up to 3× between runs. Two causes, both
worth understanding:

- **OCR cost scales with screen *content*.** Full-screen OCR measured 197 ms on
  a quiet desktop and 592 ms with a dense web page open. The incremental path
  scales with *change* instead, so the busier the screen, the larger the gap.
  End-to-end, the same comparison came out at 6.5× on a quiet screen and
  **14.3×** on a busy one.
- **Machine load.** One run recorded a UIA tree walk at 3,410 ms while
  benchmarks were competing for the CPU.

This is why ratios are reported from within a single run rather than mixing
absolute numbers across runs, and why `benchmarks/` exists: re-measure rather
than trusting these figures.

### 2.9 Remembering screens between visits

Applications are deterministic. The Save dialog looks the same every time it
opens. Yet the agent re-read all of it on every visit, and again in the next
session, because nothing remembered what was learned.

The atlas stores a screen's layout, keyed by a structural signature, and reuses
it on the next visit: **125 ms cold read → 1.4 ms warm recall, 89× cheaper**,
with a 5/5 hit rate on a live desktop and correct rejection of blank, inverted
and wrong-application screens.

The design splits into two parts doing different jobs:

- **The signature decides which remembered screen might apply.** It is a
  downsampled edge map, capturing *arrangement* rather than content. Measured on
  a live desktop: an idle screen drifts 0.0000 between frames, while a
  structurally different image sits at 0.379. Huge separation, so the tolerance
  is not delicate.
- **Verification decides whether it actually does.** Because at 48×27 two
  screens with the same arrangement but different words look identical — which
  is not a defect, it is the division of labour.

**Two failed approaches, both instructive.**

*Verifying by re-reading text does not work.* The obvious design is to OCR a few
remembered elements and compare strings. It fails, because **OCR output is not a
stable identity**. Segmentation depends on the crop it is given (§2.3:
full-frame and crop OCR agree only ~91%), and small on-screen text is often
garbled — real stored labels from this machine included `'Elevatc'`, `'Con tir'`
and `'Subarr&'`. Comparing one garbling against a differently-cropped garbling
rejected screens that were perfectly intact. Worse, tight crops around small
text returned *nothing at all* until padded past 64 px.

The fix was to stop asking what a region says and ask whether it still looks the
same. Comparing pixels costs no OCR, cannot be confused by segmentation, and is
exact.

*Mean absolute difference is the wrong metric for that comparison.* A small
piece of text in a mostly-blank region barely moves the mean, so a changed
heading in a wide box scored 1.47 against 0.00 for an identical one — far too
close to threshold safely. Counting *how many cells changed* is not diluted by
the blank area around the change:

| Case | mean | cells changed |
|---|---|---|
| identical | 0.00 | **0.000** |
| noise elsewhere on screen | 0.00 | **0.000** |
| heading changed (wide box) | 1.47 | **0.020** |
| heading changed (tight box) | 12.79 | **0.180** |
| a row moved 25 px | 30.76 | **0.310** |

That table also shows why verifier regions are capped in width: a tight box
around the change scores nine times higher than a full-width one.

**Failing closed.** A stale layout that gets used is the worst outcome this
system can produce — the agent clicks somewhere arbitrary. So every unexpected
condition is a rejection: no verifiers, a region that no longer fits on screen,
a comparison that cannot be made. A screen with nothing verifiable is *not
remembered at all*, since it could only ever be trusted blindly. A failed recall
costs one signature plus a few tiny image comparisons — negligible against the
full read it was trying to avoid.

### 2.10 CI caught the bug I had just fixed

The first run of the test workflow failed on Linux with
`ModuleNotFoundError: No module named 'mcp.server.fastmcp'` — precisely the
breakage §1.1 exists to prevent.

The cause: to keep CI fast, the Linux job installs with `pip install -e .
--no-deps` and then lists the light dependencies by hand. `--no-deps` means
`pyproject.toml`'s `mcp[cli]>=1.0,<2` is never applied, and the hand-written
list said `"mcp[cli]"` with no bound. The constraint existed, was correct, and
was bypassed by the very pipeline meant to verify it.

Worth remembering as a general shape: **a constraint expressed in one place and
re-stated in another will drift**, and the re-statement is usually in
infrastructure nobody re-reads. The same run also showed two OCR tests asserting
that a backend exists, which is false by construction on a runner where OCR is
deliberately not installed; they now skip.

### 2.11 Not looking at all

Perception had been made cheap. The next step was making it unnecessary.

Two findings drove this, and the first was a surprise.

**The largest cost in the loop was not perception — it was sleeping.** Every
action tool ended with `time.sleep(0.3)`, a fixed wait chosen for the slowest
case, so every action paid the worst case whether or not anything took that
long. With perception down to ~45 ms, that sleep was six times the cost of the
work around it.

The compositor already knows when the screen is changing, so the wait can end
when the interface actually settles:

```
fixed sleep previously used per action : 300.0 ms
median time the screen was changing    :   0.0 ms
median actual wait                     :  61.5 ms
saved per action                       : 238.5 ms   -> 11.9s over 50 steps
```

Defining "settled" took a correction. The first implementation waited for *no*
change and timed out on every single sample, because a real desktop is never
still: measured while idle, a blinking caret and a ticking clock produce a
change event roughly every 18 ms, covering a median of 32 pixels. Genuine UI
changes cover tens of thousands. So the criterion is "nothing *large* recently",
with the threshold at 4,000 px — far above that noise floor, far below any real
change.

**The second finding is that a known action does not need observing at all.**
Applications are deterministic: clicking Save produces the same dialog every
time. After the first observation the outcome is already known, so it is enough
to *confirm* the expected screen rather than read it again. Measured, prediction
is **19–23× cheaper than observing** (2.3 ms versus 43–50 ms).

This is speculative execution applied to perception, and it needed no new
machinery — the atlas (§2.9) confirms a screen by pixels, and the compositor
(§2.5) says when to look. What it added was a transition model:
`(screen, action) → outcome`, learned by watching.

**Three safeguards, each from a failure the tests found.**

*Transitions must be seen twice before being trusted.* One observation could be
a coincidence.

*Transitions that prove wrong are retired.* A transition predicted wrongly more
often than rightly is worse than none, so it stops being used.

*Prediction re-checks the layout, not just the sampled regions.* The first
version went straight to the expected screen and ran only the pixel verifiers,
skipping the layout-signature check that `recall` does. A change falling outside
every sampled region therefore went unnoticed. A test caught it; prediction now
runs both checks, the same pair the atlas uses.

**And a limit that cannot be engineered away.** Verification proves the *layout*
is the one expected — the same controls in the same places. It does not prove
every character is identical, and it cannot. A single changed digit alters fewer
pixels than a blinking caret does:

| Whole-screen grid | a caret appears | a digit changes |
|---|---|---|
| 64×36 | 0.00043 | 0.00000 |
| 128×72 | 0.00033 | 0.00000 |
| 256×144 | 0.00022 | 0.00000 |
| 320×180 | 0.00017 | 0.00003 |

The signal is *inverted* at every resolution tried, so no threshold separates
them. I spent several iterations trying to tune my way out before accepting it
was structural. Rather than pretend otherwise, the guarantee is stated for what
it is: a confirmed prediction means the screen is safe to act on, not that
volatile text such as a clock or a counter is current. `observe(force_full=True)`
is the escape hatch, and the behaviour is pinned by a test that asserts the
limitation rather than hiding it.

**A failed prediction is information.** It means the interface did something it
does not normally do — an unexpected dialog, an error, a slow load — so it is
reported to the agent as a `surprise` rather than silently absorbed as a cache
miss.

---

### 2.12 The metric everything else was a proxy for

Every measurement up to this point was perception *cost*. Cost is a proxy. The
metric that matters for a GUI agent is whether it finishes the job — and a
cheaper perception path that quietly degraded accuracy would be worse than no
optimisation at all.

That was an unexamined assumption for eight versions. `benchmarks/bench_tasks.py`
exists to examine it: scripted tasks driven through the real MCP tool surface,
across four configurations from v0.4-style full-screenshot perception to memory
plus prediction, verified against **Calculator's own state via UI Automation**.
Grading OCR with OCR would only establish that it agrees with itself.

**Result, 60 runs (5 per task per configuration):**

| configuration | passed | median | tokens |
|---|---|---|---|
| v0.4-style (full screenshot, no memory) | 15/15 | 7.2 s | 170,690 |
| delta only | 14/15 | 6.9 s | 8,622 |
| delta + memory | 15/15 | 6.8 s | 9,052 |
| delta + memory + prediction | 15/15 | 6.9 s | 12,916 |

**59/60. Accuracy is flat across every configuration; token cost falls 19.8×.**
That is the claim the previous eight versions of work were making on credit, and
it is now paid for: cheaper did not mean worse.

The single failure was not a perception failure, and the harness said so —
Calculator itself showed `49` after being driven `4 + 5 =`, so a XAML button
dropped an invoke. The task checks the application's own state *before* grading
perception precisely so that a dropped click cannot be reported as a perception
defect.

Two honest caveats. **Wall-clock barely moved** (7.2 s → 6.9 s) because these
tasks are dominated by Calculator's ~3 s launch and the deliberate 0.35 s settle
between clicks; perception is a small part of the total, and the win is in tokens
and in the per-observation latency measured in §2.4. And **memory and prediction
do not pay for themselves here** — they amortise over repeat visits, and a
handful of short novel tasks contains none. Reporting that plainly is more useful
than a benchmark shaped to flatter the newest feature.

Getting to that number required fixing a bug that only a full-system run could
expose (§2.13). Building the harness produced three more findings.

**Windows OCR cannot see isolated digits.** Pointed at Calculator it returned 30
text elements — `DEG`, `MC`, `Function`, `Trigonometry`, `log` — and **not one
digit**. Not misread: absent. Text recognisers are trained on words and lines,
and a lone glyph on a button has no line context to belong to. The cascade now
routes queries of one or two characters to the accessibility tree first, because
for those the pixel rungs are not a cheaper path to the same answer, they are a
guaranteed miss followed by the accessibility rung anyway.

**The label a human reads is not the label a machine exposes.** The button a
person sees as "7" is named `Seven`. An agent reasoning from a screenshot asks
for the wrong string, and no perception work fixes it — that needs a synonym
layer, or an agent told to use accessible names. It is a whole class of failure
that sits outside the problem I had been optimising.

**Benchmarks need repeats before they are evidence.** The first version ran each
task once, and reported flapping results: a configuration would "fail" on one
run and pass on the next, because a XAML button occasionally does not process an
invoke before the next one lands. A single sample cannot distinguish "this
configuration is worse" from "that click did not register". Three repeats and a
settle delay turned a noise generator into a measurement.

Three was still not enough. One sweep reported **24/36** with one task failing in
all four configurations — a clean, systematic-looking signal that I spent real
time chasing as a cascade defect. The next sweep, with no change to the code
under test, reported **36/36**. The failure had been environmental. The lesson is
not "add repeats" but *a result that looks systematic can still be noise, and the
way to find out is to reproduce it before explaining it* — I built a whole
hypothesis about stale coordinates before checking whether the finding was real.
The repeat count is now an environment variable, so a load-bearing number can be
re-run at higher N instead of trusted at three.

**A safety decision worth recording.** Notepad was the obvious second subject,
until launching it restored a document with unsaved changes belonging to the
machine's owner. A benchmark has no business anywhere near that, so the subject
list is Calculator alone — stateless, so opening and closing it cannot lose
anyone's work.

**And a self-inflicted wound.** A patch script opened the harness with
`open(path, "w")`, which truncates immediately, then raised before writing. The
file was left at zero bytes. `atlas.save()` writes to a temporary file and
renames it precisely so an interrupted save cannot do this; the throwaway script
did not, because it was throwaway. Small tools deserve the same care as the code
they edit.

---

### 2.13 The bug that every isolated benchmark passed

Running the full sweep surfaced a line in the logs that no component test ever
produced:

```
Desktop Duplication unavailable (COMError: (-2147024809, 'The parameter is
incorrect.', ...)); using tile hashing
```

`E_INVALIDARG`, once per configuration, on a machine where I had personally
measured Desktop Duplication working at 0.14 ms (§2.6). The fallback did its job
— nothing broke — which is exactly why it had gone unnoticed.

Probing it in isolation, duplication worked. Probing it after importing the
server, it failed. The decisive experiment was to build two sources in one
process:

| | result |
|---|---|
| first instance | succeeds |
| second instance, while the first is alive | **E_INVALIDARG** |
| new instance, after closing the first | succeeds |

**Windows grants one Desktop Duplication per output per process.** oswright built
two: one for settle detection, one for the screen model. Whichever touched the
compositor first won, and the other silently spent the entire project running on
tile hashing.

So the compositor path I designed, hand-wrote COM bindings for, benchmarked at
0.14 ms and wrote up in §2.6 **was half disabled in the actual server**. Every
component was correct alone. The defect lived in the composition, and the only
thing that could see it was a full-system run — which is the argument for
`bench_tasks.py` existing, made better than I could have made it deliberately.

The fix is to model the resource the way the OS actually grants it: one per
process, borrowed rather than owned, reference-counted, keyed by thread as well
as output because a duplication belongs to the thread that created it.

```python
def acquire_shared(output_index: int = 0) -> "DxgiDirtySource":
    key = (threading.get_ident(), output_index)
    with _shared_lock:
        source = _shared_sources.get(key)
        if source is None:
            source = DxgiDirtySource(output_index)
            source._shared_key = key
            _shared_sources[key] = source
        _shared_refs[key] = _shared_refs.get(key, 0) + 1
    return source
```

The measured effect was larger than the fix looked. With the compositor actually
answering, an unchanged screen is confirmed authoritatively instead of being
re-examined, so observations stop producing payload:

| | before | after |
|---|---|---|
| tokens, delta-only, 15 runs | 23,584 | **8,622** |
| total reduction vs v0.4-style | 7.2× | **19.8×** |

A resource-ownership bug worth 2.7× in tokens, invisible to 221 passing tests and
to every benchmark that measured one component at a time.

Two things now guard it. A test asserts that two trackers share one source and
that the refcount returns to zero on release — a *test*, not a benchmark, because
the failure mode is silent degradation rather than a wrong answer. And the
docstring on `DxgiDirtySource` now says the constraint out loud, since the class
is impossible to use correctly without knowing it.

**The general lesson:** a fallback that hides a defect is worse than no fallback
unless something is watching whether it fires. Graceful degradation without
observability is just a bug with good manners.

---

### 2.14 One application was hiding three bugs

Every task number up to here came from Calculator. It is XAML, stateless, fully
exposed to accessibility, and it always reopens in the same position — the
easiest surface Windows has. "Accuracy is flat across configurations" rested
entirely on it.

Three more subjects went in behind one `Subject` interface: **File Explorer** (a
native Win32 list view), **Chrome** (web content), and **VS Code** (Electron).
Each needed ground truth that the perception layer has no hand in. Explorer is
graded through UI Automation; Chrome and VS Code are graded through the **window
title**, with the browser fixture reporting what was clicked by setting
`document.title`. That trick is what makes it possible to grade web and Electron
content without grading OCR with OCR.

The corpus broke three claims within its first run.

**A literal search cannot find text the recogniser mangled.** Asked for
`bravo_notes.txt`, Windows OCR returned `bravo notes.b(t` — the underscore
rendered as a space, the `x` as `(`. The text was plainly on screen, OCR had
read it, and the task still failed. Separators are the usual casualty: thin,
low-contrast, routinely dropped. Approximate matching is now a fallback used
only after a literal search finds nothing, so it cannot change an answer that
already worked, and it **refuses when two candidates score alike** — `alpha`,
`bravo` and `charlie` variants of the same filename must not collapse into one
answer. New subjects went from **5/12 to 11/12**.

**Explorer does not show the filename you wrote to disk.** It hides known
extensions, so the label is `meeting_notes`, and whether it does is a per-machine
setting. The benchmark now resolves the target from the live window instead of
hardcoding it — a benchmark that assumes its own fixtures are what the screen
shows is measuring the wrong thing.

**An entire IDE exposes 18 accessibility elements.** Probing VS Code returned
`Minimize`, `Maximize`, `Restore`, `Close`, and one node named **`Chrome Legacy
Window`** containing the whole interface. Not a shallow tree — an opaque one.
OCR read 94 elements from the same frame, including every filename. This is the
sharpest statement of the Electron blind spot I have, and it needs no benchmark
harness to reproduce.

---

### 2.15 The benchmark was reading its own output

The most valuable failure came from the corpus, and it was mine.

`arithmetic by label` began failing 0/3 in **every pixel configuration** while
passing 9/9 in the accessibility-only one. That shape — one perception path
failing everywhere, another passing everywhere — is not a flake. The click trace
said why:

```
Seven@r2(898,796)  Multiply by@r2(1133,795)  Eight@r1(1587,452)  Equals@r2(1133,936)
```

`Eight` resolved to x=1587. The Calculator window spans x 767..1185. The agent
had clicked the word "Eight" **in my own terminal**, which was displaying the
benchmark's output — including the labels it was searching for. The benchmark
had become part of the screen it was measuring, and it fed itself: once a
failure printed those labels, the next run found them.

The underlying defect is oswright's, not the benchmark's. `window_title`
constrained only the accessibility rungs; the pixel rungs read the whole screen
and returned whatever they found first. The docstring said exactly that and was
accurate — but the effect is that **an automation tool clicks an application the
caller did not ask for**, which is a correctness problem before it is a
perception one. The pixel rungs are now filtered to the named window's
rectangle, and when no window is named nothing is filtered, because filtering
against an unknown rectangle would discard every correct answer.

After the fix, all four cascade configurations return **15/15**.

**And a correction I want on the record.** Earlier in the same session I saw a
click land at (600, 66) for a file that was at (211, 157), concluded the screen
model was serving stale coordinates, and built a currency check for it. That
check is sound and I kept it — answering from memory that nothing has verified
is indefensible, and the atlas had verified its answers from the start while the
model never did. But (600, 66) turned out to sit inside my own maximised session
window. **The evidence I cited was the scoping bug, not staleness.** I fixed a
real weakness for the wrong reason, and only found out because a later fix made
the symptom disappear. Both fixes stand; the reasoning that produced the first
one does not.

---

### 2.16 Measuring the argument instead of making it

The architectural claim has always been that **neither pixels nor accessibility
wins everywhere, so route per query**. That is precisely the bet other Windows
GUI agents take the other way, with accessibility-only designs. It had been
argued for six versions and never measured.

Two ablation configurations now isolate each half — `accessibility only`, which
is the competing posture, and `pixels only`.

| configuration | Calculator | File Explorer | Chrome |
|---|---|---|---|
| full cascade (4 variants) | **9/9** | **3/3** | **3/3** |
| accessibility only | 9/9 | **0/3** | **0/3** |
| pixels only | **6/9** | 3/3 | 3/3 |

Each single-mode posture is blind exactly where the design predicted, and for
reasons that are legible rather than statistical:

- **Accessibility-only is blind outside XAML.** It is perfect on Calculator and
  scores zero on Explorer's list view and on web content. On VS Code it also
  scores zero, against an interface it can only see as one opaque node.
- **Pixels-only fails on Calculator's buttons**, because the button a human
  reads as `7` is *named* `Seven`, and OCR returns no digits from Calculator at
  all. The label a person sees and the label a machine exposes are different
  strings, and no amount of perception engineering reconciles them.

The cascade is the only configuration that passes everywhere. That is the first
evidence for the central design decision that is not an argument — and it was a
genuine risk to run, because a result showing accessibility-only matching the
cascade everywhere would have meant the simpler competing design was right and
this one is over-engineered.

---

### 2.17 The comparison I had been avoiding

Asked directly whether oswright was better than Windows-MCP, the honest answer
for six versions was *no idea* — every benchmark compared oswright to earlier
versions of itself. That is the comparison that flatters, and it is not the one
a person choosing a tool cares about.

The gate was set before starting: **can Windows-MCP be driven tool-by-tool,
without an LLM and without credentials?** If not, abandon — a comparison that
cannot be reproduced from a script is not evidence. It passed: it requires
Python 3.14 (installed alongside, not over, the existing interpreter), auth is
optional and applies only to HTTP, and over stdio it exposes 20 tools.

Reading its tool schemas showed the two designs differ **mechanically**, not by
tuning:

- **Windows-MCP** returns the screen to the agent — `Snapshot` renders the
  accessibility tree as `(x,y) button "Seven" [action: click]` — and takes
  coordinates back through `Click`. The description of the screen is charged to
  the model's context on every action.
- **oswright** takes the text and returns the outcome: `click_element(text=
  "Seven")` resolves internally through the cascade.

Both were run on the same task, the same four buttons, graded by Calculator's
own UI Automation display. Neither tool grades itself. Windows-MCP ran with its
own defaults, and I measured **its best case as well as its prescribed one**,
because a comparison that only reports the unflattering configuration is an
advertisement.

| | passed | tokens | steps | seconds |
|---|---|---|---|---|
| oswright | 3/3 | **419** | 4 | **2.7** |
| Windows-MCP, snapshot per action | 3/3 | 8,048 | 8 | 5.6 |
| Windows-MCP, snapshot once | 3/3 | 2,033 | 5 | 4.2 |

**19.2× less context than its documented loop, 4.9× than its cheapest possible
use, at half the wall-clock, with both tools correct.**

Two things make that honest rather than triumphant. Snapshotting once is only
safe because Calculator does not move between clicks — an agent that assumes
that in general is acting on stale coordinates, which is exactly the
unsoundness oswright had at rung 0 and had to fix, so the 4.9× is measured
against a configuration that is not generally safe. And this is one task on one
application: it says nothing about robustness across a corpus, nor about the
product surface Windows-MCP has and oswright does not.

**The mistake worth recording.** The first run reported Windows-MCP failing with
the display showing `9,999`. I had passed `label=N` to `Click`, having read the
schema and not the output format — their labels are for annotated screenshots,
while the default tree gives coordinates. My adapter drove the wrong buttons and
the harness recorded it as *their* defect.

It was caught because `9,999` is not a plausible result of pressing 7 × 8, and
because a fairness rule written down before the run said their failures had to
be reported — which made me look at one instead of accepting it. **When you
build the apparatus that measures your competitor, every bug in it defaults to
their disadvantage.** That asymmetry does not announce itself; the only defence
is deciding in advance what a fair run looks like.

---

## Part 3 — Results

Measured over a 14-step agent loop at 1920×1080:
| | v0.4.0 | v0.5.0 |
|---|---|---|
| Median latency per step | 212 ms | **33 ms** (6.5×) |
| Tokens per observation | ~2,764 | **~49** |
| Tokens over 14 steps | 38,696 | **3,930** (10×) |
| Lookup of known text | 318 ms | **0.055 ms** (5,800×) |
| Action payload over MCP | 194,935 B | **597 B** (326×) |

A 50-step task returning a screenshot per action costs ~138,200 image tokens.
That is the number the design is aimed at.

---

## Part 4 — Things I got wrong, and dead ends

Worth knowing, because these are the questions an interviewer will ask.

1. **Assumed capturing dirty regions would be cheaper.** It is 2× *worse*
   (§2.4). Fixed per-grab cost dominates; area is irrelevant.
2. **Assumed the accessibility tree would be the fast path.** It is often
   slower than OCR and is blind on Electron (§2.2). The literature says
   otherwise; the machine disagreed with the literature.
3. **Assumed a 92% recall figure meant the model was losing text.** Mostly it
   was OCR segmentation instability, which a control experiment isolated
   (§2.3). Without the control I would have spent the time optimising the wrong
   thing.
4. **Wrote a perceptual hash for the OCR cache.** A perceptual hash is designed
   to collide on similar images — precisely wrong for a cache, where a collision
   serves stale results for a screen that genuinely changed. Replaced with an
   exact digest.
5. **Made `find_text` unbounded.** A terminal with a large scrollback took
   5.2 s. A rung in a cascade must fail fast, so it now has both a match cap
   and a wall-clock budget.
6. **Ruff wanted to rewrite 213 `Optional[X]` annotations to `X | None`.**
   Declined: pure churn across the entire MCP tool surface, with a non-zero
   chance of perturbing generated schemas, for zero functional gain. Rule
   disabled with a written reason rather than silently ignored.

7. **Tried to verify a cached screen by re-reading its text.** OCR output is not
   a stable identity — its segmentation depends on the crop, and small text
   comes back garbled differently each time (§2.9). Verification had to move
   from "what does this say?" to "does this still look the same?".
8. **Used a mean absolute difference to compare regions.** It dilutes a small
   real change across a large blank area (§2.9). Counting changed cells does
   not.
9. **Let CI bypass a dependency constraint** by re-stating it without the bound
   (§2.10). The pin was right; the pipeline verifying it was not.

10. **Waited a fixed 300 ms after every action** while optimising perception
    down to 45 ms. The sleep was six times the cost of the work around it
    (§2.11). Worth checking what actually dominates before optimising.
11. **Defined "settled" as "no change"**, which never happens on a real
    desktop — the first implementation timed out on every sample (§2.11).
12. **Let prediction skip the layout check** that recall performs, so a change
    outside every sampled region went unnoticed (§2.11). A test caught it.
13. **Tried to tune my way out of a structural limit.** No whole-screen
    resolution can distinguish a changed digit from a blinking caret; the
    signal is inverted at every grid tried (§2.11). The right move was to state
    the guarantee accurately instead.

14. **Optimised a proxy metric for eight versions without checking it.** All the
    perception work targeted cost; the metric that matters is task success
    (§2.12). The harness that checks it was the last thing built, not the first.
15. **Wrote a benchmark with no repeats.** It flapped, reporting a dropped click
    as a configuration difference (§2.12). One sample is not a measurement.
16. **Truncated a file with `open(path, "w")` in a throwaway patch script** that
    then raised, leaving it empty (§2.12). The production code writes to a
    temporary file and renames precisely to prevent this.

17. **Built two owners for a resource the OS grants once per process.** Desktop
    Duplication was silently half-disabled for the entire project because the
    second `DirtyTracker` lost the race and fell back to hashing (§2.13). Every
    component passed alone; only a full-system run could see it.
18. **Shipped a fallback with no observability.** The degradation logged at
    `INFO` and was drowned in benchmark output for weeks. A fallback nothing
    watches is a bug with good manners (§2.13).
19. **Believed a systematic-looking failure without reproducing it.** A 24/36
    sweep with one task failing in all four configurations read as a real
    defect; the next identical sweep returned 36/36 (§2.12). I built the
    hypothesis before confirming the finding.

20. **Trusted one application to stand for all of them.** Every accuracy claim
    rested on Calculator, the easiest surface Windows has, and three of the
    four claims broke within one run of a wider corpus (§2.14).
21. **Let `window_title` mean one thing in the docs and another in effect.** It
    constrained only the accessibility rungs, so the pixel rungs could click a
    different application entirely (§2.15). Accurate documentation of wrong
    behaviour is still wrong behaviour.
22. **Let the benchmark become part of the screen it measured.** Its own console
    output contained the labels it was searching for, and the agent clicked them
    (§2.15). A measurement apparatus that is visible to the thing it measures is
    not neutral.
23. **Diagnosed a real weakness from the wrong evidence.** I attributed a
    misplaced click to a stale screen model and built a currency check for it;
    the coordinate turned out to be inside my own window (§2.15). The fix was
    worth keeping and the reasoning was not, which is an uncomfortable pair to
    hold at once.
24. **Wrote a test that pinned a bug in place.** `test_model_hit_costs_nothing`
    asserted only that the fastest rung was fast, and said nothing about whether
    its answer was right, so the unsound behaviour it guarded survived every
    run (§2.15). A test that encodes an optimisation without its precondition
    protects the optimisation from being corrected.
25. **Guessed at a cause and started implementing before measuring.** I decided
    low-contrast verifier regions explained a false prediction and began writing
    an entropy threshold; measuring first showed those regions score 31–62
    standard deviation, nowhere near uniform. The hypothesis was wrong and the
    measurement took four minutes.
26. **Drove a competitor's tool wrong and nearly published it as their defect.**
    I passed `label=N` to Windows-MCP's `Click` after reading its schema and not
    its output; it clicked the wrong buttons and the harness recorded `9,999` as
    a Windows-MCP failure (§2.17). When you build the apparatus that measures
    someone else's tool, every bug in it defaults to their disadvantage.

Approaches investigated and rejected on evidence:

- **Hooking DirectWrite/GDI to recover text from the render path.** This is
  what game text-hookers and old screen readers did, and it would give perfect
  text at zero recognition cost. Blocked on modern Windows by Arbitrary Code
  Guard and Control Flow Guard on exactly the hardened apps that matter, and it
  requires DLL injection. Not viable as a general solution.
- **Screen-reader "off-screen model" via display-driver interception.** Dead
  since DWM; there is no longer a video path to intercept.
- **Rendering the query string and template-matching it** (query-by-string
  word spotting). Sound in principle and unexplored in GUI agents, but degrades
  badly across unknown fonts, ClearType subpixel rendering, and DPI scaling.
  UIA `FindText` achieves the same goal exactly, where it is available.

---

## Part 5 — What is deliberately not done

- **Wayland input injection**, and macOS `AXTextMarker` as a `TextPattern`
  equivalent.
- **Transitions keyed on more than the immediately preceding screen.** Some
  actions depend on state that is not visible, and a one-step model cannot
  represent that; it currently shows up as a transition that stops being
  trusted.
- **A vision-model rung** for surfaces that are neither accessible nor
  text-legible: games, canvases, image editors.

---

## Part 6 — Questions worth being able to answer

- *Why not just use the accessibility tree like everyone else?* — §2.2. Bring
  the VS Code number: 18 elements.
- *How do you know the incremental model isn't silently losing text?* — §2.3.
  The invariant, and the test that asserts a region grew to cover what it
  invalidated (`test_invalidated_elements_are_fully_rescanned`).
- *Isn't a dirty-rectangle cache just a VNC trick from the 1990s?* — Yes, and
  that is the point: it is well-understood technology that no published GUI
  agent applies. The novelty is the application, not the mechanism.
- *What breaks this?* — A screen that changes constantly (video playback,
  animations) degrades to full rescans; the `FULL_RESCAN_THRESHOLD` exists to
  make that degradation graceful rather than pathological. Applications that
  are neither accessible nor text-legible (games, canvas) still need the VLM
  rung, which is not implemented.
- *Why is TextPattern below OCR in the cascade if it is exact?* — §2.6.
  Ordering is by "try first", not by quality.
- *How do you know a cached screen is still valid?* — §2.9. Pixels, not text,
  and it fails closed. Bring the table showing why mean difference was the wrong
  metric.
- *Is this better than Windows-MCP or the other GUI agents?* — Not as a
  product: they have OAuth, analytics, virtual-desktop management, a watchdog,
  a vendored UIA library and actual users. Perception is where this wins, and
  it is measured (§2.2, §2.11). Task success across the two has not been
  compared, and saying otherwise would be a claim without evidence (§2.12).
- *What would settle that?* — Running `bench_tasks.py` against both, on the
  same tasks. The harness exists; the comparison does not.
- *What does a confirmed prediction actually guarantee?* — §2.11. The layout,
  not every character. Bring the table showing the signal is inverted, and the
  test that pins the limitation rather than hiding it.
- *What is the single biggest remaining cost?* — OCR of the changed regions,
  on the observations that still happen at all.
- *Your numbers vary by 3× between runs — why should I believe them?* — §2.8.
  Because they are ratios measured within a run, and because `benchmarks/` is
  in the repository so you can re-measure.
