import logging
from typing import Annotated

from agent import Skill, tool, bypass

log = logging.getLogger(__name__)


class ClaudeCodeSkill(Skill):
    def __init__(self, model: str, expose_tool: bool = True):
        super().__init__()
        self._model = model
        self._expose_tool = expose_tool

    def get_tools(self):
        return super().get_tools() if self._expose_tool else []

    @bypass("claude", "Запустить Claude Code", standalone=True)
    async def launch_command(self, args: str):
        self.agent.call_before_next_message(self.launch(task=args))

    @tool("Запустить Claude Code для работы с кодом в указанной папке")
    async def launch(
        self,
        task: Annotated[str, "Задача для Claude Code"] = "",
        project_path: Annotated[str, "Путь к проекту"] = "",
    ) -> dict:
        from src.agent.agent import stoppable
        from src.modes.coding import CodingTransport
        from src.skills.sandbox import SandboxSkill
        from src.transport.multi import MultiTransport

        sandbox = next((s for s in self.agent.skills if isinstance(s, SandboxSkill)), None)
        if not sandbox:
            return {"error": "Требуется SandboxSkill с Docker-контейнером"}

        coding_transport = CodingTransport(project_path or "/workspace")
        coding_transport.resolve_path = sandbox.resolve_path
        coding_transport.workspace_host_dir = sandbox.workspace_dir
        coding_transport.start_watcher()

        sub = await self.agent.spawn_subagent(
            "claude_code",
            __class__="src.agent.claude_agent.ClaudeAgent",
            skills=[],
            transport=MultiTransport([self.agent.transport, coding_transport]),
            model_name=self._model,
        )

        url = await coding_transport.get_url('/')
        await self.agent.transport.send_message(
            f"\U0001f4bb Claude Code: {url}\nДля выхода: /stop"
        )

        if task:
            await sub.process_message([{"type": "text", "text": task}])

        try:
            await stoppable(sub.loop(), sub._stop_event)
        finally:
            coding_transport.cleanup()

        return {"status": "finished"}
