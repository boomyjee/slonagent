"""HindsightProvider — интеграция с Hindsight Engine (embedded, без сервера).

Использует MemoryEngine напрямую с embedded PostgreSQL (pg0) и локальными эмбеддингами.
Функционально эквивалентен HindsightApiProvider, но не требует запущенного сервера.

Установка зависимости:
    pip install -e lib/hindsight/hindsight-api
"""
import logging, os
from datetime import datetime
from typing import Annotated

from agent import tool
from src.memory.providers.base import BaseProvider
from src.memory.memory import Memory

log = logging.getLogger(__name__)


class HindsightProvider(BaseProvider):
    def __init__(
        self,
        model_name: str,
        api_key: str,
        bank_id: str = "slonagent",
        embedding_model: str = "BAAI/bge-small-en-v1.5",
        consolidate_tokens: int = 3_000,
        recall_max_tokens: int = 2_000,
    ):
        super().__init__(consolidate_tokens=consolidate_tokens)
        self.model_name = model_name
        self.api_key = api_key
        self.bank_id = bank_id
        self.embedding_model = embedding_model
        self.recall_max_tokens = recall_max_tokens
        self._engine = None
        self._ctx = None

    async def start(self):
        from hindsight_api.engine.memory_engine import MemoryEngine
        from hindsight_api.engine.task_backend import SyncTaskBackend
        from hindsight_api.engine.embeddings import LocalSTEmbeddings
        from hindsight_api.models import RequestContext

        db_path = os.path.join(Memory.memory_dir, "hindsight", "pg0")
        os.makedirs(db_path, exist_ok=True)

        self._engine = MemoryEngine(
            db_url=f"pg0:{db_path}",
            memory_llm_provider="gemini",
            memory_llm_api_key=self.api_key,
            memory_llm_model=self.model_name,
            embeddings=LocalSTEmbeddings(model_name=self.embedding_model),
            task_backend=SyncTaskBackend(),
        )
        await self._engine.initialize()
        self._ctx = RequestContext(internal=True)
        log.info("[HindsightProvider] engine initialized, bank=%s", self.bank_id)

    # ── consolidate ───────────────────────────────────────────────────────────

    async def _consolidate(self, pending: list):
        if not self._engine:
            log.warning("[HindsightProvider] engine not initialized, skipping consolidation")
            return

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
                        event["event_date"] = ts
                    items.append(event)
                else:
                    item: dict = {
                        "content": f"{label}: {part['text']}",
                        "context": "conversation",
                    }
                    if ts:
                        item["event_date"] = ts
                    items.append(item)

        if not items:
            return
        try:
            await self._engine.retain_batch_async(
                bank_id=self.bank_id,
                contents=items,
                request_context=self._ctx,
            )
            n_doc = sum(1 for i in items if "document_id" in i)
            log.info("[HindsightProvider] retain_batch %d items (%d doc, %d conv)", len(items), n_doc, len(items) - n_doc)
        except Exception as e:
            log.warning("[HindsightProvider] retain_batch failed: %s", e)

    # ── context ───────────────────────────────────────────────────────────────

    async def _recall(self, query: str, max_tokens: int, budget: str = "mid") -> list:
        from hindsight_api.engine.memory_engine import Budget
        result = await self._engine.recall_async(
            bank_id=self.bank_id,
            query=query,
            budget=Budget(budget),
            max_tokens=max_tokens,
            request_context=self._ctx,
        )
        return [
            (f.text, getattr(f, "document_id", None))
            for f in (result.results or []) if getattr(f, "text", None)
        ]

    async def get_context_prompt(self, user_text: str = "") -> str:
        if not self._engine or not user_text:
            return ""
        try:
            results = await self._recall(user_text[:1500], self.recall_max_tokens)
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
            doc = await self._engine.get_document(
                document_id=document_id,
                bank_id=self.bank_id,
                request_context=self._ctx,
            )
            if not doc:
                return {"error": f"Документ {document_id} не найден."}
            return {
                "document_id": doc.get("id", document_id),
                "original_text": doc.get("original_text"),
                "memory_unit_count": doc.get("memory_unit_count"),
                "created_at": str(doc.get("created_at", "")),
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
            result = await self._engine.reflect_async(
                bank_id=self.bank_id,
                query=query,
                request_context=self._ctx,
            )
            return {"answer": result.text}
        except Exception as e:
            log.warning("[HindsightProvider] reflect failed: %s", e)
            return {"error": str(e)}
