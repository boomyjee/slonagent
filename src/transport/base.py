class BaseTransport:
    def __init__(self):
        self.agent = None

    def set_agent(self, agent):
        self.agent = agent

    async def send_message(self, text: str, stream_id=None):
        return stream_id

    async def send_system_prompt(self, text: str):
        pass

    async def send_thinking(self, text: str, stream_id=None, final: bool = False):
        return stream_id

    async def on_tool_call(self, name: str, args: dict):
        pass

    async def on_tool_result(self, name: str, result):
        pass

    async def process_message(self, content_parts: list, user_message_id=None):
        await self.agent.process_message(content_parts=content_parts, user_message_id=user_message_id)

    async def send_processing(self, active: bool):
        pass

    async def inject_message(self, text: str):
        pass
