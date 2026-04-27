"""CodingMode — sub-agent factory.

Spawns a sub-agent that inherits the parent's transport (which already
includes a DashboardTransport providing the IDE UI). Sends the task and
waits for done.
"""
import logging
from typing import Annotated

from agent import Skill, tool
from src.transport.dashboard import DashboardTransport

log = logging.getLogger(__name__)


class CodingModeSkill(Skill):
    @tool("Запустить кодинг режим с веб-интерфейсом для работы с кодом")
    async def launch(
        self,
        task: Annotated[str, "Задача для кодинг-агента"] = "",
        project_path: Annotated[str, "Путь к проекту"] = "/workspace",
    ) -> dict:
        from src.agent.agent import stoppable
        from src.modes.coding.coding_skill import CodingSkill
        from src.skills.sandbox import SandboxSkill
        from src.skills.web import WebSkill

        parent_sandbox = next((s for s in self.agent.skills if isinstance(s, SandboxSkill)), None)
        parent_web = next((s for s in self.agent.skills if isinstance(s, WebSkill)), None)
        workspace_dir = parent_sandbox.workspace_dir if parent_sandbox else None

        coding_skill = CodingSkill()
        sub = await self.agent.spawn_subagent(
            "coding_mode",
            memory_providers=[],
            skills=[
                coding_skill,
                SandboxSkill(workspace_dir=workspace_dir),
                WebSkill(parent_web.api_key if parent_web else ""),
            ],
        )

        initial = f"Project root: {project_path}"
        if task:
            initial += f"\n\nTask: {task}"
        await sub.memory.add_turn({"role": "user", "content": initial})

        def find(t): return t if isinstance(t, DashboardTransport) else next((d for c in getattr(t, 'transports', []) if (d := find(c))), None)
        dashboard = find(self.agent.transport)
        url = await dashboard.get_url('/') if dashboard else ""
        await self.agent.transport.send_message(
            f"\U0001f4bb Coding mode: {url}\nДля выхода: /stop" if url
            else "\U0001f4bb Coding mode\nДля выхода: /stop"
        )

        await stoppable(sub.loop(), coding_skill.done)

        return {"result": coding_skill.result} if coding_skill.done.is_set() else {"status": "interrupted"}
