"""Transport that fans out events to multiple transports."""
import logging
from src.transport.base import BaseTransport

log = logging.getLogger(__name__)


class MultiTransport(BaseTransport):
    """Broadcasts all events to a list of child transports."""

    def __init__(self, transports: list[BaseTransport]):
        super().__init__()
        self.transports = transports

    def set_agent(self, agent):
        super().set_agent(agent)
        for t in self.transports:
            t.set_agent(agent)
            t.set_on_message(lambda parts, uid=None, trigger_answer=True, src=t: self._child_message(src, parts, uid, trigger_answer))

    async def _child_message(self, source, content_parts, user_message_id=None, trigger_answer=True):
        text = "\n".join(p.get("text", "") for p in content_parts if p.get("type") == "text")
        if text:
            for t in self.transports:
                if t is not source:
                    try:
                        await t.inject_message(text)
                    except Exception:
                        log.warning("inject_message to %s failed", type(t).__name__, exc_info=True)
        await self.process_message(content_parts, user_message_id, trigger_answer=trigger_answer)

    async def send_message(self, text, stream_id=None, final=True):
        for t in self.transports:
            await t.send_message(text, stream_id, final=final)

    async def send_thinking(self, text, stream_id=None, final=False):
        for t in self.transports:
            await t.send_thinking(text, stream_id, final=final)

    async def send_memory_info(self, text):
        for t in self.transports:
            await t.send_memory_info(text)

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

    def get_skills(self):
        return [s for t in self.transports for s in t.get_skills()]

    async def inject_message(self, text):
        for t in self.transports:
            await t.inject_message(text)

    async def send_app_url(self, url, text, button=""):
        for t in self.transports:
            await t.send_app_url(url, text, button)

    async def send_images(self, paths):
        for t in self.transports:
            await t.send_images(paths)

    async def send_files(self, paths):
        for t in self.transports:
            await t.send_files(paths)

    async def send_voice(self, audio_path):
        for t in self.transports:
            await t.send_voice(audio_path)

    async def send_suggestions(self, text, options):
        for t in self.transports:
            await t.send_suggestions(text, options)

    async def thread_rename(self, uuid, name):
        for t in self.transports:
            await t.thread_rename(uuid, name)

    async def thread_delete(self, uuid):
        for t in self.transports:
            await t.thread_delete(uuid)

    async def close(self):
        for t in self.transports:
            await t.close()
