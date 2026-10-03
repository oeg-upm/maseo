import json
import os
import sys
from contextlib import AsyncExitStack
from typing import Any, Dict

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

HERE = os.path.dirname(os.path.abspath(__file__))

SERVERS = {
    "syntax_check": "syntax_check_server.py",
    "cq_coverage": "cq_coverage_server.py",
    "oops_scan": "oops_scan_server.py",
    "hermit_consistency": "hermit_consistency_server.py",
    "themis_test": "themis_test_server.py",
}


def _parse(result: Any) -> Dict[str, Any]:
    if isinstance(result, list):
        for block in result:
            text = block.get("text") if isinstance(block, dict) \
                else getattr(block, "text", None)
            if text:
                result = text
                break
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            return json.loads(result)
        except ValueError:
            return {"raw": result}
    return {"raw": str(result)}


class MCPToolbox:

    def __init__(self):
        self._client = MultiServerMCPClient({
            name: {"command": sys.executable,
                   "args": [os.path.join(HERE, "mcp_servers", script)],
                   "transport": "stdio"}
            for name, script in SERVERS.items()})
        self._stack = AsyncExitStack()
        self._tools: Dict[str, Any] = {}

    async def __aenter__(self) -> "MCPToolbox":
        for name in SERVERS:
            session = await self._stack.enter_async_context(
                self._client.session(name))
            for tool in await load_mcp_tools(session):
                self._tools[tool.name] = tool
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self._stack.aclose()

    async def call(self, name: str, **kwargs) -> Dict[str, Any]:
        if name not in self._tools:
            raise KeyError(f"No MCP tool named '{name}' "
                           f"(available: {', '.join(sorted(self._tools))}).")
        return _parse(await self._tools[name].ainvoke(kwargs))


if __name__ == "__main__":
    print("tools.py is the MCP client for the servers under mcp_servers/, "
          "not an entry point. Run: python agent_graph.py <domain>")
