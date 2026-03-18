"""
Example: Automate opening Notepad, typing text, and saving a file.

Usage:
    python examples/notepad_example.py
"""

import time
from oswright import OSWright


def main():
    with OSWright(timeout=15) as ow:
        screen = ow.screen()

        # Open Notepad via the Start menu
        screen.press("Win")
        time.sleep(1)
        screen.type_text("Notepad")
        time.sleep(1)
        screen.press("Enter")
        time.sleep(2)

        # Type some text
        screen.type_text("Hello from OSWright!\n")
        screen.type_text("This text was typed by an OS-level automation framework.\n")
        screen.type_text("Think of it as Playwright, but for your entire desktop.")

        # Save the file with Ctrl+S
        screen.press("Ctrl+S")
        time.sleep(1)

        # Wait for Save dialog and type filename
        screen.wait_for_text("Save as", timeout=10)
        screen.type_text("oswright_demo.txt")
        screen.press("Enter")

        print("Done! File saved as oswright_demo.txt")


if __name__ == "__main__":
    main()
