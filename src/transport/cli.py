import asyncio

from src.transport.base import BaseTransport

class CliTransport(BaseTransport):
    def __init__(self):
        super().__init__()
        self._printed: dict[int, int] = {}  # stream_id → chars printed

    def _stream_print(self, text: str, stream_id, prefix: str):
        printed = self._printed.get(stream_id, 0) if stream_id else 0
        if printed == 0:
            print(f"\n{prefix}", end="", flush=True)
        print(text[printed:], end="", flush=True)
        if stream_id:
            self._printed[stream_id] = len(text)

    async def send_message(self, text: str, stream_id=None):
        self._stream_print(text, stream_id, "Агент: ")

    async def send_system_prompt(self, text: str):
        print(f"[system]\n{text}\n")

    async def send_thinking(self, text: str, stream_id=None, final: bool = False):
        self._stream_print(text, stream_id, "[думает...]\n")

    async def on_tool_call(self, name: str, args: dict):
        print(f"[{name}] {args}")

    async def on_tool_result(self, name: str, result):
        print(f"[{name}] -> {result}")

    async def inject_message(self, text: str):
        print(f"\n[→] {text}")
