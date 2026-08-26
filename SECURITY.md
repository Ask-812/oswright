# Security

## What this software does

OSWright controls the mouse, keyboard, clipboard and windows of the machine it
runs on, and can read the screen. An MCP client driving it can therefore do
anything the logged-in user can do.

That is the entire purpose, and it is also the risk. Treat it as you would
treat handing someone remote control of your desktop.

## Things worth knowing before you run it

- **Keyboard input goes to whatever has focus.** The typing tools cannot target
  a window; they send input to the foreground. If focus moves mid-task -- a
  notification, a dialog, another application stealing it -- keystrokes land
  somewhere they were not intended. Do not drive it against a machine where
  that would matter.
- **Windows may refuse a focus request.** `SetForegroundWindow` fails when
  another process holds the foreground lock, so "focus the window, then type"
  can silently fail at the first half and still execute the second.
- **Screenshots capture everything on screen**, including anything sensitive
  that happens to be visible, and are returned to whatever model is driving.
- **Prompt injection is a real risk here.** An agent that reads the screen can
  read text written by someone else -- a web page, a document, an email -- and
  that text can contain instructions. A screen-reading agent with mouse and
  keyboard control is a confused-deputy problem by construction.

## Reporting a vulnerability

Open a GitHub security advisory:
https://github.com/Ask-812/oswright/security/advisories/new

Please do not open a public issue for anything that could be used against
someone before it is fixed.
