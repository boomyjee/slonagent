import logging
from datetime import datetime
from typing import Annotated

from agent import tool
from src.memory.base import BaseProvider

log = logging.getLogger(__name__)


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
        api_key: str | None = None,
        consolidate_tokens: int = 3_000,
        recall_max_tokens: int = 2_000,
    ):
        super().__init__(consolidate_tokens=consolidate_tokens)
        self._base_url = base_url
        self._bank_id = bank_id
        self._api_key = api_key
        self._recall_max_tokens = recall_max_tokens
        self._client = None

    def _get_client(self):
        """Async-клиент для использования в async контексте (один event loop)."""
        if self._client is None:
            from hindsight_client import Hindsight
            self._client = Hindsight(base_url=self._base_url, api_key=self._api_key)
        return self._client

    async def _recall(self, query: str, max_tokens: int, budget: str = "mid") -> list:
        response = await self._get_client().arecall(
            bank_id=self._bank_id,
            query=query,
            max_tokens=max_tokens,
            budget=budget,
        )
        return [
            (r.text, getattr(r, "document_id", None))
            for r in (response.results or []) if getattr(r, "text", None)
        ]

    # ── consolidate ───────────────────────────────────────────────────────────

    async def _consolidate(self, pending: list):
        items = []

        for turn in pending:
            if not isinstance(turn, dict):
                continue
            label = "Пользователь" if turn.get("role") == "user" else "Ассистент"
            ts_raw = turn.get("_timestamp")
            ts = datetime.fromisoformat(ts_raw) if isinstance(ts_raw, str) else ts_raw
            for part in turn.get("parts", []):
                if not isinstance(part, dict) or "text" not in part:
                    continue
                doc_id = part.get("_document_id")
                if doc_id:
                    # факты из документа — без timestamp
                    items.append({
                        "content": part["text"],
                        "context": "attached document",
                        "document_id": doc_id,
                    })
                    # отдельный event: кто и когда загрузил документ
                    event: dict = {
                        "content": f"{label} загрузил документ {doc_id}",
                        "context": "document upload event",
                    }
                    if ts:
                        event["timestamp"] = ts
                    items.append(event)
                else:
                    item: dict = {
                        "content": f"{label}: {part['text']}",
                        "context": "conversation",
                    }
                    if ts:
                        item["timestamp"] = ts
                    items.append(item)

        if not items:
            return
        try:
            await self._get_client().aretain_batch(
                bank_id=self._bank_id,
                items=items,
            )
            n_doc = sum(1 for i in items if "document_id" in i)
            log.info("[HindsightProvider] retain_batch %d items (%d doc, %d conv)", len(items), n_doc, len(items) - n_doc)
        except Exception as e:
            log.warning("[HindsightProvider] retain_batch failed: %s", e)

    # ── context ───────────────────────────────────────────────────────────────

    async def get_context_prompt(self, user_text: str = "") -> str:
        if not user_text:
            return ""
        try:
            results = await self._recall(user_text[:1500], self._recall_max_tokens)
        except Exception as e:
            log.warning("[HindsightProvider] recall failed: %s", e)
            return ""
        if not results:
            return ""
        conv_lines = [f"- {text}" for text, doc_id in results if not doc_id]
        doc_lines  = [f"- {text}" for text, doc_id in results if doc_id]
        parts = []
        if conv_lines:
            parts.append("Из разговоров:\n" + "\n".join(conv_lines))
        if doc_lines:
            parts.append("Из документов (факты принадлежат документам, не реальным событиям):\n" + "\n".join(doc_lines))
        memories = "\n\n".join(parts)
        return (
            "<hindsight_memories>\n"
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
            raw = await self._recall(query[:1500], max_tokens, "high")
            results = [{"text": t, "from_document": bool(d)} for t, d in raw]
            return {"results": results, "count": len(results), "from_documents": sum(1 for r in results if r["from_document"])}
        except Exception as e:
            log.warning("[HindsightProvider] recall tool failed: %s", e)
            return {"error": str(e)}

    @tool("Глубокий анализ памяти с рассуждением. Используй для сложных вопросов о прошлом.")
    async def hindsight_reflect(
        self,
        query: Annotated[str, "Вопрос для анализа"],
    ) -> dict:
        try:
            response = await self._get_client().areflect(
                bank_id=self._bank_id,
                query=query,
                budget="low",
            )
            return {"answer": getattr(response, "answer", str(response))}
        except Exception as e:
            log.warning("[HindsightProvider] reflect failed: %s", e)
            return {"error": str(e)}
