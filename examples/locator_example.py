"""
Example: Using locators and assertions (Playwright-style).

Demonstrates:
  - Locator-based element finding
  - Auto-wait behavior
  - Assertions
  - Screenshot capture
"""

from oswright import OSWright


def main():
    with OSWright(timeout=10) as ow:
        screen = ow.screen()

        # Take a screenshot of the current desktop
        screen.screenshot("desktop.png")
        print("Screenshot saved to desktop.png")

        # Read all text currently visible on screen
        elements = screen.read_text()
        print(f"\nFound {len(elements)} text elements on screen:")
        for el in elements[:10]:  # Show first 10
            print(f"  '{el.text}' at ({el.x}, {el.y}) confidence={el.confidence:.2f}")

        # Use a locator to find the taskbar clock/time
        # (This is just an example - adjust the text to match your screen)
        clock = screen.locator(text="AM")  # or "PM"
        if clock.is_visible(timeout=2):
            print(f"\nFound clock: {clock.text_content()}")
            clock.screenshot("clock.png")
            print("Clock screenshot saved to clock.png")

        # Assertions example
        try:
            screen.locator(text="Recycle Bin").expect().to_be_visible(timeout=5)
            print("\nRecycle Bin is visible on desktop!")
        except AssertionError as e:
            print(f"\n{e}")

        # Screen info
        size = screen.get_size()
        pos = screen.get_mouse_position()
        print(f"\nScreen size: {size['width']}x{size['height']}")
        print(f"Mouse position: {pos}")


if __name__ == "__main__":
    main()
