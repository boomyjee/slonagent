"""Transport that fans out events to multiple transports."""
from src.transport.base import BaseTransport


class MultiTransport(BaseTransport):
    """Broadcasts all events to a list of child transports.

    Does not call set_agent on children — they keep their own agent binding.
    process_message goes through self.agent to avoid duplicates.
    """

    def __init__(self, transports: list[BaseTransport]):
        super().__init__()
        self.transports = transports

    def set_agent(self, agent):
        self.agent = agent

    async def send_message(self, text, stream_id=None):
        for t in self.transports:
            await t.send_message(text, stream_id)

    async def send_thinking(self, text, stream_id=None, final=False):
        for t in self.transports:
            await t.send_thinking(text, stream_id, final=final)

    async def send_system_prompt(self, text):
        for t in self.transports:
            await t.send_system_prompt(text)

    async def on_tool_call(self, name, args):
        for t in self.transports:
            await t.on_tool_call(name, args)

    async def on_tool_result(self, name, result):
        for t in self.transports:
            await t.on_tool_result(name, result)

    async def send_processing(self, active):
        for t in self.transports:
            await t.send_processing(active)

    async def inject_message(self, text):
        for t in self.transports:
            await t.inject_message(text)

    async def process_message(self, content_parts, user_message_id=None):
        await self.agent.process_message(content_parts, user_message_id)
