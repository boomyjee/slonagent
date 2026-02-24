import asyncio, logging, os, sys, uuid
from typing import Annotated
from agent import Skill, tool

_LIB = os.path.join(os.path.dirname(__file__), "lib", "SimpleMem")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)


class SimplememSkill(Skill):
    def __init__(self, memory_dir: str = None):
        super().__init__()

        root = os.path.dirname(os.path.abspath(sys.modules["__main__"].__file__))
        base = memory_dir or os.path.join(root, "memory", "simplemem")
        os.makedirs(base, exist_ok=True)

        from cross.orchestrator import create_orchestrator
        self._orch = create_orchestrator(
            project="slonagent",
            db_path=os.path.join(base, "cross_memory.db"),
            lancedb_path=os.path.join(base, "lancedb"),
        )
        self._session_id: str | None = None
        self._recorded: int = 0

    def get_context_prompt(self) -> str:
        try:
            ctx = self._orch.get_context_for_prompt()
            if ctx:
                return f"## Долгосрочная память\n{ctx}"
        except Exception as e:
            logging.debug("[SimplememSkill] get_context_for_prompt: %s", e)
        return ""

    @tool("Семантический поиск по долгосрочной памяти прошлых диалогов.")
    def search_memory(self, query: Annotated[str, "Поисковый запрос на естественном языке"]) -> dict:
        results = self._orch.search(query, top_k=10)
        if not results:
            return {"result": "Ничего не найдено."}
        return {"results": [{"content": e.content, "session": e.content_session_id} for e in results]}

    @tool("Статистика долгосрочной памяти: сколько сессий, событий и наблюдений сохранено.")
    def memory_stats(self) -> dict:
        return self._orch.get_stats()

    async def on_message_processed(self, messages: list):
        new_messages = messages[self._recorded:]
        if not new_messages:
            return

        try:
            if self._session_id is None:
                first_user = next((m for m in new_messages if m.get("role") == "user"), None)
                user_prompt = ""
                if first_user:
                    parts = first_user.get("parts", [])
                    user_prompt = next(
                        (p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p), ""
                    )
                result = await self._orch.start_session(
                    content_session_id=str(uuid.uuid4()),
                    user_prompt=user_prompt,
                )
                self._session_id = result["memory_session_id"]

            for msg in new_messages:
                role = msg.get("role", "user")
                parts = msg.get("parts", [])
                text = " ".join(
                    p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p
                ).strip()
                if text:
                    await self._orch.record_message(self._session_id, text, role=role)

            self._recorded = len(messages)
            await self._orch.stop_session(self._session_id)
            await self._orch.end_session(self._session_id)
            self._session_id = None
            logging.info("[SimplememSkill] сессия сохранена.")
        except Exception as e:
            logging.error("[SimplememSkill] ошибка: %s", e)
