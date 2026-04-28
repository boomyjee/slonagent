"""BaseBackend — контракт бэкенда LLM-вызовов для Agent.

Backend хранит provider-specific state (клиент, сессия, MCP-сервер) и
делает LLM-вызов через свой API. Agent делегирует ему `llm()` и `close()`.
"""


class BaseBackend:
    def __init__(self, agent):
        self.agent = agent

    async def llm(self, tool_choice: str = None, parallel_tool_calls: bool = None,
                  temperature: float = 1.0, max_tokens: int | None = None,
                  system_prompt: str | None = None):
        raise NotImplementedError

    async def close(self):
        pass
