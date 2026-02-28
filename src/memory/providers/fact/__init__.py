"""fact/ — FactProvider: локальный аналог HindsightProvider.

Вместо API-вызовов к серверу — напрямую вызывает локальные модули:
  retain   → src.memory.providers.fact.retain
  recall   → src.memory.providers.fact.recall
  reflect  → src.memory.providers.fact.reflect  (consolidation pipeline)
  storage  → src.memory.providers.fact.storage   (SQLite + LanceDB)

Логика — 1 в 1 с HindsightProvider:
  _consolidate()       накопленные ходы → retain() (по лимиту токенов)
                       + фоновая консолидация через reflect.consolidate()
  get_context_prompt() recall() по тексту пользователя → в системный промпт
  fact_recall          явный семантический поиск (≡ hindsight_recall)
  fact_get_document    получить полный текст документа (≡ hindsight_get_document)
  fact_reflect         глубокий анализ: recall + LLM синтез (≡ hindsight_reflect)
"""
import asyncio
import logging
import os
from datetime import datetime
from typing import Annotated, Optional

import httpx
from google import genai

from agent import tool
from src.memory.memory import Memory
from src.memory.providers.base import BaseProvider
from src.memory.providers.fact.retain import NONE_DATE_YEAR, RetainItem, retain
from src.memory.providers.fact.recall import recall_async, RecallResponse
from src.memory.providers.fact.storage import Storage

log = logging.getLogger(__name__)

NONE_DATE = datetime(NONE_DATE_YEAR, 1, 1)

_REFLECT_PROMPT = """\
You are a memory analyst. Based on the facts retrieved from long-term memory, \
answer the question thoroughly and precisely.

Retrieved facts:
{facts}

Question: {query}

Instructions:
- Base your answer ONLY on the facts above.
- If multiple facts are relevant, synthesize them into a coherent answer.
- If facts are insufficient or contradictory, say so explicitly.
- Be concise but complete.
"""


class FactProvider(BaseProvider):
    """
    Провайдер памяти на базе локального Hindsight-совместимого движка.

    Хранит факты в SQLite + LanceDB (без внешнего сервера).
    Встраивание — SentenceTransformer (Qwen3-Embedding-0.6B).
    LLM — тот же genai.Client, что и основной агент.
    """

    EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-0.6B"

    def __init__(
        self,
        model_name: str,
        api_key: str,
        embed_model: str = EMBEDDING_MODEL,
        consolidate_tokens: int = 3_000,
        recall_limit: int = 10,
        recall_max_tokens: int = 2_000,
    ):
        """
        Args:
            model_name:         Имя LLM-модели (Gemini) для извлечения фактов и reflect.
            api_key:            API-ключ Gemini.
            embed_model:        Название SentenceTransformer модели для эмбеддингов.
            consolidate_tokens: Порог токенов накопленных ходов для запуска retain.
            recall_limit:       Количество фактов в auto-recall (get_context_prompt).
            recall_max_tokens:  Мягкое ограничение вывода recall для токенов в контексте.
        """
        super().__init__(consolidate_tokens=consolidate_tokens)
        self._model_name        = model_name
        self._embed_model_name  = embed_model
        self._recall_limit      = recall_limit
        self._recall_max_tokens = recall_max_tokens

        self._embed:   Optional[object]  = None
        self._storage: Optional[Storage] = None

        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        http_client = httpx.Client(proxy=proxy_url) if proxy_url else None
        http_options = (
            {"httpx_client": http_client, "api_version": "v1alpha"}
            if http_client else {"api_version": "v1alpha"}
        )
        self._llm = genai.Client(api_key=api_key, http_options=http_options)

    # ── Embedding ────────────────────────────────────────────────────────────────

    def _get_embed(self):
        if self._embed is None:
            from sentence_transformers import SentenceTransformer
            self._embed = SentenceTransformer(self._embed_model_name)
            log.info(
                "[FactProvider] embedding model loaded: %s, dim=%d",
                self._embed_model_name,
                self._embed.get_sentence_embedding_dimension(),
            )
        return self._embed

    def _embed_fn(self, text_or_texts):
        """
        Универсальная embed функция для Storage.
        Принимает str (одиночный) или list[str] (батч для retain/dedup).
        """
        return self._get_embed().encode(text_or_texts, normalize_embeddings=True).tolist()

    def _encode_query(self, text: str) -> list:
        """Encode для запросов (prompt_name="query" если поддерживается моделью)."""
        embed = self._get_embed()
        if hasattr(embed, "prompts") and "query" in (embed.prompts or {}):
            return embed.encode(text, prompt_name="query", normalize_embeddings=True).tolist()
        return embed.encode(text, normalize_embeddings=True).tolist()

    # ── Storage (lazy) ───────────────────────────────────────────────────────────

    def _get_storage(self) -> Storage:
        if self._storage is None:
            dim          = self._get_embed().get_sentence_embedding_dimension()
            sqlite_path  = os.path.join(Memory.memory_dir, "fact", "facts.db")
            lancedb_path = os.path.join(Memory.memory_dir, "fact", "lancedb")
            self._storage = Storage(sqlite_path, lancedb_path, self._embed_fn, dim)
            log.info("[FactProvider] Storage initialized (dim=%d)", dim)
        return self._storage

    # ── Consolidate (retain pipeline) ────────────────────────────────────────────

    async def _consolidate(self, pending: list) -> None:
        """
        Конвертирует накопленные ходы в RetainItem и запускает retain pipeline.
        Логика идентична HindsightProvider._consolidate.
        """
        items: list[RetainItem] = []

        for turn in pending:
            if not isinstance(turn, dict):
                continue
            label  = "Пользователь" if turn.get("role") == "user" else "Ассистент"
            ts_raw = turn.get("_timestamp")
            ts     = (
                datetime.fromisoformat(ts_raw)
                if isinstance(ts_raw, str) else (ts_raw or datetime.utcnow())
            )
            for part in turn.get("parts", []):
                if not isinstance(part, dict) or "text" not in part:
                    continue
                doc_id = part.get("_document_id")
                if doc_id:
                    items.append(RetainItem(
                        content=part["text"],
                        context="attached document",
                        event_date=NONE_DATE,
                        document_id=doc_id,
                    ))
                    items.append(RetainItem(
                        content=f"{label} загрузил документ {doc_id}",
                        context="document upload event",
                        event_date=ts,
                    ))
                else:
                    items.append(RetainItem(
                        content=f"{label}: {part['text']}",
                        context="conversation",
                        event_date=ts,
                    ))

        if not items:
            return

        storage = await asyncio.to_thread(self._get_storage)
        try:
            await retain(items, self._llm, self._model_name, storage)
            n_doc = sum(1 for i in items if i.document_id)
            log.info(
                "[FactProvider] retain %d items (%d doc, %d conv)",
                len(items), n_doc, len(items) - n_doc,
            )
        except Exception as e:
            log.warning("[FactProvider] retain failed: %s", e)
            return

        try:
            from src.memory.providers.fact.reflect import consolidate
            await consolidate(storage, self._llm, self._model_name)
        except Exception as e:
            log.warning("[FactProvider] background consolidation failed: %s", e)

    # ── Internal recall ──────────────────────────────────────────────────────────

    async def _recall(self, query: str, limit: int | None = None) -> RecallResponse:
        limit   = limit or self._recall_limit
        storage = await asyncio.to_thread(self._get_storage)
        q_vec   = await asyncio.to_thread(self._encode_query, query)
        return await recall_async(query, q_vec, storage, limit=limit)

    # ── Context prompt ───────────────────────────────────────────────────────────

    async def get_context_prompt(self, user_text: str = "") -> str:
        """Автоматический recall по тексту пользователя → системный промпт."""
        if not user_text:
            return ""
        try:
            response = await self._recall(user_text[:1500])
        except Exception as e:
            log.warning("[FactProvider] recall for context failed: %s", e)
            return ""
        if not response.results:
            return ""

        conv_lines: list[str] = []
        doc_by_id: dict[str, list[str]] = {}
        for r in response.results:
            if r.document_id:
                doc_by_id.setdefault(r.document_id, []).append(f"  - {r.fact}")
            else:
                conv_lines.append(f"- {r.fact}")

        parts = []
        if conv_lines:
            parts.append("Из разговоров:\n" + "\n".join(conv_lines))
        for doc_id, lines in doc_by_id.items():
            parts.append(
                f"Из документа {doc_id} (факты документа, не реальные события):\n"
                + "\n".join(lines)
            )

        staleness_note = ""
        if response.is_stale:
            staleness_note = (
                f"\n⚠ Память требует консолидации "
                f"({response.pending_consolidation} необработанных фактов). "
                "Используй fact_reflect для глубокого анализа."
            )

        return (
            "<fact_memories>\n"
            + "\n\n".join(parts)
            + staleness_note + "\n"
            "</fact_memories>\n\n"
            "Управляй долгосрочной памятью: fact_recall, fact_reflect"
        )

    # ── Tools ────────────────────────────────────────────────────────────────────

    @tool("Семантический поиск по долгосрочной памяти (факты, события, контекст).")
    async def fact_recall(
        self,
        query: Annotated[str, "Поисковый запрос"],
        max_facts: Annotated[int, "Максимум фактов в ответе (по умолчанию 10)"] = 10,
    ) -> dict:
        try:
            response = await self._recall(query[:1500], limit=max_facts)
            results = [
                {
                    "fact": r.fact,
                    "fact_type": r.fact_type,
                    "occurred": r.occurred_start,
                    "document_id": r.document_id,
                    "score": round(r.score, 3),
                    "sources": r.sources,
                }
                for r in response.results
            ]
            return {
                "results": results,
                "count": len(results),
                "freshness": response.freshness,
                "pending_consolidation": response.pending_consolidation,
            }
        except Exception as e:
            log.warning("[FactProvider] fact_recall tool failed: %s", e)
            return {"error": str(e)}

    @tool("Получить полный текст документа из памяти по его document_id.")
    async def fact_get_document(
        self,
        document_id: Annotated[str, "ID документа (из результатов fact_recall)"],
    ) -> dict:
        try:
            storage = await asyncio.to_thread(self._get_storage)
            rows = await asyncio.to_thread(
                storage.conn.execute,
                "SELECT chunk_index, chunk_text FROM chunks WHERE document_id = ? ORDER BY chunk_index",
                (document_id,),
            )
            chunks = rows.fetchall()
            if not chunks:
                return {"error": f"Документ {document_id!r} не найден в памяти."}
            return {
                "document_id": document_id,
                "original_text": "\n".join(c["chunk_text"] for c in chunks),
                "chunk_count": len(chunks),
            }
        except Exception as e:
            log.warning("[FactProvider] fact_get_document failed: %s", e)
            return {"error": str(e)}

    @tool("Глубокий анализ памяти с рассуждением. Используй для сложных вопросов о прошлом.")
    async def fact_reflect(
        self,
        query: Annotated[str, "Вопрос для глубокого анализа"],
    ) -> dict:
        try:
            response = await self._recall(query[:1500], limit=30)
            if not response.results:
                return {"answer": "В памяти не найдено релевантных фактов по данному вопросу."}

            facts_text = "\n".join(
                f"- [{r.fact_type}] {r.fact}"
                + (f" (occurred: {r.occurred_start})" if r.occurred_start else "")
                for r in response.results
            )
            llm_response = await asyncio.to_thread(
                self._llm.models.generate_content,
                model=self._model_name,
                contents=[{"role": "user", "parts": [{"text": _REFLECT_PROMPT.format(
                    facts=facts_text, query=query,
                )}]}],
            )
            return {
                "answer": (llm_response.text or "").strip(),
                "facts_used": len(response.results),
                "freshness": response.freshness,
            }
        except Exception as e:
            log.warning("[FactProvider] fact_reflect failed: %s", e)
            return {"error": str(e)}
