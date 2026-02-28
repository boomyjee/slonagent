"""HindsightProvider — интеграция с Hindsight.

Поддерживает два режима:
- Встроенный сервер (base_url не передан): запускает HindsightServer в фоновом потоке,
  использует embedded PostgreSQL. Веб-панель доступна по адресу, выводимому в лог.
- Внешний сервер (base_url передан): подключается к уже запущенному Hindsight-серверу
  (Docker, Podman и т.п.).

Отправляет диалоги в Hindsight (retain), который извлекает факты, строит граф сущностей
и сохраняет документы с возможностью восстановить оригинал. Поиск (recall) возвращает
релевантные факты с учётом временного контекста и связей между сущностями.
"""
import logging
from datetime import datetime
from typing import Annotated

from agent import tool
from src.memory.providers.base import BaseProvider

log = logging.getLogger(__name__)


class HindsightProvider(BaseProvider):
    """
    Провайдер памяти на базе Hindsight.

    Если base_url не передан — запускает встроенный сервер (embedded PostgreSQL,
    веб-панель доступна локально). Если base_url передан — подключается к внешнему
    серверу (Docker/Podman).

    Логика:
    - _consolidate(): накопленные ходы → retain() (по лимиту токенов)
    - get_context_prompt(): recall() по тексту пользователя → в системный промпт
    - Тулы hindsight_recall / hindsight_reflect для явного поиска
    """

    def __init__(
        self,
        base_url: str | None = None,
        bank_id: str = "slonagent",
        api_key: str | None = None,
        llm_provider: str = "gemini",
        llm_api_key: str | None = None,
        llm_model: str | None = None,
        consolidate_tokens: int = 3_000,
        recall_max_tokens: int = 2_000,
    ):
        """
        Args:
            base_url: URL сервера. Если None — запускается встроенный сервер.
            bank_id: Идентификатор банка памяти.
            api_key: API-ключ для авторизации на Hindsight-сервере (если есть).
            llm_provider: LLM-провайдер для встроенного сервера (gemini, openai, groq, ...).
            llm_api_key: API-ключ LLM для встроенного сервера.
            llm_model: Имя модели для встроенного сервера.
            consolidate_tokens: Порог токенов для консолидации.
            recall_max_tokens: Максимум токенов в ответе recall.
        """
        super().__init__(consolidate_tokens=consolidate_tokens)
        self._base_url = base_url
        self._bank_id = bank_id
        self._api_key = api_key
        self._llm_provider = llm_provider
        self._llm_api_key = llm_api_key
        self._llm_model = llm_model
        self._recall_max_tokens = recall_max_tokens
        self._server = None
        if base_url is None:
            self._start_server()
        from hindsight_client import Hindsight
        self._client = Hindsight(base_url=self._base_url, api_key=self._api_key)

    def _start_server(self):
        from hindsight import HindsightServer
        server = HindsightServer(
            db_url="pg0://slonagent",
            llm_provider=self._llm_provider,
            llm_api_key=self._llm_api_key or "",
            llm_model=self._llm_model or "",
        )
        server.start()
        self._server = server
        self._base_url = server.url
        log.info("[HindsightProvider] embedded server started at %s", self._base_url)

    async def _recall(self, query: str, max_tokens: int, budget: str = "mid") -> list:
        response = await self._client.arecall(
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
                    items.append({
                        "content": part["text"],
                        "context": "attached document",
                        "document_id": doc_id,
                    })
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
            await self._client.aretain_batch(
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
        doc_by_id: dict[str, list[str]] = {}
        for text, doc_id in results:
            if doc_id:
                doc_by_id.setdefault(doc_id, []).append(f"  - {text}")
        parts = []
        if conv_lines:
            parts.append("Из разговоров:\n" + "\n".join(conv_lines))
        for doc_id, lines in doc_by_id.items():
            parts.append(f"Из документа {doc_id} (факты принадлежат документу, не реальным событиям):\n" + "\n".join(lines))
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
            results = [{"text": t, "document_id": d} for t, d in raw]
            return {"results": results, "count": len(results)}
        except Exception as e:
            log.warning("[HindsightProvider] recall tool failed: %s", e)
            return {"error": str(e)}

    @tool("Получить полный текст документа из памяти по его document_id.")
    async def hindsight_get_document(
        self,
        document_id: Annotated[str, "ID документа (из результатов recall)"],
    ) -> dict:
        try:
            from hindsight_client_api import ApiClient, Configuration
            from hindsight_client_api.api import DocumentsApi

            config = Configuration(host=self._base_url)
            async with ApiClient(config) as api_client:
                if self._api_key:
                    api_client.set_default_header("Authorization", f"Bearer {self._api_key}")
                api = DocumentsApi(api_client)
                doc = await api.get_document(bank_id=self._bank_id, document_id=document_id)
                return {
                    "document_id": doc.id,
                    "original_text": doc.original_text,
                    "memory_unit_count": doc.memory_unit_count,
                    "created_at": str(doc.created_at),
                }
        except Exception as e:
            log.warning("[HindsightProvider] get_document failed: %s", e)
            return {"error": str(e)}

    @tool("Глубокий анализ памяти с рассуждением. Используй для сложных вопросов о прошлом.")
    async def hindsight_reflect(
        self,
        query: Annotated[str, "Вопрос для анализа"],
    ) -> dict:
        try:
            response = await self._client.areflect(
                bank_id=self._bank_id,
                query=query,
                budget="low",
            )
            return {"answer": getattr(response, "answer", str(response))}
        except Exception as e:
            log.warning("[HindsightProvider] reflect failed: %s", e)
            return {"error": str(e)}
