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
        from src.skills.sandbox import SandboxSkill
        from src.transport.dashboard import DashboardTransport

        sandbox = next((s for s in self.agent.skills if isinstance(s, SandboxSkill)), None)
        if not sandbox:
            return {"error": "Требуется SandboxSkill с Docker-контейнером"}

        sub = await self.agent.spawn_subagent(
            "claude_code",
            skills=[],
            model_name=self._model,
            backend="claude",
            memory_compressor={
                "__class__": "src.memory.compressors.log.LogCompressor",
                "model_name": "haiku",
                "backend": "claude",
                "backend_params": {"sdk_options": {
                    "system_prompt": None,
                    "setting_sources": None,
                    "tools": [],
                }},
                "recent_tokens": 80_000,
                "compress_after_tokens": 200_000,
                "reflect_after_tokens": 300_000,
            },
        )

        def find(t): return t if isinstance(t, DashboardTransport) else next((d for c in getattr(t, 'transports', []) if (d := find(c))), None)
        dashboard = find(self.agent.transport)
        url = await dashboard.get_url('/') if dashboard else ""
        await self.agent.transport.send_message(
            f"\U0001f4bb Claude Code: {url}\nДля выхода: /stop" if url
            else "\U0001f4bb Claude Code\nДля выхода: /stop"
        )

        if task:
            await sub.process_message([{"type": "text", "text": task}])

        await stoppable(sub.loop(), sub._stop_event)

        return {"status": "finished"}
