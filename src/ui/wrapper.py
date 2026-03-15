from __future__ import annotations

from src.ui.dashboard import Dashboard, agent_context


def UITransportWrapper(transport_class, dashboard: Dashboard):
    """Returns a subclass of transport_class that mirrors all events to dashboard."""

    class Wrapped(transport_class):
        def __init__(self, *args, agent_id: str = "main", **kwargs):
            self.dashboard = dashboard
            self.agent_id = agent_id
            super().__init__(*args, **kwargs)
            dashboard.register_transport(agent_id, self)

        async def on_user_message(self, text: str) -> None:
            self.dashboard.add_chat("user", text, agent_id=self.agent_id)

        async def send_message(self, text: str, stream_id=None):
            stream_id = await super().send_message(text, stream_id)
            chat_id = id(stream_id) if stream_id is not None else None
            self.dashboard.add_chat("assistant", text, chat_id, agent_id=self.agent_id)
            return stream_id

        async def send_thinking(self, text: str, stream_id=None, final: bool = False):
            stream_id = await super().send_thinking(text, stream_id, final=final)
            thinking_id = id(stream_id) if stream_id is not None else None
            self.dashboard.add_collapsible("[think]", text, thinking_id, agent_id=self.agent_id)
            return stream_id

        async def send_system_prompt(self, text: str):
            label = text.split("\n")[0].strip("[] ")[:40]
            self.dashboard.add_collapsible(f"[sys] {label}", text, agent_id=self.agent_id)
            return await super().send_system_prompt(text)

        async def on_tool_call(self, name: str, args: dict):
            lines = "\n".join(f"  {k}: {v}" for k, v in args.items())
            text = f"[{name}]\n{lines}" if lines else f"[{name}]"
            self.dashboard.add_collapsible(f"[>] {name}", text, agent_id=self.agent_id)
            return await super().on_tool_call(name, args)

        async def on_tool_result(self, name: str, result):
            if isinstance(result, dict):
                parts = [
                    f"<binary {len(v)} bytes>" if isinstance(v, (bytes, bytearray)) else f"[{k}]\n{v}"
                    for k, v in result.items() if v not in (None, "", [], {})
                ]
                text = "\n".join(parts) if parts else "(пусто)"
            elif isinstance(result, (bytes, bytearray)):
                text = f"<binary {len(result)} bytes>"
            else:
                text = str(result)
            self.dashboard.add_collapsible(f"[<] {name}", text, agent_id=self.agent_id)
            return await super().on_tool_result(name, result)

        async def handle_message(self, message):
            agent_context.set(self.agent_id)
            return await super().handle_message(message)


    Wrapped.__name__ = f"UI{transport_class.__name__}"
    return Wrapped
