"""HomePilot's MCP client for commerce servers.

HomePilot never imports the store's code to order: it calls tools over the Model Context
Protocol, exactly as it would call Swiggy's or Zomato's official MCP servers once they allow
third-party apps. STORE_MCP_URL picks the server:
  * unset             -> our demo store mounted on this server (http://127.0.0.1:$PORT/store/mcp)
  * "internal"        -> the demo store in-process (tests)
  * any https://... URL -> another MCP commerce server
"""
import asyncio
import json
import os

from . import config


def _target():
    url = config.STORE_MCP_URL
    if url == "internal":
        from .store_server import mcp
        return mcp
    if url:
        return url
    return f"http://127.0.0.1:{os.getenv('PORT', '8000')}/store/mcp"


def describe() -> str:
    t = _target()
    return t if isinstance(t, str) else "in-process demo store"


async def _call_async(tool: str, args: dict, target=None) -> dict:
    from mcp.client.client import Client
    async with Client(target or _target(), read_timeout_seconds=15) as client:
        result = await client.call_tool(tool, args)
    data = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if isinstance(data, dict):
        return data.get("result", data) if set(data) == {"result"} else data
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
    return {"error": "Empty response from the store."}


def call(tool: str, args: dict | None = None) -> dict:
    """Synchronous helper. Must not run on the server's event-loop thread (brain.py calls
    tools in a worker thread), because the store may be served by this same process."""
    try:
        return asyncio.run(_call_async(tool, args or {}))
    except Exception as e:  # network/protocol errors become a normal tool error
        if not config.STORE_MCP_URL:
            # Our own demo store could not be reached over HTTP (wrong port, proxy, cold
            # start): talk to the same MCP server in-process so the demo keeps working.
            try:
                from .store_server import mcp
                return asyncio.run(_call_async(tool, args or {}, mcp))
            except Exception:
                pass
        return {"error": f"Store unavailable ({type(e).__name__}): {str(e)[:160]}"}
