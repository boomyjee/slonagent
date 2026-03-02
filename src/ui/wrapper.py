from __future__ import annotations

import asyncio
import logging

from src.ui.dashboard import Dashboard, UILogHandler

_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def UITransportWrapper(transport_class):
    """Returns a subclass of transport_class with TUI dashboard mixed in."""

    class Wrapped(transport_class):
        def __init__(self, *args, **kwargs):
            self.dashboard = Dashboard()
            root_logger = logging.getLogger()
            root_logger.setLevel(logging.INFO)
            fmt = logging.Formatter(_LOG_FORMAT)
            console = logging.StreamHandler()
            console.setFormatter(fmt)
            root_logger.addHandler(console)
            web = UILogHandler(self.dashboard)
            web.setFormatter(fmt)
            root_logger.addHandler(web)
            super().__init__(*args, **kwargs)

        async def on_user_message(self, text: str) -> None:
            self.dashboard.call_later(self.dashboard.add_chat, "user", text)

        async def send_message(self, text: str):
            self.dashboard.call_later(self.dashboard.add_chat, "assistant", text)
            return await super().send_message(text)

        async def send_thinking(self, text: str):
            self.dashboard.call_later(self.dashboard.add_collapsible, "[think]", text)
            return await super().send_thinking(text)

        async def send_system_prompt(self, text: str):
            label = text.split("\n")[0].strip("[] ")[:40]
            self.dashboard.call_later(self.dashboard.add_collapsible, f"[sys] {label}", text)
            return await super().send_system_prompt(text)

        async def on_tool_call(self, name: str, args: dict):
            lines = "\n".join(f"  {k}: {v}" for k, v in args.items())
            text = f"[{name}]\n{lines}" if lines else f"[{name}]"
            self.dashboard.call_later(self.dashboard.add_collapsible, f"[>] {name}", text)
            return await super().on_tool_call(name, args)

        async def on_tool_result(self, name: str, result):
            if isinstance(result, dict):
                parts = []
                for k, v in result.items():
                    if v in (None, "", [], {}):
                        continue
                    if isinstance(v, (bytes, bytearray)):
                        parts.append(f"[{k}]\n<binary {len(v)} bytes>")
                    else:
                        parts.append(f"[{k}]\n{v}")
                text = "\n".join(parts) if parts else "(пусто)"
            elif isinstance(result, (bytes, bytearray)):
                text = f"<binary {len(result)} bytes>"
            else:
                text = str(result)
            self.dashboard.call_later(self.dashboard.add_collapsible, f"[<] {name}", text)
            return await super().on_tool_result(name, result)

        async def _web_chat_task(self):
            while True:
                text = await self.dashboard.get_incoming()
                self.dashboard.call_later(self.dashboard.add_chat, "user", text)
                await self.inject_message(text)

        async def start(self):
            dashboard_task = asyncio.create_task(self.dashboard.run_async())
            transport_task = asyncio.create_task(super().start())
            web_chat_task = asyncio.create_task(self._web_chat_task())
            try:
                await asyncio.wait([dashboard_task, transport_task, web_chat_task], return_when=asyncio.FIRST_COMPLETED)
            finally:
                dashboard_task.cancel()
                transport_task.cancel()
                web_chat_task.cancel()
                await asyncio.gather(dashboard_task, transport_task, web_chat_task, return_exceptions=True)

    Wrapped.__name__ = f"UI{transport_class.__name__}"
    return Wrapped
