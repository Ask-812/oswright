# OSWright

A Model Context Protocol (MCP) server that provides **OS-level desktop automation** using OCR and image matching. This server enables LLMs to interact with any desktop application -- click buttons, type text, read screens, and fill forms -- just like [Playwright MCP](https://github.com/microsoft/playwright-mcp) does for browsers.

### Key Features

- **Cross-platform.** Windows (Win32 API), Linux (pynput/X11), macOS (pynput/Quartz).
- **OCR-powered.** Finds UI elements by their visible text using EasyOCR.
- **Image matching.** Locates elements by template image via OpenCV.
- **Auto-snapshot.** Every action returns a screenshot so the agent always sees current state.
- **Compound tools.** High-level actions like `click_text`, `fill_field`, `fill_form` reduce round-trips.

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

## Configuration

OSWright MCP server supports the following arguments. They can be provided in the JSON configuration as part of the `"args"` list:

| Option | Description | Env Variable |
|--------|-------------|-------------|
| `--port <port>` | Port for SSE transport. If omitted, uses stdio (default). | `FASTMCP_PORT` |
| `--host <host>` | Host to bind SSE server to. Default: `localhost`. | `FASTMCP_HOST` |
| `--transport <mode>` | Transport protocol: `stdio`, `sse`, `streamable-http`. Auto-detected from `--port`. | |
| `--ocr-languages <langs>` | OCR languages (default: `en`). Example: `--ocr-languages en es fr` | `OSWRIGHT_OCR_LANGUAGES` |
| `--timeout <seconds>` | Default timeout for auto-wait operations (default: `10`). | `OSWRIGHT_TIMEOUT` |
| `--log-level <level>` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR`. Default: `INFO`. | `OSWRIGHT_LOG_LEVEL` |

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

When running from a remote machine or a worker process, use SSE transport:

```bash
uvx oswright --port 8931
```

Then in your MCP client config:

```json
{
  "mcpServers": {
    "oswright": {
      "url": "http://localhost:8931/mcp"
    }
  }
}
```

## Platform Notes

| Platform | Input Backend | Notes |
|----------|--------------|-------|
| Windows | Win32 API (SendInput) | No extra dependencies. Works out of the box. |
| Linux | pynput (X11) | Requires X11 display server. Wayland has limited support. |
| macOS | pynput (Quartz) | Grant Accessibility permissions in System Settings > Privacy > Accessibility. |

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
  __init__.py          # Package entry point
  core.py              # OSWright class (= Browser)
  screen.py            # Screen class (= Page)
  locator.py           # Locator + Assertions (= Locator + expect)
  capture.py           # Screen capture (mss - cross-platform)
  detect.py            # OCR + image matching (easyocr, opencv)
  input.py             # Platform dispatcher for input backends
  _input_windows.py    # Windows backend (Win32 API)
  _input_pynput.py     # Linux/macOS backend (pynput)
  mcp_server.py        # MCP server for AI agent integration
```

## License

MIT
