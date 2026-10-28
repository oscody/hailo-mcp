"""Start server.py over stdio like an MCP client would, and exercise each tool.

Run: .venv/bin/python smoke_test.py
"""

import asyncio
import json
import sys
import time
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

HERE = Path(__file__).parent


def show(label, result):
    text = "".join(getattr(c, "text", "") for c in result.content)
    flag = "ERROR" if result.is_error else "ok"
    print(f"\n[{label}] {flag}\n{text[:600]}")
    return result


async def main():
    params = StdioServerParameters(command=sys.executable, args=[str(HERE / "server.py")])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        print("tools:", [t.name for t in tools.tools])

        show("status", await session.call_tool("hailo_status", {}))

        t = time.time()
        show("ask", await session.call_tool("hailo_ask", {
            "system": "Answer in one short sentence.",
            "prompt": "What is a Raspberry Pi?",
        }))
        print(f"(round trip {time.time() - t:.1f} s)")

        show("too long (expect error)", await session.call_tool("hailo_ask", {"prompt": "word " * 2000}))
        show("empty (expect error)", await session.call_tool("hailo_ask", {"prompt": "  "}))

        # Two at once: the lock should run them one after the other, not crash hailo-ollama.
        t = time.time()
        a, b = await asyncio.gather(
            session.call_tool("hailo_ask", {"prompt": "Name one fruit. One word.", "max_tokens": 8}),
            session.call_tool("hailo_ask", {"prompt": "Name one color. One word.", "max_tokens": 8}),
        )
        show("parallel A", a)
        show("parallel B", b)
        print(f"(both done in {time.time() - t:.1f} s)")


asyncio.run(main())
