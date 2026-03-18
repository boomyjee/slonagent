import asyncio
import base64
import json
import logging
import re

from agent import Skill


def _sanitize(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name).lower()


class McpSkill(Skill):
    """Подключает произвольные MCP-серверы и предоставляет их инструменты агенту."""

    def __init__(self, servers: list[dict]):
        self.servers_config = servers
        self._tool_sessions: dict[str, tuple] = {}  # tool_name -> (ClientSession, original_tool_name)
        self._tasks: list[asyncio.Task] = []
        super().__init__()

    async def start(self):
        for server_config in self.servers_config:
            ready = asyncio.Event()
            task = asyncio.create_task(self._run_server(server_config, ready))
            self._tasks.append(task)
            await ready.wait()

    async def _run_server(self, config: dict, ready: asyncio.Event):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        name = config["name"]
        server_params = StdioServerParameters(
            command=config["command"],
            args=config.get("args") or [],
            env=config.get("env") or None,
        )
        try:
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools_result = await session.list_tools()
                    prefix = _sanitize(name)
                    for t in tools_result.tools:
                        tool_name = f"{prefix}_{_sanitize(t.name)}"
                        self._tool_sessions[tool_name] = (session, t.name)
                        schema = t.inputSchema or {"type": "object", "properties": {}}
                        self._tools.append({"type": "function", "function": {
                            "name": tool_name,
                            "description": t.description or "",
                            "parameters": schema,
                        }})
                    logging.info("[McpSkill] Сервер %s: %d инструментов", name, len(tools_result.tools))
                    ready.set()
                    await asyncio.Event().wait()  # держим контекст и сессию живыми
        except asyncio.CancelledError:
            logging.info("[McpSkill] Сервер %s остановлен", name)
        except Exception as e:
            logging.error("[McpSkill] Ошибка сервера %s: %s", name, e)
            ready.set()

    async def dispatch_tool_call(self, tool_call: dict) -> dict:
        name = tool_call["function"]["name"]
        if name not in self._tool_sessions:
            return {"error": f"Unknown MCP tool: {name}"}

        session, original_name = self._tool_sessions[name]
        args = json.loads(tool_call["function"].get("arguments") or "{}")

        try:
            result = await session.call_tool(original_name, args)
        except Exception as e:
            return {"error": str(e)}

        text_parts = []
        image_parts = []
        for item in result.content:
            if item.type == "text":
                text_parts.append(item.text)
            elif item.type == "image":
                data = base64.b64decode(item.data)
                b64 = base64.b64encode(data).decode()
                image_parts.append({"type": "image_url", "image_url": {"url": f"data:{item.mimeType};base64,{b64}"}})

        response: dict = {"result": "\n".join(text_parts)}
        if result.isError:
            response["is_error"] = True
        if image_parts:
            response["_parts"] = image_parts
        return response
