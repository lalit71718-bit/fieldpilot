"""FieldPilot enterprise tools exposed over MCP (Model Context Protocol).

Any MCP client can use the same actions the voice agent uses, e.g.:
    mcp dev backend/mcp_server.py          # MCP Inspector
or add to Claude Desktop / any agent framework:
    {"command": "python", "args": ["-m", "backend.mcp_server"], "cwd": "<repo>"}
"""
try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

from . import tools

mcp = FastMCP("fieldpilot")

for _name, (_fn, _schema) in tools.TOOLS.items():
    mcp.add_tool(_fn, name=_name, description=_schema["description"])

if __name__ == "__main__":
    mcp.run()
