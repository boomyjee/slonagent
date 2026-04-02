"""Stub library for slonagent sandbox scripts.

Provides the same Skill/tool API as the host agent.
Scripts define Skill subclasses; the runner discovers and executes them automatically.

Usage:
    from slonagent import Skill, tool
    from typing import Annotated

    class MySkill(Skill):
        @tool("Description")
        async def my_tool(self, arg: Annotated[str, "Desc"]) -> dict:
            await self.agent.transport.send_message("Hello!")
            content_parts, _ = await self.agent.next_message()
            return {"result": "ok"}
"""

import sys, json, asyncio
from typing import Annotated, get_type_hints, get_args, get_origin


def tool(description: str):
    def decorator(fn):
        fn._is_tool = True
        fn._tool_description = description
        return fn
    return decorator


def _rpc(method, *args, **kwargs):
    sys.stdout.write(json.dumps({"method": method, "args": args, "kwargs": kwargs}) + "\n")
    sys.stdout.flush()
    line = sys.stdin.readline()
    return json.loads(line) if line.strip() else {}


class _Proxy:
    def __init__(self, path=""):
        self._path = path

    def __getattr__(self, name):
        return _Proxy(f"{self._path}.{name}" if self._path else name)

    async def __call__(self, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: _rpc(self._path, *args, **kwargs))


class Skill:
    def __init__(self):
        self.agent = _Proxy("agent")
