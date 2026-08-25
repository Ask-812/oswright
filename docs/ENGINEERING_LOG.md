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

- **Per-application UI atlas.** Applications are deterministic: the same dialog
  has the same layout every time. Caching layouts across sessions, keyed by a
  content-addressed fingerprint, would make repeat visits nearly free. Not in
  the literature.
- **Speculative perception.** With an atlas and a cheap change oracle (which
  now exists, §2.5), an agent could predict the post-action screen state and
  verify the prediction rather than re-perceiving. Correct predictions would
  cost nothing. This is predictive coding applied to GUI agents; the premise
  (change is sparse) is measured, the transition model is not built.
- **Wayland input injection**, and macOS `AXTextMarker` as a `TextPattern`
  equivalent.

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
- *What is the single biggest remaining cost?* — Screen capture, at ~33–48 ms.
  Reading pixels from the DXGI texture already acquired in §2.5, instead of a
  second `mss` grab, is the obvious next move.
