"""Smoke test: start server.py over stdio like Claude Desktop does and call every tool.

Usage:  python test_client.py <path to a KiCad project folder>
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

SERVER = Path(__file__).with_name("server.py")


def show(name, result):
    print(f"\n=== {name} ===")
    if result.is_error:
        print("ERROR:", result.content[0].text)
        return
    for block in result.content:
        if block.type == "text":
            text = block.text
            print(text if len(text) < 1500 else text[:1500] + "\n... (truncated)")
        else:
            print(f"<{block.type} {getattr(block, 'mime_type', '')}, {len(block.data)} base64 chars>")


async def main(project: str):
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("Tools:", ", ".join(t.name for t in tools.tools))

            calls = [
                ("kicad_version", {}),
                ("find_projects", {"directory": str(Path(project).parent)}),
                ("run_erc", {"project": project}),
                ("run_drc", {"project": project}),
                ("list_components", {"project": project}),
                ("export_bom", {"project": project}),
                ("board_stats", {"project": project}),
                ("export_manufacturing_files", {"project": project}),
                ("export_schematic_pdf", {"project": project}),
                ("render_pcb_3d", {"project": project, "isometric": True}),
            ]
            for name, args in calls:
                show(name, await session.call_tool(name, args))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
