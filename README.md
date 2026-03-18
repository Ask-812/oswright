# OSWright 🖥️

**Playwright-like automation framework for the operating system.**

OSWright lets you automate your desktop the same way Playwright automates browsers — with auto-waiting locators, screenshots, assertions, and a clean API.

## Installation

```bash
pip install -e .
```

### Platform Requirements

| Platform | Input Backend | Extra Dependencies |
|----------|--------------|-------------------|
| Windows  | Win32 API (SendInput) | None (uses ctypes) |
| Linux    | pynput (X11) | `pynput` (auto-installed) |
| macOS    | pynput (Quartz) | `pynput` (auto-installed) |

**Linux notes:** Requires an X11 display server. Wayland has limited support.
**macOS notes:** Grant Accessibility permissions in System Settings > Privacy > Accessibility.

## Quick Start

```python
from oswright import OSWright

with OSWright() as ow:
    screen = ow.screen()

    # Click a button by its visible text
    screen.click(text="Start")

    # Type text
    screen.type_text("Hello World")

    # Press keyboard shortcuts
    screen.press("Enter")
    screen.press("Ctrl+S")

    # Take a screenshot
    screen.screenshot("desktop.png")
```

## Core Concepts

### Screen (≈ Playwright's Page)

The `Screen` is your main interaction surface — the entire desktop or a specific monitor.

```python
screen = ow.screen()          # All monitors
screen = ow.screen(monitor=1) # Primary monitor only
```

### Locators (≈ Playwright's Locators)

Locators find elements on screen using **OCR text detection** or **image template matching**. They are **lazy** (don't search until an action is performed) and **auto-wait** (retry until element appears or timeout).

```python
# Find by text (OCR)
screen.locator(text="Save").click()
screen.locator(text="File", exact=True).click()

# Find by image template
screen.locator(image="button_save.png").click()

# Playwright-style shortcuts
screen.get_by_text("Submit").click()
screen.get_by_image("icon.png").hover()
```

### Auto-Wait

Like Playwright, all actions automatically wait for the element to appear:

```python
# Waits up to 10s (default) for "Welcome" text to appear, then clicks
screen.click(text="Welcome")

# Custom timeout
screen.locator(text="Loading Complete", timeout=30).wait_for()

# Wait for element to disappear
screen.wait_for_text_gone("Please wait...")
```

### Direct Actions

For quick operations without locators:

```python
# Click at coordinates
screen.click(x=500, y=300)

# Type and press keys
screen.type_text("Hello World")
screen.press("Ctrl+A")
screen.hotkey("alt", "f4")

# Mouse operations
screen.scroll(-3)  # Scroll down
screen.drag(100, 100, 500, 500)
screen.move(400, 300)
```

### Assertions (≈ Playwright's expect)

```python
# Assert element is visible
screen.locator(text="Welcome").expect().to_be_visible()

# Assert element is NOT visible
screen.locator(text="Error").expect().not_to_be_visible()

# Assert text content
screen.locator(text="Score").expect().to_have_text("Score: 100")
```

### Screen Reading

```python
# Read all text on screen
elements = screen.read_text()
for el in elements:
    print(f"Found: '{el.text}' at ({el.x}, {el.y}) conf={el.confidence:.2f}")

# Find specific text
matches = screen.find_text("Settings")
if matches:
    print(f"Settings found at {matches[0].center}")

# Screenshot a region
screen.screenshot("region.png", region={"left": 0, "top": 0, "width": 500, "height": 400})
```

### Fill (Click + Clear + Type)

```python
# Click a text field and type into it
screen.locator(text="Username").fill("admin")
screen.locator(text="Password").fill("secret")
screen.locator(text="Login").click()
```

### Chaining

```python
# Target specific matches when multiple exist
screen.locator(text="OK").first().click()   # First match
screen.locator(text="OK").last().click()    # Last match
screen.locator(text="OK").nth(2).click()    # Third match
```

## API Comparison with Playwright

| Playwright | OSWright | Notes |
|---|---|---|
| `page = browser.new_page()` | `screen = ow.screen()` | Screen = Page |
| `page.locator('#btn')` | `screen.locator(text="Click me")` | CSS → OCR/Image |
| `page.get_by_text('OK')` | `screen.get_by_text('OK')` | Same API! |
| `page.click('#btn')` | `screen.click(text="Click me")` | |
| `page.fill('#input', 'hi')` | `screen.fill('hi', target_text="Name")` | |
| `page.press('Enter')` | `screen.press('Enter')` | Same API! |
| `page.screenshot()` | `screen.screenshot()` | Same API! |
| `expect(loc).to_be_visible()` | `loc.expect().to_be_visible()` | Similar pattern |
| `page.wait_for_selector()` | `screen.wait_for_text()` | Selector → Text/Image |

## Architecture

```
oswright/
├── __init__.py          # Package entry point
├── core.py              # OSWright class (≈ Browser)
├── screen.py            # Screen class (≈ Page)
├── locator.py           # Locator + Assertions (≈ Locator + expect)
├── capture.py           # Screen capture (mss - cross-platform)
├── detect.py            # OCR + image matching (easyocr, opencv)
├── input.py             # Platform dispatcher for input backends
├── _input_windows.py    # Windows backend (Win32 API)
├── _input_pynput.py     # Linux/macOS backend (pynput)
└── mcp_server.py        # MCP server for AI agent integration
```

## Requirements

- **Python 3.10+**
- **Windows, Linux, or macOS**
- Dependencies: `mss`, `Pillow`, `opencv-python`, `numpy`, `easyocr`
- Linux/macOS additionally: `pynput` (auto-installed)

## MCP Server

OSWright includes an MCP (Model Context Protocol) server, enabling AI agents
to control the desktop:

```bash
# Run the MCP server
python -m oswright

# Or via the entry point
oswright
```

The MCP server exposes tools like `screenshot`, `click_text`, `type_text`,
`find_text_on_screen`, `fill_field`, and more — with auto-snapshot after
every action so the agent always sees current screen state.

## License

MIT
