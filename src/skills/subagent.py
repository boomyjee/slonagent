import asyncio, logging, tempfile
from typing import Annotated

from agent import Skill, tool
from src.transport.base import BaseTransport


class SubAgentSkill(Skill):
    """Запуск суб-агентов для параллельного выполнения независимых задач."""

    @tool("Запустить одного или нескольких суб-агентов параллельно. Каждый суб-агент работает изолированно и возвращает текстовый результат. Используй для независимых подзадач, параллельного исследования нескольких тем, или когда промежуточная работа не должна засорять основной контекст.")
    async def run_subagents(
        self,
        tasks: Annotated[list[str], "Список задач. Каждая задача — отдельный суб-агент, все запускаются параллельно."],
        system_prompt: Annotated[str, "Системный промпт для всех суб-агентов (необязательно)"] = "",
    ) -> dict:
        from agent import Agent

        class CaptureTransport(BaseTransport):
            def __init__(self):
                super().__init__()
                self.result = ""

            async def send_message(self, text: str, stream_id=None):
                self.result = text
                return stream_id

        class PassthroughCompressor(Skill):
            async def compress(self, turns: list) -> list:
                return turns

        class SystemPromptSkill(Skill):
            def __init__(self, prompt: str):
                super().__init__()
                self._prompt = prompt

            async def get_context_prompt(self, user_text: str = "") -> str:
                return self._prompt

        logging.info("[subagent] spawning %d sub-agent(s)", len(tasks))

        async def run_one(task: str) -> str:
            transport = CaptureTransport()
            extra_skills = [SystemPromptSkill(system_prompt)] if system_prompt else []
            with tempfile.TemporaryDirectory() as tmp_dir:
                sub = Agent(
                    model_name=self.agent.model_name,
                    api_key=self.agent.api_key,
                    agent_dir=tmp_dir,
                    memory_compressor=PassthroughCompressor(),
                    transport=transport,
                    skills=extra_skills,
                )
                await sub.start()
                await sub.process_message(message_parts=[{"text": task}], user_query=task)
            return transport.result

        results = await asyncio.gather(*[run_one(t) for t in tasks])
        return {
            f"subagent_{i + 1}": {"task": t, "result": r}
            for i, (t, r) in enumerate(zip(tasks, results))
        }
