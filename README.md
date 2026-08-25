# OSWright

A Model Context Protocol (MCP) server that provides **OS-level desktop automation** using OCR and image matching. This server enables LLMs to interact with any desktop application -- click buttons, type text, read screens, and fill forms -- just like [Playwright MCP](https://github.com/microsoft/playwright-mcp) does for browsers.

### Key Features

- **Cross-platform.** Windows (Win32 API), Linux (pynput/X11), macOS (pynput/Quartz).
- **Accessibility tree.** Find elements deterministically by role and name via Windows UI Automation — 100% accurate, instant, no model needed.
- **Fast OCR.** Windows OCR (built-in, instant) with EasyOCR fallback for Linux/macOS. Results are cached automatically.
- **Lightweight on Windows.** No PyTorch download — Windows uses the built-in OCR engine, so a full install is a few MB rather than a few GB.
- **Image matching.** Locates elements by template image via OpenCV.
- **Window management.** List, focus, minimize, close, and screenshot specific windows.
- **Screenshot diffing.** Detect when the screen changes with `wait_for_change`.
- **Clipboard access.** Read and write system clipboard for data transfer.
- **App launcher.** Launch applications and wait for them to load.
- **Auto-snapshot.** Every action returns a screenshot so the agent always sees current state.
- **43 MCP tools.** Screen, OCR, UIA, mouse, keyboard, windows, clipboard, and compound actions.
- **Incremental perception.** Rescans only the parts of the screen that changed, and can return what changed instead of a full screenshot — ~21× fewer tokens per step.
- **Screen memory.** Recognises screens it has read before and reuses them, verified by pixels — 89× cheaper than reading again.
- **Resolution cascade.** Element lookups stop at the cheapest method that works; repeat lookups cost ~0.05 ms.
- **DPI-correct.** Coordinates are physical pixels everywhere, so clicks land correctly on scaled displays.
- **Test suite.** 180 automated tests; the desktop-driving ones skip themselves when no display is available.

### Requirements

- Python 3.10 or newer
- VS Code, Cursor, Windsurf, Claude Desktop, or any other MCP client

## Getting started

First, install the OSWright MCP server with your client.

**Standard config** works in most tools:

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

> **Note:** If you don't have `uvx`, you can use `pip install oswright` and then set `"command": "oswright"` directly.

<details>
<summary>Claude Desktop</summary>

Follow the MCP install [guide](https://modelcontextprotocol.io/quickstart/user), use the standard config above.

</details>

<details>
<summary>Claude Code</summary>

```bash
claude mcp add oswright uvx oswright
```

</details>

<details>
<summary>VS Code</summary>

Add to your user or workspace `settings.json` under `mcp.servers`:

```json
{
  "mcp": {
    "servers": {
      "oswright": {
        "command": "uvx",
        "args": ["oswright"]
      }
    }
  }
}
```

Or use the VS Code CLI:

```bash
code --add-mcp '{"name":"oswright","command":"uvx","args":["oswright"]}'
```

</details>

<details>
<summary>Cursor</summary>

Go to `Cursor Settings` -> `MCP` -> `Add new MCP Server`. Name it `oswright`, use `command` type with the command `uvx oswright`.

</details>

<details>
<summary>Windsurf</summary>

Follow Windsurf MCP [documentation](https://docs.windsurf.com/windsurf/cascade/mcp). Use the standard config above.

</details>

<details>
<summary>Cline</summary>

Add to your `cline_mcp_settings.json`:

```json
{
  "mcpServers": {
    "oswright": {
      "type": "stdio",
      "command": "uvx",
      "args": ["oswright"],
      "disabled": false
    }
  }
}
```

</details>

<details>
<summary>Goose</summary>

Go to `Advanced settings` -> `Extensions` -> `Add custom extension`. Name it `oswright`, use type `STDIO`, and set the `command` to `uvx oswright`.

</details>

<details>
<summary>Using pip instead of uvx</summary>

If you prefer a standard pip install:

```bash
pip install oswright
```

Then use this config:

```json
{
  "mcpServers": {
    "oswright": {
      "command": "oswright"
    }
  }
}
```

Or run directly:

```bash
python -m oswright
```

</details>

## Incremental perception

Most GUI agents re-perceive the entire screen on every step: full screenshot,
full OCR, then hand the model a fresh image. Measured on a live desktop, the
median observation changes **0.012% of pixels** — so a full rescan does roughly
240× more work than the change warrants, and the screenshot it returns costs
~2,800 image tokens whether anything happened or not.

OSWright keeps a model of the screen between observations and rescans only the
regions that actually moved.

```
observe()  ->  {"changed": true,
                "added":   [{"text": "Saved", "x": 812, "y": 447}],
                "removed": ["Unsaved changes"],
                "screen_fraction_scanned": 0.015}
```

Measured on this machine over a 14-step agent loop:

| | v0.4.0 (full OCR + screenshot) | incremental |
|---|---|---|
| Median latency per step | 212 ms | **33 ms** |
| Tokens per observation | ~2,764 | **~49** |
| Tokens over 14 steps | 38,696 | **1,025** |
| Screen re-read | 100% | **16%** |

The busier the screen, the larger the gap: full OCR scales with how much text is
on screen, whereas the incremental path scales with how much *changed*. The same
comparison measures 6.5× on a quiet desktop and **14.3×** with a dense web page
open. Re-measure with [`benchmarks/`](benchmarks/) rather than trusting these.

### The resolution cascade

`find_element` and `click_element` stop at the first method that can answer,
so cost tracks how *novel* the request is rather than how large the screen is:

| Rung | Method | Typical cost |
|---|---|---|
| 0 | Already in the screen model | **~0.05 ms** |
| 1 | Rescan only what changed | ~70 ms |
| 2 | Accessibility tree (knows a Button *is* a button) | ~40 ms |
| 3 | App's own text buffer via UIA TextPattern — exact characters | ~400 ms |
| 4 | Full-screen OCR | ~250 ms |

Looking up text the model already knows is **~5,000× cheaper** than the v0.4.0
path (0.05 ms versus 244 ms). The response reports which rung answered, so you
can see what a task is actually costing.

Rung 3 is worth understanding: UIA's `TextRange.FindText` searches the
application's *own* text buffer and returns exact bounding rectangles. It is
immune to font, DPI, antialiasing and OCR error. It sits below the pixel rungs
only because scanning a window's controls for it costs a few hundred
milliseconds of cross-process COM — it is the accurate rung, not the fast one.

> **Note on ordering.** These rungs are ordered by *measurement*, not by theory.
> The common advice is to make the accessibility tree primary, but on real
> applications it is not always cheaper: walking Chrome's tree took 537 ms here,
> slower than a full-screen OCR pass, and VS Code exposed only 18 elements to it.
> Neither pixels nor accessibility wins everywhere, which is why this is a
> cascade rather than a choice.

### Asking the compositor instead of looking

On Windows, the desktop compositor already knows which pixels changed and
exposes them through DXGI Desktop Duplication. Asking it costs **0.14 ms and
transfers no pixels**, against tens of milliseconds to capture a frame and
discover it was identical — so an idle observation skips the capture entirely.

When something *has* changed, the compositor is left holding that frame, so its
pixels are read directly from the GPU rather than grabbed a second time through
a different API — **1.5–2.3× faster** than `mss` in measurements here.

The compositor is used only as a fast *negative* for change detection. When it
reports a change, the dirty regions still come from hashing the captured frame:
the two are measured over slightly different intervals, so compositor rectangles
can under-report relative to the pixels actually captured, and an under-reported
region is text that never gets re-read. It degrades silently to tile hashing and
normal capture wherever Desktop Duplication is unavailable.

Enable delta observations for action tools with `--observation-mode delta`.
The default remains `screenshot` for compatibility with existing clients.

**Reproduce all of this yourself:** see [`benchmarks/`](benchmarks/).
The reasoning behind each decision, including the dead ends, is in
[`docs/ENGINEERING_LOG.md`](docs/ENGINEERING_LOG.md).

## Configuration

OSWright MCP server supports the following arguments. They can be provided in the JSON configuration as part of the `"args"` list:

| Option | Description | Env Variable |
|--------|-------------|-------------|
| `--port <port>` | Port for SSE transport. If omitted, uses stdio (default). | `FASTMCP_PORT` |
| `--host <host>` | Host to bind the HTTP/SSE server to. Default: `127.0.0.1`. | `FASTMCP_HOST` |
| `--transport <mode>` | Transport protocol: `stdio`, `sse`, `streamable-http`. Auto-detected from `--port`. | |
| `--ocr-languages <langs>` | OCR languages (default: `en`). Example: `--ocr-languages en es fr` | `OSWRIGHT_OCR_LANGUAGES` |
| `--timeout <seconds>` | Default timeout for auto-wait operations (default: `10`). | `OSWRIGHT_TIMEOUT` |
| `--snapshot-max-width <px>` | Downscale the auto-snapshot returned after each action. `0` (default) keeps full resolution. Lower values cut token cost significantly. | `OSWRIGHT_SNAPSHOT_MAX_WIDTH` |
| `--observation-mode <mode>` | What action tools return: `screenshot` (default), `delta` (only what changed, ~30× fewer tokens), or `both`. | `OSWRIGHT_OBSERVATION_MODE` |
| `--no-atlas` | Do not remember screens across visits. | `OSWRIGHT_NO_ATLAS` |
| `--allow-remote` | Required to bind a non-loopback address. See [Security](#security). | |
| `--log-level <level>` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR`. Default: `INFO`. | `OSWRIGHT_LOG_LEVEL` |

An explicit command-line flag always wins over the corresponding environment variable.

### Example: Multi-language OCR

```json
{
  "mcpServers": {
    "oswright": {
      "command": "uvx",
      "args": ["oswright", "--ocr-languages", "en", "es", "fr"]
    }
  }
}
```

### Standalone MCP server (SSE)

When running from a worker process or another machine, use SSE transport:

```bash
uvx oswright --port 8931
```

Then in your MCP client config:

```json
{
  "mcpServers": {
    "oswright": {
      "url": "http://127.0.0.1:8931/sse"
    }
  }
}
```

## Security

**OSWright has no authentication.** Anyone who can reach the port gets full
keyboard, mouse, screen and clipboard control of the machine — it is remote
desktop takeover, not a sandboxed API.

The server therefore binds to `127.0.0.1` by default and **refuses** to start on
a non-loopback address unless you pass `--allow-remote`. To reach it from
another machine, prefer an SSH tunnel over exposing the port:

```bash
ssh -L 8931:127.0.0.1:8931 user@desktop-host
```

Stdio transport (the default, used by every MCP client config above) is not
network-exposed at all and is the recommended way to run OSWright.

Tools that can destroy work are annotated accordingly: `close_window` is marked
destructive, and `launch_app` starts arbitrary programs. Screenshot tools refuse
to overwrite an existing `save_path`.

## Platform Notes

| Platform | Input Backend | OCR Backend | Extra downloads |
|----------|--------------|-------------|-------|
| Windows | Win32 API (SendInput) | Windows OCR (instant, built-in) | **None.** No PyTorch. UI Automation included. |
| Linux | pynput (X11) | EasyOCR | PyTorch (~2.5 GB). Requires X11; Wayland has limited support. |
| macOS | pynput (Quartz) | EasyOCR | PyTorch (~2.5 GB). Grant Accessibility permissions in System Settings > Privacy > Accessibility. |

On Windows, EasyOCR is *not* installed, because the built-in Windows OCR engine
is faster and needs no model download. Install it only if you need a language
Windows OCR does not support:

```bash
pip install "oswright[easyocr]"
```

### Coordinates

All coordinates returned by OCR, image matching and UI Automation are **absolute
physical screen pixels**, ready to pass straight to `mouse_click`. This holds for
sub-regions and for multi-monitor setups where the virtual desktop starts at a
negative origin. `screenshot` also reports `origin_x`/`origin_y`, the absolute
position of the image's top-left pixel, for when you read a coordinate off the
image yourself.

## Tools

<details>
<summary><b>Screen</b></summary>

- **screenshot** -- Take a screenshot of the screen or a region. Returns the image as native MCP image content. Optionally saves to a file path.
  - Read-only: **true**

- **get_screen_info** -- Get screen dimensions and monitor count.
  - Read-only: **true**

</details>

<details>
<summary><b>OCR / Text Finding</b></summary>

- **find_text_on_screen** -- Find all occurrences of text on screen using OCR. Returns matches with coordinates and confidence.
  - Parameters: `text`, `exact`, region bounds, `monitor`
  - Read-only: **true**

- **read_screen_text** -- Read ALL visible text on the screen using OCR. Returns every detected text element with position.
  - Parameters: region bounds, `monitor`
  - Read-only: **true**

</details>

<details>
<summary><b>Image Matching</b></summary>

- **find_image_on_screen** -- Find all occurrences of a template image on screen using OpenCV template matching.
  - Parameters: `template_path`, `threshold`, `monitor`
  - Read-only: **true**

</details>

<details>
<summary><b>Mouse</b></summary>

- **mouse_click** -- Click the mouse at coordinates or current position. Returns screenshot.
  - Parameters: `x`, `y`, `button`, `clicks`

- **mouse_double_click** -- Double-click at coordinates or current position. Returns screenshot.

- **mouse_move** -- Move the mouse cursor to screen coordinates.

- **mouse_scroll** -- Scroll the mouse wheel. Returns screenshot.
  - Parameters: `amount`, `x`, `y`

- **mouse_drag** -- Drag from one point to another. Returns screenshot.
  - Parameters: `start_x`, `start_y`, `end_x`, `end_y`, `button`, `duration`

- **get_mouse_position** -- Get the current mouse cursor position.
  - Read-only: **true**

</details>

<details>
<summary><b>Keyboard</b></summary>

- **type_text** -- Type text character by character. Returns screenshot.
  - Parameters: `text`, `delay`

- **press_key** -- Press a key or combo like `Enter`, `Ctrl+C`, `Alt+Tab`. Returns screenshot.
  - Parameters: `key`

</details>

<details>
<summary><b>Compound Actions</b></summary>

- **click_text** -- Find text via OCR and click on it. Auto-retries until found or timeout. Returns screenshot.
  - Parameters: `text`, `exact`, `button`, `timeout`, `poll_interval`, `monitor`

- **double_click_text** -- Find text via OCR and double-click on it. Returns screenshot.

- **right_click_text** -- Find text via OCR and right-click on it. Returns screenshot.

- **hover_text** -- Find text via OCR and hover over it. Returns screenshot.

- **fill_field** -- Find a label, click it, clear, and type a value. Returns screenshot.
  - Parameters: `target_text`, `value`, `exact`, `timeout`, `monitor`

- **fill_form** -- Fill multiple fields in one call. Reduces round-trips.
  - Parameters: `fields` (list of `{label, value}`), `timeout`, `monitor`

- **wait_for_text** -- Wait for text to appear on screen. Polls via OCR.
  - Parameters: `text`, `exact`, `timeout`, `poll_interval`, `monitor`
  - Read-only: **true**

- **wait_for_text_gone** -- Wait for text to disappear from screen.
  - Parameters: `text`, `exact`, `timeout`, `poll_interval`, `monitor`
  - Read-only: **true**

- **wait_for_time** -- Wait for a specified duration (capped at 30s), then screenshot.

</details>

<details>
<summary><b>Window Management</b></summary>

- **list_windows** -- List all visible windows. Optionally filter by title substring.
  - Parameters: `title_filter`
  - Read-only: **true**

- **focus_window** -- Bring a window to the foreground by title. Returns screenshot.
  - Parameters: `title`

- **close_window** -- Close a window by title (sends WM_CLOSE). Returns screenshot.
  - Parameters: `title`

- **minimize_window** -- Minimize a window by title. Returns screenshot.
  - Parameters: `title`

- **screenshot_window** -- Capture a screenshot of just one window.
  - Parameters: `title`, `save_path`
  - Read-only: **true**

</details>

<details>
<summary><b>Clipboard</b></summary>

- **get_clipboard** -- Get the current text content of the system clipboard.
  - Read-only: **true**

- **set_clipboard** -- Copy text to the system clipboard.
  - Parameters: `text`

</details>

<details>
<summary><b>App Management</b></summary>

- **launch_app** -- Launch an application and optionally wait for it to load. Runs the program directly, never through a shell.
  - Parameters: `command`, `args`, `wait_text`, `timeout`
  - Reports `wait_text_found` so you can tell whether the app actually loaded.

- **get_ocr_info** -- Get info about the active OCR backend and available backends.
  - Read-only: **true**

</details>

<details>
<summary><b>Incremental Perception</b></summary>

- **observe** -- Report what changed on screen since the last observation. Rescans only the regions that moved. Prefer this over `screenshot` for tracking state.
  - Parameters: `force_full`
  - Read-only: **true**

- **find_element** -- Find on-screen text using the cheapest method that can answer. Reports which cascade rung responded.
  - Parameters: `text`, `exact`, `window_title`
  - Read-only: **true**

- **click_element** -- Find text via the cascade and click it. The cheap alternative to `click_text`.
  - Parameters: `text`, `exact`, `button`, `window_title`

- **read_model_text** -- Read on-screen text from the incremental model without re-OCRing the display.
  - Parameters: `query`, `limit`
  - Read-only: **true**

- **perception_stats** -- Report how much perception work the model has avoided.
  - Read-only: **true**

- **remember_screen** -- Remember the current screen so future visits skip reading it. Persists across sessions.
  - Read-only: **true**

- **atlas_stats** -- Report what the screen atlas has remembered and how often it helped.
  - Read-only: **true**

</details>

<details>
<summary><b>Accessibility / UI Automation (Windows)</b></summary>

- **get_ui_tree** -- Get the accessibility tree of the focused window. Returns all interactive elements with names, types, positions. Deterministic and instant.
  - Parameters: `window_title`, `max_depth`
  - Read-only: **true**

- **click_ui_element** -- Click a UI element using the accessibility tree. More reliable than OCR.
  - Parameters: `name`, `control_type`, `automation_id`, `window_title`

- **fill_ui_element** -- Set the value of a UI element (e.g., text box). More reliable than OCR-based fill.
  - Parameters: `value`, `name`, `automation_id`, `window_title`

</details>

<details>
<summary><b>Advanced Screen</b></summary>

- **get_active_window** -- Get info about the currently focused window.
  - Read-only: **true**

- **wait_for_change** -- Wait for the screen to visually change. Takes a baseline screenshot, polls until different.
  - Parameters: `timeout`, `poll_interval`

</details>

## Python Library

OSWright also works as a standalone Python library with a Playwright-style API:

```python
from oswright import OSWright

with OSWright() as ow:
    screen = ow.screen()
    screen.click(text="Start")
    screen.type_text("Hello World")
    screen.press("Ctrl+S")
    screen.screenshot("desktop.png")
```

See the [examples/](examples/) directory for more.

## Architecture

```
oswright/
  __init__.py          # Package entry point (single source of __version__)
  core.py              # OSWright class (= Browser)
  screen.py            # Screen class (= Page)
  locator.py           # Locator + Assertions (= Locator + expect)
  capture.py           # Screen capture (mss - cross-platform, thread-safe)
  dirty.py             # Change detection - which parts of the screen moved
  screenmodel.py       # Persistent screen model, updated incrementally
  cascade.py           # Resolution cascade - cheapest method that can answer
  atlas.py             # Remembers screens across visits and sessions
  textprovider.py      # Exact text from the app itself via UIA TextPattern
  detect.py            # OCR dispatcher with caching (auto-selects best backend)
  _ocr_windows.py      # Windows OCR backend (instant, built-in)
  accessibility.py     # Windows UI Automation (deterministic element finding)
  cache.py             # Screenshot diffing, image hashing, OCR result cache
  _dpi.py              # Process DPI awareness (keeps every API in physical pixels)
  _dxgi_windows.py     # Compositor dirty rectangles via DXGI Desktop Duplication
  input.py             # Platform dispatcher for input backends
  _input_windows.py    # Windows input backend (Win32 API)
  _input_pynput.py     # Linux/macOS input backend (pynput)
  window.py            # Window management (list, focus, close)
  clipboard.py         # Clipboard read/write (cross-platform)
  mcp_server.py        # MCP server (43 tools for AI agents)
tests/
  conftest.py          # Fixtures that skip when no display/OCR is available
  test_core.py         # Unit tests (no desktop required)
  test_perception.py   # Incremental perception (stubbed, runs headless)
  test_atlas.py        # Screen memory and its failure modes (headless)
  test_e2e.py          # End-to-end tests against the real desktop (marked `e2e`)
```

### Remembering screens

Applications are deterministic — the same dialog has the same layout every time.
OSWright remembers screens it has read and reuses them on the next visit, across
sessions: **125 ms cold read → 1.4 ms warm recall (89×)**.

A remembered screen is never trusted on recognition alone. A few regions are
spot-checked *by pixels* before the layout is reused, so a screen that has
changed is rejected rather than acted on. Verification fails closed: a screen
with nothing checkable is not remembered at all.

Disable with `--no-atlas`. Remembered screens live in `~/.oswright/atlas.json`.

### Not done yet

- **Speculative perception.** With both the atlas and the change oracle in
  place, an agent could predict the post-action screen and verify the prediction
  rather than re-perceiving. Correct predictions would cost nothing.
- **Wayland input injection**, and macOS `AXTextMarker` as a TextPattern
  equivalent.

## Development

```bash
pip install -e ".[dev]"

pytest tests/                # everything available on this machine
pytest tests/ -m "not e2e"   # unit tests only, no desktop needed
ruff check oswright tests    # lint
python benchmarks/bench_pipeline.py   # reproduce the performance numbers
```

Design decisions, measurements and dead ends are recorded in
[`docs/ENGINEERING_LOG.md`](docs/ENGINEERING_LOG.md).

## License

MIT
