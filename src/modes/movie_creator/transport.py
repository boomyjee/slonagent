"""Web transport — mirrors agent events to MovieServer web UI."""
from src.transport.base import BaseTransport
from src.modes.movie_creator.server import MovieServer


class WebTransport(BaseTransport):

    def __init__(self, server: MovieServer):
        super().__init__()
        self.server = server

    async def send_message(self, text: str, stream_id=None):
        await self.server.send_chat(text, stream_id=stream_id)

    async def send_thinking(self, text: str, stream_id=None, final: bool = False):
        if final:
            await self.server.send_event("thinking", text=text)

    async def on_tool_call(self, name: str, args: dict):
        await self.server.send_event("tool_call", name=name)

    async def send_processing(self, active: bool):
        await self.server.send_event("processing" if active else "processing_done")

    async def process_message(self, content_parts: list, user_message_id=None):
        await self.agent.process_message(content_parts, user_message_id)

    async def inject_message(self, text: str):
        await self.server.send_chat(text, role="user")
