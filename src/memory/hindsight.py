import asyncio
import concurrent.futures
import logging
from typing import Annotated

from agent import tool
from src.memory.base import BaseProvider

log = logging.getLogger(__name__)

RECALL_TIMEOUT = 10  # секунды


def _turns_to_text(turns: list) -> str:
    lines = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role", "")
        text = " ".join(
            p.get("text", "") for p in turn.get("parts", [])
            if isinstance(p, dict) and "text" in p
        ).strip()
        if text:
            label = "Пользователь" if role == "user" else "Ассистент"
            lines.append(f"{label}: {text}")
    return "\n".join(lines)


class HindsightProvider(BaseProvider):
    """
    Провайдер памяти на базе Hindsight.

    Требует работающий сервер (Docker/Podman):
      start_hindsight_server.bat

    Логика:
    - _consolidate(): накопленные ходы → retain() (по лимиту токенов)
    - get_context_prompt(): recall() по тексту пользователя → в системный промпт
    - Тулы hindsight_recall / hindsight_reflect для явного поиска
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8888",
        bank_id: str = "slonagent",
        consolidate_tokens: int = 3_000,
        recall_max_tokens: int = 2_000,
    ):
        super().__init__(consolidate_tokens=consolidate_tokens)
        self._base_url = base_url
        self._bank_id = bank_id
        self._recall_max_tokens = recall_max_tokens
        self._client = None

    def _get_client(self):
        if self._client is None:
            from hindsight_client import Hindsight
            self._client = Hindsight(base_url=self._base_url)
        return self._client

    def _recall_sync(self, query: str, max_tokens: int, budget: str = "mid") -> list[str]:
        """
        Вызывает recall() в отдельном потоке.

        hindsight_client.recall() внутри использует asyncio.run(), которое
        падает если уже есть running event loop. Запуск в ThreadPoolExecutor
        создаёт чистый поток без event loop — там asyncio.run() работает нормально.
        """
        def _call():
            response = self._get_client().recall(
                bank_id=self._bank_id,
                query=query,
                max_tokens=max_tokens,
                budget=budget,
            )
            return [r.text for r in (response.results or []) if getattr(r, "text", None)]

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_call).result(timeout=RECALL_TIMEOUT)

    # ── consolidate ───────────────────────────────────────────────────────────

    async def _consolidate(self, pending: list):
        text = _turns_to_text(pending)
        if not text:
            return
        try:
            await asyncio.to_thread(
                self._get_client().retain,
                bank_id=self._bank_id,
                content=text,
            )
            log.info("[HindsightProvider] retained %d chars", len(text))
        except Exception as e:
            log.warning("[HindsightProvider] retain failed: %s", e)

    # ── context ───────────────────────────────────────────────────────────────

    def get_context_prompt(self, user_text: str = "") -> str:
        if not user_text:
            return ""
        try:
            results = self._recall_sync(user_text, self._recall_max_tokens)
        except Exception as e:
            log.warning("[HindsightProvider] recall failed: %s", e)
            return ""
        if not results:
            return ""
        memories = "\n".join(f"- {r}" for r in results)
        return (
            "<hindsight_memories>\n"
            "Релевантные воспоминания из прошлых разговоров:\n"
            f"{memories}\n"
            "</hindsight_memories>\n\n"
            "Управляй долгосрочной памятью: hindsight_recall, hindsight_reflect"
        )

    # ── tools ─────────────────────────────────────────────────────────────────

    @tool("Семантический поиск по долгосрочной памяти (факты, события, контекст).")
    async def hindsight_recall(
        self,
        query: Annotated[str, "Поисковый запрос"],
        max_tokens: Annotated[int, "Максимум токенов в ответе (по умолчанию 2000)"] = 2_000,
    ) -> dict:
        try:
            results = await asyncio.to_thread(self._recall_sync, query, max_tokens, "high")
            return {"results": results, "count": len(results)}
        except Exception as e:
            log.warning("[HindsightProvider] recall tool failed: %s", e)
            return {"error": str(e)}

    @tool("Глубокий анализ памяти с рассуждением. Используй для сложных вопросов о прошлом.")
    async def hindsight_reflect(
        self,
        query: Annotated[str, "Вопрос для анализа"],
    ) -> dict:
        try:
            def _call():
                response = self._get_client().reflect(
                    bank_id=self._bank_id,
                    query=query,
                    budget="low",
                )
                return getattr(response, "answer", str(response))

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                answer = pool.submit(_call).result(timeout=30)
            return {"answer": answer}
        except Exception as e:
            log.warning("[HindsightProvider] reflect failed: %s", e)
            return {"error": str(e)}
