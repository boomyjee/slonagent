"""Antigravity MCP Bridge: Stdio MCP server spawned by agy.exe to route tool calls to SlonAgent.

Communicates with the parent Python process (AntigravityBackend) via a local HTTP loopback server.
"""
import argparse
import asyncio
import json
import logging
import sys

import httpx
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
log = logging.getLogger("antigravity_mcp")


def parse_args():
    parser = argparse.ArgumentParser(description="SlonAgent Antigravity MCP Bridge")
    parser.add_argument("--port", type=int, required=True, help="Port of local loopback bridge server")
    return parser.parse_args()


async def run_server(port: int):
    app = Server("slon")
    base_url = f"http://127.0.0.1:{port}"

    def _dbg(msg: str):
        try:
            with open("C:/Temp/mcp_debug.log", "a", encoding="utf-8") as f:
                f.write(f"[{port}] {msg}\n")
        except Exception:
            pass

    _dbg(f"Bridge starting for port {port}")

    @app.list_tools()
    async def list_tools() -> list[types.Tool]:
        _dbg("list_tools called")
        try:
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                resp = await client.get(f"{base_url}/tools")
                resp.raise_for_status()
                data = resp.json()
                tools = []
                for t in data.get("tools", []):
                    tools.append(types.Tool(
                        name=t["name"],
                        description=t.get("description", ""),
                        inputSchema=t.get("inputSchema") or {"type": "object", "properties": {}},
                    ))
                _dbg(f"list_tools returning {[t.name for t in tools]}")
                return tools
        except Exception as e:
            _dbg(f"list_tools failed: {e}")
            log.error("Failed to list tools from bridge: %s", e)
            return []

    @app.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        _dbg(f"call_tool called: name={name}, arguments={arguments}")
        try:
            async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
                resp = await client.post(
                    f"{base_url}/call",
                    json={"name": name, "arguments": arguments},
                )
                resp.raise_for_status()
                data = resp.json()
                result_text = data.get("result", "")
                if not isinstance(result_text, str):
                    result_text = json.dumps(result_text, ensure_ascii=False)
                _dbg(f"call_tool returning: {result_text}")
                return [types.TextContent(type="text", text=result_text)]
        except Exception as e:
            _dbg(f"call_tool failed: {e}")
            log.error("Failed to execute tool %s via bridge: %s", name, e)
            return [types.TextContent(type="text", text=f"Error executing tool {name}: {e}")]

    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


def main():
    args = parse_args()
    asyncio.run(run_server(args.port))


if __name__ == "__main__":
    main()
