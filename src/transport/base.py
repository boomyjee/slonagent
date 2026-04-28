import logging

log = logging.getLogger(__name__)


class BaseTransport:
    def __init__(self):
        self.agent = None
        self.on_message = None

    def set_agent(self, agent):
        self.agent = agent
        self.on_message = agent.process_message

    def set_on_message(self, callback):
        self.on_message = callback

    def get_agent(self):
        return self.agent

    def get_skills(self):
        return []

    async def send_message(self, text: str, stream_id=None, final: bool = True):
        pass

    async def send_system_prompt(self, text: str):
        pass

    async def send_thinking(self, text: str, stream_id=None, final: bool = False):
        pass
    
    async def send_memory_info(self, text: str, stream_id=None, final: bool = False):
        pass

    async def on_tool_call(self, name: str, args: dict):
        pass

    async def on_tool_result(self, name: str, result):
        pass

    async def process_message(self, content_parts: list, user_message_id=None, trigger_answer: bool = True):
        if self.on_message:
            await self.on_message(content_parts, user_message_id, trigger_answer=trigger_answer)
        else:
            log.warning("process_message called but on_message not set")

    async def send_processing(self, active: bool):
        pass

    async def inject_message(self, text: str):
        pass

    async def send_app_url(self, url: str, text: str, button: str = ""):
        await self.send_message(f"{text}: {url}")
