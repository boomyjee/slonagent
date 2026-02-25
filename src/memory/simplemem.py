import asyncio, logging, os, sys, uuid
from typing import Annotated
from agent import tool
from src.memory.base import BaseMemory

_LIB = os.path.join(os.path.dirname(__file__), "..", "..", "lib", "SimpleMem")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)


class SimpleMemMemory(BaseMemory):
    def __init__(self, memory_dir: str = None, hard_limit_tokens: int = 500_000, soft_limit_tokens: int = 50_000, min_user_turns: int = 10, consolidate_tokens: int = 20_000):
        super().__init__(hard_limit_tokens=hard_limit_tokens, soft_limit_tokens=soft_limit_tokens, min_user_turns=min_user_turns, consolidate_tokens=consolidate_tokens)

        root = os.path.dirname(os.path.abspath(sys.modules["__main__"].__file__))
        base = memory_dir or os.path.join(root, "memory", "simplemem")
        os.makedirs(base, exist_ok=True)

        from main import SimpleMemSystem
        from cross.orchestrator import create_orchestrator

        simplemem = SimpleMemSystem(
            db_path=os.path.join(base, "simplemem_lancedb"),
        )
        self._orch = create_orchestrator(
            project="slonagent",
            db_path=os.path.join(base, "cross_memory.db"),
            lancedb_path=os.path.join(base, "lancedb"),
            simplemem=simplemem,
        )

    def get_context_prompt(self) -> str:
        try:
            ctx = self._orch.get_context_for_prompt()
            if ctx:
                return f"## Долгосрочная память\n{ctx}"
        except Exception as e:
            logging.debug("[SimpleMemMemory] get_context_for_prompt: %s", e)
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

    async def _consolidate(self, pending):
        messages = [
            (t["role"], " ".join(p.get("text", "") for p in t.get("parts", []) if isinstance(p, dict) and "text" in p).strip())
            for t in pending
            if isinstance(t, dict) and t.get("role") in ("user", "model")
        ]
        messages = [(role, text) for role, text in messages if text]
        if not messages:
            return
        try:
            user_prompt = next((text for role, text in messages if role == "user"), "")
            result = await self._orch.start_session(
                content_session_id=str(uuid.uuid4()),
                user_prompt=user_prompt,
            )
            session_id = result["memory_session_id"]

            for role, text in messages:
                await self._orch.record_message(session_id, text, role=role)

            await self._orch.stop_session(session_id)
            await self._orch.end_session(session_id)
            logging.info("[SimpleMemMemory] сессия сохранена.")
        except Exception as e:
            logging.error("[SimpleMemMemory] ошибка: %s", e)
