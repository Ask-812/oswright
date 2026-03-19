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
- **Fast OCR on Windows** — uses built-in Windows OCR (instant, zero model download) with EasyOCR fallback on Linux/macOS
- Two ways to use it: as an **MCP server** (for AI agents) or as a **Python library** (for scripts)
- Playwright-style Python API with auto-waiting locators and assertions
- **30+ MCP tools** with auto-snapshot after every action
- **Window management** — list, focus, close, minimize, and screenshot specific windows
- **Clipboard access** — read and write system clipboard for data transfer
- **App launcher** — launch applications and wait for them to load
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
