from __future__ import annotations

import asyncio
import logging

from src.ui.dashboard import Dashboard, UILogHandler

_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


class UITransportWrapper:
    """
    Wraps any transport, intercepting chat messages for the TUI dashboard.
    All other methods are proxied transparently to the original transport.
    """

    def __init__(self, transport) -> None:
        self._t = transport
        self.dashboard = Dashboard()
        if hasattr(transport, "on_user_message"):
            async def _on_user_message(text: str) -> None:
                self.dashboard.call_later(self.dashboard.add_chat, "user", text)
            transport.on_user_message = _on_user_message

        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        handler = UILogHandler(self.dashboard)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root_logger.addHandler(handler)

    def set_agent(self, agent):
        self._t.set_agent(agent)

    async def start(self):
        dashboard_task = asyncio.create_task(self.dashboard.run_async())
        transport_task = asyncio.create_task(self._t.start())
        try:
            await asyncio.wait([dashboard_task, transport_task], return_when=asyncio.FIRST_COMPLETED)
        finally:
            dashboard_task.cancel()
            transport_task.cancel()
            await asyncio.gather(dashboard_task, transport_task, return_exceptions=True)

    async def send_message(self, text: str):
        self.dashboard.call_later(self.dashboard.add_chat, "assistant", text)
        return await self._t.send_message(text)

    def __getattr__(self, name: str):
        return getattr(self._t, name)
