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

    @tool("Запустить Claude Code для работы с кодом")
    async def launch(
        self,
        task: Annotated[str, "Задача для Claude Code"] = "",
    ) -> dict:
        from src.skills.config import ConfigSkill
        from src.skills.sandbox import SandboxSkill
        from src.transport.dashboard import DashboardTransport

        sandbox = next((s for s in self.agent.skills if isinstance(s, SandboxSkill)), None)
        if not sandbox:
            return {"error": "Требуется SandboxSkill с Docker-контейнером"}

        # Отбираем у клода ТОЛЬКО доступ к хост-ФС/шеллу — но оставляем Task
        # (субагенты), TodoWrite, WebFetch, WebSearch, ExitPlanMode. Хост-тулы
        # подменяются нашими MCP-обёртками над SandboxSkill: клод видит их
        # как mcp__slon__exec / read / write / replace / grep / glob.
        # Свой инстанс SandboxSkill для subagent'а (skill хранит ссылку на agent),
        # своя песочница — паттерн как в coding mode.
        sub = await self.agent.spawn_subagent(
            "claude_code",
            skills=[SandboxSkill(workspace_dir=sandbox.workspace_dir), ConfigSkill()],
            model_name=self._model,
            backend="claude",
            backend_params={"sdk_options": {"disallowed_tools": [
                "Bash", "Edit", "MultiEdit", "Write", "NotebookEdit",
                "Read", "Grep", "Glob", "KillShell",
            ]}},
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
                "min_recent_turns": 100,
                "max_recent_turns_tokens": 150_000,
                "compress_after_tokens": 40_000,
                "reflect_after_tokens": 50_000,
            },
        )

        def find(t): return t if isinstance(t, DashboardTransport) else next((d for c in getattr(t, 'transports', []) if (d := find(c))), None)
        dashboard = find(self.agent.transport)
        url = await dashboard.get_url('/') if dashboard else ""
        await self.agent.transport.send_message(
            f"\U0001f4bb Claude Code: {url}\nДля выхода: /exit" if url
            else "\U0001f4bb Claude Code\nДля выхода: /exit"
        )

        if task:
            await sub.process_message([{"type": "text", "text": task}])

        await sub.loop()

        return {"status": "finished"}
