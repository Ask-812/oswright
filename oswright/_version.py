"""
The version, alone, with no imports.

Both `oswright/__init__.py` and `oswright/mcp_server.py` need it, and the
server must not pull in the package root to get it: `__init__` imports the
capture, locator and screen stack, which is a lot of work for one string and
couples the MCP entry point to modules it does not otherwise use.
"""

__version__ = "0.8.2"
