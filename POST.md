# OSWright — Playwright-like automation, but for your entire desktop

Playwright MCP lets AI agents control browsers. But what about everything else — native apps, system dialogs, legacy software, game UIs?

**OSWright** is an open-source MCP server that gives AI agents full control over your desktop using OCR and image matching. It sees your screen the way you do — by reading the text and recognizing visual elements.

## How it works

1. Agent takes a screenshot to see the current screen
2. Finds buttons, labels, and fields by their visible text (OCR) or by matching template images
3. Clicks, types, scrolls, drags, fills forms
4. Every action returns a fresh screenshot so the agent always has context

## Use cases

- Automating legacy enterprise apps that have no API
- RPA-style workflows across multiple desktop applications
- Testing native desktop software with AI
- Letting AI agents navigate OS-level dialogs (file pickers, installers, system settings)
- Filling forms across any application
- Building AI assistants that can actually use your computer
- Managing windows, clipboard, and launching apps programmatically

## What makes it different

- Works with **any application**, not just browsers
- **Cross-platform:** Windows, Linux, macOS
- **Fast OCR on Windows** — uses built-in Windows OCR (instant, zero model download, no PyTorch) with EasyOCR fallback on Linux/macOS
- Two ways to use it: as an **MCP server** (for AI agents) or as a **Python library** (for scripts)
- Playwright-style Python API with auto-waiting locators and assertions
- **36 MCP tools** with auto-snapshot after every action
- **Window management** — list, focus, close, minimize, and screenshot specific windows
- **Clipboard access** — read and write system clipboard for data transfer
- **App launcher** — launch applications and wait for them to load
- **DPI- and multi-monitor-correct** coordinates, ready to click
- One-line install, zero config

## Install

```
pip install oswright
```

## MCP config

Works with Claude Desktop, VS Code, Cursor, Windsurf, Cline, Goose, and any MCP client:

```json
{
  "mcpServers": {
    "oswright": {
      "command": "uvx",
      "args": ["oswright"]
    }
  }
}
```

## Links

- **GitHub:** https://github.com/Ask-812/oswright
- **PyPI:** https://pypi.org/project/oswright/

---

## Version History

### v0.5.1 — Compositor-driven change detection

The Windows compositor already knows which pixels changed — it has to, in order
to present efficiently — and exposes it through DXGI Desktop Duplication.
Computing the same thing by hashing a captured frame is redundant work.

More importantly, `AcquireNextFrame` answers "did anything change?" **without
transferring any pixels**: 0.14 ms, against ~48 ms to capture a frame and then
discover it was identical. Most observations during an agent session are of an
idle screen, so the capture is skipped entirely.

| | v0.4.0 | v0.5.0 | v0.5.1 |
|---|---|---|---|
| Median latency per step | 212 ms | 71 ms | **33 ms** |
| Idle observation | ~212 ms | ~60 ms | **~0.6 ms** |

Measured live, 8 of 15 observations were skipped without capturing anything.

Implementing this meant hand-writing the DXGI COM interfaces via `comtypes`,
since no Python binding exposes dirty rectangles. Two failures worth recording:
`D3D11CreateDevice` without declared `argtypes` truncates pointers on 64-bit and
faults rather than returning an error; and `comtypes` raises `COMError`, which is
not an `OSError`, so `DXGI_ERROR_WAIT_TIMEOUT` — the *normal* signal meaning
"nothing changed", arriving on every idle poll — was being treated as fatal.

**Deliberate limitation.** The compositor is used only as a fast *negative*.
When it reports a change, regions still come from hashing the captured frame,
because the two are measured over slightly different intervals and compositor
rectangles can under-report relative to the pixels actually captured. An
under-reported region is text that never gets re-read, which is the exact
failure this design exists to prevent. Roughly 7 ms of hashing buys that
guarantee.

Also added: `benchmarks/` so every performance claim is reproducible, and
`docs/ENGINEERING_LOG.md` recording the reasoning, the measurements, and the
dead ends — including the discovery that capturing only dirty regions is *2×
slower* than one full-screen grab, because `mss` has a fixed ~16.6 ms per-grab
cost regardless of region size.

### v0.5.0 — Incremental perception

The expensive thing about a GUI agent is not clicking, it is looking. Every
published agent re-perceives the whole screen on every step: full screenshot,
full OCR, then hand the model a fresh image and let it work out what changed.

Measured on a live desktop, **the median observation changes 0.012% of pixels.**
A full rescan therefore does roughly 240× more work than the change warrants,
and the screenshot it returns costs ~2,800 image tokens whether anything
happened or not.

v0.5.0 keeps a model of the screen between observations and updates only what
moved.

**Measured over a 14-step agent loop on a 1920×1080 display:**

| | v0.4.0 | v0.5.0 |
|---|---|---|
| Median latency per step | 185 ms | **71 ms** |
| Tokens per observation | ~2,764 | **~84** |
| Tokens over 14 steps | 38,696 | **1,800** |
| Screen re-read per step | 100% | **11.5%** |
| Lookup of already-known text | 244 ms | **0.05 ms** |

**New**

- `observe` — returns what appeared and disappeared since last time, not a picture.
- `find_element` / `click_element` — resolution cascade; reports which rung answered.
- `read_model_text`, `perception_stats`.
- `--observation-mode delta` makes every action tool return a diff instead of a
  screenshot. Default stays `screenshot` for compatibility.
- `oswright/dirty.py`, `screenmodel.py`, `cascade.py`, `textprovider.py`.

**The cascade**

Element lookup stops at the first rung that can answer, so cost tracks how
*novel* the request is rather than how large the screen is: the screen model
(~0.05 ms) → an incremental rescan (~70 ms) → the accessibility tree (~40 ms) →
the application's own text buffer via UIA `TextRange.FindText`, which is exact
and immune to font/DPI/antialiasing (~400 ms) → full-screen OCR (~250 ms).

The ordering comes from measurement rather than theory. The usual advice is to
make the accessibility tree primary; on real applications it is not always
cheaper. Walking Chrome's tree took **537 ms** — slower than a full OCR pass —
and VS Code exposed only **18 elements** to it. Neither pixels nor accessibility
wins everywhere, which is exactly why this is a cascade and not a choice.

**Correctness**

Incremental perception is only sound if it cannot silently lose text. Two
invariants enforce that: anything invalidated is fully rescanned (a dirty region
is grown until it wholly contains every element it touches, otherwise a region
clipping a word deletes the element and re-detects only the fragment), and
elements are never mutated in place.

A control experiment worth recording: OCR is 100% deterministic on an identical
frame, but full-frame OCR and OCR of a *crop of the same area* agree only ~91%,
because cutting an image changes how the engine groups glyphs into words
(`"Placer"` becomes `"Plac"` + `"er"`). Region-based OCR is therefore not
identical to full-screen OCR by construction. Dirty regions are padded wider
than they are tall to keep text lines intact.

**Not done yet** — DXGI dirty rectangles (the compositor already knows what
changed; screen capture is now the largest remaining cost per observation), a
persistent per-application UI atlas, and speculative perception.

### v0.4.0

Correctness and packaging release. Several of these were install-blocking.

**Fixed — packaging (`pip install oswright` was broken)**

- **`mcp` upper bound.** `mcp[cli]>=1.0` allowed `mcp` 2.x, which removed `mcp.server.fastmcp`. A fresh install produced a server that could not import at all. Now pinned to `<2`.
- **Windows OCR dependencies were wrong.** The `winocr` extra declared the `winocr` package (which ships `winsdk.*`), but the code imports `winrt.*`. A clean install silently fell back to EasyOCR. The correct `winrt-Windows.*` projections are now default dependencies on Windows — including Foundation, Foundation.Collections and Storage.Streams, which are needed at runtime but never imported directly.
- **No more PyTorch on Windows.** EasyOCR is now installed only on Linux/macOS, where it is the only backend. A Windows install went from ~2.5 GB to a few MB. Use `pip install "oswright[easyocr]"` to opt in.
- **Single version source.** `pyproject.toml` and `__init__.py` had drifted apart.

**Fixed — coordinates**

- **Cached OCR results were mutated in place.** Region offsets were added directly to `ElementMatch` objects handed out by the cache, so every cache hit re-applied the offset and coordinates drifted further on each call.
- **Multi-monitor origins ignored.** Coordinates were image-relative, so any monitor placed left of or above the primary produced clicks offset by the virtual-desktop origin.
- **DPI mismatch.** On a scaled display, window rectangles were logical pixels while screenshots were physical pixels — and which one you got depended on import order, because `mss` quietly makes the process DPI-aware. OSWright now declares per-monitor DPI awareness at import, so everything is physical pixels.
- **Window capture clamped to zero,** cropping or losing windows on monitors with negative coordinates. Now clipped to the real virtual-desktop bounds.

**Fixed — reliability**

- **OCR cache could serve stale results.** The key was a 16×16 perceptual hash, which is designed to collide on similar images; a genuinely changed screen could hash to its previous value. Now an exact content digest.
- **`json.dumps` crashed on successful image matches** — template match coordinates were `numpy.int64`.
- **Solid-colour templates matched everywhere** at confidence 1.0, because `TM_CCOEFF_NORMED` degenerates for a zero-variance template.
- **`wait_for_change` missed colour-only changes** — it compared luminance, so red and green of equal brightness read as identical.
- **Emoji and other non-BMP characters were corrupted** by `type_text` on Windows (truncated into a 16-bit field instead of sent as a UTF-16 surrogate pair).
- **Clipboard writes on macOS always failed** with `TypeError` (`Popen` has no `timeout` parameter). Windows clipboard writes now check every step and no longer leak memory on failure. Added Wayland (`wl-clipboard`) support.
- **64-bit ctypes bugs.** Window and process handles were truncated to 32 bits by ctypes' default `int` return type.
- **Stuck modifiers and mouse buttons.** An exception mid-`press()` or mid-`drag()` left Ctrl/Alt or a mouse button held down, corrupting every later input.
- **`mss` is not thread-safe**, but the MCP server runs tools in a thread pool and shared one instance. Now one per thread.
- **Concurrent MCP requests could interleave** keystrokes and clicks mid-action. Actions now hold a lock.
- **`--timeout` did nothing.** Every tool hardcoded 10s.
- **`--host`/`--port` did nothing.** They were applied after `FastMCP` had already read its settings, so the server always bound to `:8000`.
- **`--log-level` was overridden by the environment** even when passed explicitly.
- **Only the first `--ocr-languages` value was used** by the Windows backend.
- Fatal locator errors (missing template file, no search criteria) burned the full timeout before reporting a misleading "timeout".
- `wait_for(state="hidden")` reported success on any transient capture error.
- Text assertions checked once instead of polling, and ignored the configured timeout.
- `get_ui_tree` hid controls that expose only an `automation_id`.
- Linux `wmctrl` parsing dropped the first word of every window title.

**Fixed — safety**

- **`launch_app` rejected every Windows path.** The v0.3.0 injection fix blocklisted `\`, so `C:\Windows\notepad.exe` was refused. The blocklist is gone — with `shell=False` nothing interprets shell syntax — and replaced with correct argv parsing plus an explicit `args` list.
- **No authentication on remote binds.** The server now refuses non-loopback addresses unless `--allow-remote` is passed, and warns loudly when it is.
- `close_window` is now annotated destructive; screenshot tools refuse to overwrite an existing file.
- Partial region bounds were silently ignored, capturing the whole screen instead of the requested area.

**Added**

- `--snapshot-max-width` to downscale the auto-snapshot returned after every action, cutting token cost.
- CI: lint plus tests on Windows (3.10–3.13) and Linux, and a packaging check.
- Test suite grew from 22 to 91, and `pytest tests/` now actually works — the e2e tests were previously a plain script that pytest collected and errored on.

**Performance**

- Server startup no longer imports EasyOCR (and therefore torch) just to check whether it exists.
- One OCR engine and one screen capture are shared across `Screen` objects instead of built per `screen()` call, and both are created lazily.
- Image locators no longer require an OCR backend at all.

### v0.3.0

- **Accessibility tree support** — Windows UI Automation backend for deterministic element finding by role and name. 100% accurate, instant, no model needed. Tools: `get_ui_tree`, `click_ui_element`, `fill_ui_element`.
- **OCR result caching** — Perceptual image hashing avoids redundant OCR scans when screen hasn't changed. Automatic cache hits speed up repeated queries.
- **Screenshot diffing** — `wait_for_change` tool detects when the screen visually changes after an action. `images_differ` and `get_diff_region` utilities.
- **get_active_window** — New tool to identify which window is currently focused.
- **Test suite** — 22 automated tests covering cache, diff, clipboard, window management, OCR backend selection.
- **Security fix** — `launch_app` now rejects shell metacharacters and uses `shell=False`.
- **35+ MCP tools** total. Improved MCP instructions for better agent guidance.

### v0.2.0

- **Windows OCR backend** — Built-in Windows.Media.Ocr, instant recognition, zero model download, ~10x faster than EasyOCR. Auto-selected on Windows.
- **Window management** — `list_windows`, `focus_window`, `close_window`, `minimize_window`, `screenshot_window`. Cross-platform (Win32, wmctrl, osascript).
- **Clipboard tools** — `get_clipboard`, `set_clipboard`. Cross-platform (Win32, pbcopy/pbpaste, xclip/xsel).
- **App launcher** — `launch_app` with optional `wait_text` to wait for the app to load.
- **OCR info tool** — `get_ocr_info` to see which backend is active and available.
- **30+ MCP tools** total (up from 20 in v0.1.0).
- **Pluggable OCR architecture** — detect.py refactored to auto-select the best available backend.

### v0.1.0

- Initial release.
- Cross-platform input: Windows (Win32 API), Linux/macOS (pynput).
- OCR text detection via EasyOCR, image template matching via OpenCV.
- MCP server with 20+ tools — screenshot, click, type, scroll, drag, find text, fill forms.
- Playwright-style Python library API with auto-waiting locators and assertions.
- CLI arguments: `--port`, `--host`, `--transport`, `--ocr-languages`, `--timeout`, `--log-level`.
- SSE transport for remote/multi-client access.
- GitHub Actions workflow for PyPI trusted publishing.
