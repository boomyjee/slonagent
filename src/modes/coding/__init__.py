import asyncio
import json
import logging
from typing import Annotated
from agent import Skill, tool
from src.modes.coding.server import CodingServer

log = logging.getLogger(__name__)


async def start_tunnel(port, subdomain, sish_domain, sish_port, sish_key):
    import asyncssh
    logging.getLogger("asyncssh").setLevel(logging.WARNING)
    key = asyncssh.import_private_key(sish_key)
    conn = await asyncssh.connect(
        sish_domain, sish_port, known_hosts=None, client_keys=[key], username="tunnel",
    )
    await conn.forward_remote_port(subdomain, 80, "localhost", port)
    url = f"https://{subdomain}.{sish_domain}:8443"
    log.info("[coding] tunnel URL: %s", url)
    return url, conn


class CodingModeSkill(Skill):
    def __init__(self, port: int = 3200, sish_port: int = 2222, sish_domain: str = "", sish_key: str = ""):
        super().__init__()
        self._port = port
        self._sish_port = sish_port
        self._sish_domain = sish_domain
        self._sish_key = sish_key

    @tool("Запустить кодинг режим с веб-интерфейсом для работы с кодом")
    async def start_coding(self, project_path: Annotated[str, "Путь к проекту (например /workspace или /mnt/c/dev/myproject)"] = "/workspace") -> dict:
        transport = self.agent.transport

        from src.skills.coding import CodingSkill
        from src.skills.sandbox import SandboxSkill
        from src.skills.web import WebSkill

        # Spawn subagent first — server needs its resolve_path
        parent_web = next((s for s in self.agent.skills if isinstance(s, WebSkill)), None)
        sub = await self.agent.spawn_subagent(
            "coding_mode",
            memory_providers=[],
            skills=[CodingSkill(), SandboxSkill(), WebSkill(parent_web.api_key if parent_web else "")],
        )
        sub.memory.clear()
        await sub.memory.add_turn({"role": "user", "content": f"Project root: {project_path}"})

        sub_sandbox = next(s for s in sub.skills if isinstance(s, SandboxSkill))
        server = CodingServer(self._port, sub_sandbox.resolve_path, project_path)
        await server.start()

        # Tunnel
        tunnel_conn = None
        import random, string
        session_id = ''.join(random.choices(string.ascii_lowercase, k=6))
        try:
            url, tunnel_conn = await asyncio.wait_for(
                start_tunnel(self._port, f"code-{session_id}", self._sish_domain, self._sish_port, self._sish_key),
                timeout=10,
            )
        except Exception as e:
            await transport.send_message(f"Не удалось запустить туннель: {e}")
            return {"error": str(e)}

        await transport.send_message(f"💻 Coding mode: {url}")

        # Route ws messages into subagent queue + show in Telegram
        async def _ws_to_agent():
            while True:
                text = await server.wait_for_chat()
                await transport.inject_message(text)
                await transport.process_message(content_parts=[{"type": "text", "text": text}])

        asyncio.create_task(_ws_to_agent())

        # Chat loop — listens to subagent queue (both ws and Telegram)
        try:
            while True:
                content_parts, _ = await sub.next_message()
                msg = " ".join(p.get("text", "") for p in content_parts if isinstance(p, dict)).strip()
                await server.send_chat(msg, role="user")
                await sub.memory.add_turn({"role": "user", "content": content_parts})

                tool_calls, text = await sub.llm()
                if text:
                    await server.send_chat(text)

                while tool_calls:
                    for tc in tool_calls:
                        name = tc["function"]["name"]
                        args = json.loads(tc["function"].get("arguments") or "{}")
                        await server.send_tool_call(name, args)

                    await sub.dispatch_tool_calls(tool_calls)
                    await server.send_event("files_changed")

                    tool_calls, text = await sub.llm()
                    if text:
                        await server.send_chat(text)
        finally:
            if tunnel_conn:
                tunnel_conn.close()

        return {"status": "done"}
