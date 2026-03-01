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
                self.dashboard.call_from_thread(self.dashboard.add_chat, "user", text)
            transport.on_user_message = _on_user_message

    async def start(self):
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)
        handler = UILogHandler(self.dashboard)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root_logger.addHandler(handler)

        await asyncio.gather(
            self.dashboard.run_async(),
            self._t.start(),
        )

    async def send_message(self, text: str):
        self.dashboard.call_from_thread(self.dashboard.add_chat, "assistant", text)
        return await self._t.send_message(text)

    def __getattr__(self, name: str):
        return getattr(self._t, name)
