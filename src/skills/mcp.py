import asyncio
import logging
import re

from google.genai import types

from agent import Skill


def _sanitize(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name).lower()


def _json_schema_to_gemini(schema: dict) -> types.Schema:
    _TYPE_MAP = {
        "string": types.Type.STRING,
        "integer": types.Type.INTEGER,
        "number": types.Type.NUMBER,
        "boolean": types.Type.BOOLEAN,
        "array": types.Type.ARRAY,
        "object": types.Type.OBJECT,
    }
    t = _TYPE_MAP.get(schema.get("type", "string"), types.Type.STRING)
    kwargs: dict = {"type": t}
    if desc := schema.get("description"):
        kwargs["description"] = desc
    if t == types.Type.ARRAY:
        kwargs["items"] = _json_schema_to_gemini(schema["items"]) if "items" in schema else types.Schema(type=types.Type.STRING)
    elif t == types.Type.OBJECT:
        props = schema.get("properties") or {}
        kwargs["properties"] = {k: _json_schema_to_gemini(v) for k, v in props.items()}
        if "required" in schema:
            kwargs["required"] = schema["required"]
    return types.Schema(**kwargs)


class McpSkill(Skill):
    """Подключает произвольные MCP-серверы и предоставляет их инструменты агенту."""

    def __init__(self, servers: list[dict]):
        self.servers_config = servers
        self._tool_sessions: dict[str, tuple] = {}  # gemini_name -> (ClientSession, original_tool_name)
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
                        gemini_name = f"{prefix}_{_sanitize(t.name)}"
                        self._tool_sessions[gemini_name] = (session, t.name)
                        self._tools.append(types.FunctionDeclaration(
                            name=gemini_name,
                            description=t.description or "",
                            parameters=_json_schema_to_gemini(
                                t.inputSchema or {"type": "object", "properties": {}}
                            ),
                        ))
                    logging.info("[McpSkill] Сервер %s: %d инструментов", name, len(tools_result.tools))
                    ready.set()
                    await asyncio.Event().wait()  # держим контекст и сессию живыми
        except asyncio.CancelledError:
            logging.info("[McpSkill] Сервер %s остановлен", name)
        except Exception as e:
            logging.error("[McpSkill] Ошибка сервера %s: %s", name, e)
            ready.set()

    async def dispatch_tool_call(self, tool_call) -> dict:
        if tool_call.name not in self._tool_sessions:
            return {"error": f"Unknown MCP tool: {tool_call.name}"}

        session, original_name = self._tool_sessions[tool_call.name]
        args = dict(tool_call.args or {})

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
                import base64
                data = base64.b64decode(item.data)
                image_parts.append(types.Part.from_bytes(data=data, mime_type=item.mimeType))

        response: dict = {"result": "\n".join(text_parts)}
        if result.isError:
            response["is_error"] = True
        if image_parts:
            response["_parts"] = image_parts
        return response
