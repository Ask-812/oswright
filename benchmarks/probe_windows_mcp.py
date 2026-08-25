"""
Can Windows-MCP be driven tool-by-tool, without an LLM and without credentials?

That is the gate for any head-to-head. A comparison that cannot be run
reproducibly, from a script, is not evidence -- and if the answer here is no,
the honest move is to abandon the comparison rather than approximate it.

Run:  python benchmarks/probe_windows_mcp.py [path-to-Windows-MCP]
"""

import asyncio
import os
import sys

DEFAULT_ROOT = os.path.join(os.environ.get("TEMP", "."), "Windows-MCP")


async def main(root: str) -> int:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    python = os.path.join(root, ".venv", "Scripts", "python.exe")
    if not os.path.exists(python):
        print(f"no interpreter at {python}")
        return 1

    params = StdioServerParameters(
        command=python,
        args=["-m", "windows_mcp", "serve", "--transport", "stdio"],
        cwd=root,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools

            print(f"connected without credentials; {len(tools)} tools\n")
            for tool in sorted(tools, key=lambda t: t.name):
                summary = (tool.description or "").strip().splitlines()
                first = summary[0][:66] if summary else ""
                print(f"  {tool.name:<22} {first}")

            # The perception tool is the one that matters for a comparison:
            # everything oswright does differently lives behind it.
            names = {t.name.lower() for t in tools}
            for candidate in ("snapshot", "state", "state-tool", "snapshot-tool"):
                if candidate in names:
                    print(f"\nperception tool present: {candidate!r}")
                    break
    return 0


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ROOT
    raise SystemExit(asyncio.run(main(root)))
