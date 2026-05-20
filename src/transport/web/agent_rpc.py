"""WebSocket /agent-rpc/{token}: in-container скрипт зовёт host-агента.

`_Host` — единственная точка экспорта; всё остальное — обвязка WS↔Channel."""

import asyncio, json, logging

from fastapi import WebSocket, WebSocketDisconnect

from agent import Agent
from src.memory.memory import Memory
from src.skills.sandbox.container_lib.rpc import Channel
from src.transport.base import BaseTransport

log = logging.getLogger(__name__)


class _Host:
    def __init__(self, agent_id: str):
        self._agent_id = agent_id

    async def get_agent(self, thread_id: str | None = None):
        return await Agent.get(self._agent_id, thread_id or "")


_ALLOWED = {
    _Host: {"get_agent"},
    Agent: {"transport", "memory", "spawn_subagent", "next_message",
            "loop", "get_agent_dir", "process_message", "llm", "close",
            "id", "thread_id"},
    BaseTransport: {
        "send_message", "send_thinking", "send_memory_info", "send_processing",
        "send_system_prompt", "on_tool_call", "on_tool_result",
        "inject_message", "send_app_url", "send_images", "send_voice",
        "send_files",
    },
    Memory: {"clear", "add_turn"},
}


async def handle_agent_rpc(agent_id: str, ws: WebSocket):
    await ws.accept()
    loop = asyncio.get_running_loop()
    inbox: asyncio.Queue = asyncio.Queue()
    closed = asyncio.Event()

    async def pump_recv():
        try:
            while True:
                inbox.put_nowait(await ws.receive_text())
        except WebSocketDisconnect:
            pass
        finally:
            inbox.put_nowait(None)
            closed.set()

    pump_task = asyncio.create_task(pump_recv())

    def readline():
        msg = asyncio.run_coroutine_threadsafe(inbox.get(), loop).result()
        return "" if msg is None else msg + "\n"

    def writeline(msg):
        if closed.is_set():
            return
        asyncio.run_coroutine_threadsafe(
            ws.send_text(json.dumps(msg, ensure_ascii=False)), loop,
        ).result()

    ch = Channel(readline, writeline, ref_prefix="h", allowed=_ALLOWED, async_loop=loop)
    ch.register("host", _Host(agent_id))
    log.info("[agent-rpc] connected agent=%s", agent_id)
    ch.start()
    try:
        await closed.wait()
    finally:
        ch.close()
        pump_task.cancel()
        try: await ws.close()
        except Exception: pass
