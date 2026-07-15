"""Smoke test for the MCP server. Connects, lists tools, publishes a test cook."""
import asyncio
import os
import sys

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

load_dotenv()

TOKEN = os.environ["MCP_BEARER_TOKEN"]
URL = f"http://127.0.0.1:{os.environ.get('PORT', '8765')}/mcp"


async def main() -> int:
    print(f"→ connecting to {URL}")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with streamablehttp_client(URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print(f"✓ initialize OK — {len(tools.tools)} tools:")
            for t in tools.tools:
                print(f"  · {t.name}")

            print("\n→ calling list_recent_cooks(n=3)")
            r = await session.call_tool("list_recent_cooks", {"n": 3})
            print(f"✓ returned:\n{r.content[0].text if r.content else '(empty)'}")

            print("\n→ calling search_cooks(query='anchovy')")
            r = await session.call_tool("search_cooks", {"query": "anchovy"})
            print(f"✓ returned:\n{r.content[0].text if r.content else '(empty)'}")

            print("\n→ calling publish_cook (test payload)")
            r = await session.call_tool(
                "publish_cook",
                {
                    "title": "MCP smoke test — sheet-pan gochujang chicken",
                    "body": (
                        "Test post from the smoke-test script. Delete after verifying.\n\n"
                        "## Ingredients\n\n"
                        "- 4 chicken thighs, bone-in skin-on\n"
                        "- 2 tbsp gochujang\n"
                        "- 1 tbsp honey\n"
                        "- 1 tbsp soy sauce\n"
                        "- 1 tsp sesame oil\n"
                    ),
                    "source_name": "smoke test",
                    "tags": ["test", "chicken", "sheet-pan"],
                },
            )
            url = r.content[0].text if r.content else "(no url)"
            print(f"✓ published: {url}")
            return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
