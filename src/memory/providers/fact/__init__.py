"""fact/ — FactProvider: локальный аналог HindsightProvider.

Вместо API-вызовов к серверу — напрямую вызывает локальные модули:
  retain   → src.memory.providers.fact.retain   (факты + консолидация в конце)
  recall   → src.memory.providers.fact.recall   (семантический поиск)
  reflect  → src.memory.providers.fact.reflect  (агентный цикл для fact_reflect)
  storage  → src.memory.providers.fact.storage  (SQLite + LanceDB)

Логика — 1 в 1 с HindsightProvider:
  _consolidate()       накопленные ходы → retain() (факты + консолидация)
  get_context_prompt() recall() по тексту пользователя → в системный промпт
  fact_recall          явный семантический поиск (≡ hindsight_recall)
  fact_get_document    получить полный текст документа (≡ hindsight_get_document)
  fact_reflect         агентный цикл: search_observations → recall → LLM (≡ hindsight_reflect)
"""
import asyncio
import logging
import os
from datetime import datetime
from typing import Annotated

import httpx
from google import genai

from agent import tool
from src.memory.memory import Memory
from src.memory.providers.base import BaseProvider
from src.memory.providers.fact.retain import RetainItem, retain
from src.memory.providers.fact.recall import recall_async
from src.memory.providers.fact.storage import Storage

log = logging.getLogger(__name__)


class FactProvider(BaseProvider):
    """
    Провайдер памяти на базе локального Hindsight-совместимого движка.

    Хранит факты в SQLite + LanceDB (без внешнего сервера).
    Встраивание — SentenceTransformer (Qwen3-Embedding-0.6B).
    LLM — тот же genai.Client, что и основной агент.
    """

    def __init__(
        self,
        model_name: str,
        api_key: str,
        consolidate_tokens: int = 3_000,
        recall_max_tokens: int = 2_000,
        auto_recall: bool = False,
        auto_consolidate: bool = True,
        retain_mission: str = "",
        custom_instructions: str = "",
        embedding_model: str = "",
        rerank_model: str = "",
    ):
        """
        Args:
            model_name:           Имя LLM-модели (Gemini) для извлечения фактов и reflect.
            api_key:              API-ключ Gemini.
            consolidate_tokens:   Порог токенов накопленных ходов для запуска retain.
            recall_max_tokens:    Мягкий лимит токенов в auto-recall (get_context_prompt).
            auto_recall:          Автоматически подмешивать recall в системный промпт.
            auto_consolidate:     Запускать create_observations после retain.
            retain_mission:       Кастомная миссия для LLM при извлечении фактов.
            custom_instructions:  Замена дефолтных guidelines извлечения (если задано).
            embedding_model:      HuggingFace-имя embedding-модели (по умолчанию Qwen3-Embedding-0.6B).
                                  При смене модели нужно пересоздать БД.
            rerank_model:         FlashRank модель для cross-encoder реранкинга.
        """
        super().__init__(consolidate_tokens=consolidate_tokens)
        self._model_name          = model_name
        self._recall_max_tokens   = recall_max_tokens
        self._auto_recall         = auto_recall
        self._auto_consolidate    = auto_consolidate
        self._retain_mission      = retain_mission
        self._custom_instructions = custom_instructions
        self._rerank_model        = rerank_model

        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        http_client = httpx.Client(proxy=proxy_url) if proxy_url else None
        http_options = (
            {"httpx_client": http_client, "api_version": "v1alpha"}
            if http_client else {"api_version": "v1alpha"}
        )
        self._llm = genai.Client(api_key=api_key, http_options=http_options)

        sqlite_path  = os.path.join(Memory.memory_dir, "fact", "facts.db")
        lancedb_path = os.path.join(Memory.memory_dir, "fact", "lancedb")
        self.storage = Storage(sqlite_path, lancedb_path, embedding_model)
        log.info("[FactProvider] Storage initialized")

    # ── Consolidate (retain pipeline) ────────────────────────────────────────────

    async def _consolidate(self, pending: list) -> None:
        """
        Конвертирует накопленные ходы в RetainItem и запускает retain pipeline.
        Диалоговые реплики конкатенируются в один item чтобы чанкер мог
        разбить их с учётом контекста соседних сообщений.
        """
        conv_lines: list[str] = []
        conv_ts: datetime | None = None
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
                        event_date=None,
                        document_id=doc_id,
                        retain_mission=self._retain_mission,
                        custom_instructions=self._custom_instructions,
                    ))
                    items.append(RetainItem(
                        content=f"{label} загрузил документ {doc_id}",
                        context="document upload event",
                        event_date=ts,
                        retain_mission=self._retain_mission,
                        custom_instructions=self._custom_instructions,
                    ))
                else:
                    if conv_ts is None:
                        conv_ts = ts
                    ts_prefix = ts.strftime("%Y-%m-%d %H:%M")
                    conv_lines.append(f"[{ts_prefix}] {label}: {part['text']}")

        if conv_lines:
            items.insert(0, RetainItem(
                content="\n".join(conv_lines),
                context="conversation",
                event_date=conv_ts,
                retain_mission=self._retain_mission,
                custom_instructions=self._custom_instructions,
            ))

        if not items:
            return

        retain(items, self._llm, self._model_name, self.storage,
               with_observations=self._auto_consolidate)

    async def _recall_text(self, query: str, query_label: str, max_tokens: int | None = None, budget: str = "mid") -> str:
        """Recall + форматирование результатов в читаемый текст для агента."""
        q_vec = await asyncio.to_thread(self.storage.encode_query, query)
        response = await recall_async(
            query, q_vec, self.storage,
            max_tokens=max_tokens or self._recall_max_tokens,
            budget=budget,
            rerank_model=self._rerank_model,
        )
        if not response.results:
            return ""

        conv_lines: list[str] = []
        obs_lines: list[str] = []
        doc_by_id: dict[str, list[str]] = {}
        for r in response.results:
            if r.document_id:
                doc_by_id.setdefault(r.document_id, []).append(f"  - {r.fact}")
            elif r.fact_type == "observation":
                obs_lines.append(f"- {r.fact}")
            else:
                conv_lines.append(f"- {r.fact}")

        parts = []
        if conv_lines:
            parts.append("Из разговоров:\n" + "\n".join(conv_lines))
        if obs_lines:
            parts.append("Синтезированные наблюдения (выведены из фактов):\n" + "\n".join(obs_lines))
        for doc_id, lines in doc_by_id.items():
            parts.append(
                f"Из документа [id={doc_id}] (факты документа, не реальные события):\n"
                + "\n".join(lines)
            )

        if not parts:
            return ""

        if response.is_stale:
            parts.append(
                f"⚠ Память не до конца обработана "
                f"({response.pending_consolidation} необработанных фактов — наблюдения ещё синтезируются)."
            )

        body = "\n\n".join(parts)
        return (
            f'<fact_memories sorted_by_relevance="1" query="{query_label}">\n'
            + body + "\n"
            "</fact_memories>\n\n"
            "Управляй долгосрочной памятью: fact_recall, fact_reflect"
        )

    async def get_context_prompt(self, user_text: str = "") -> str:
        """Автоматический recall по тексту пользователя → системный промпт."""
        if not self._auto_recall:
            return (
                "Если для ответа нужны конкретные факты — о пользователе, его жизни, "
                "предпочтениях, людях, событиях, проектах, документах или любых других "
                "темах из прошлых разговоров — сделай fact_recall перед ответом. "
                "Не угадывай то, что можно найти в памяти."
            )
        if not user_text:
            return ""
        try:
            return await self._recall_text(user_text[:1500], query_label="$LAST_USER_MESSAGE")
        except Exception as e:
            log.warning("[FactProvider] recall for context failed: %s", e, exc_info=True)
            return ""

    # ── Tools ────────────────────────────────────────────────────────────────────

    @tool(
        "Быстрый семантический поиск по долгосрочной памяти. "
        "Используй для конкретных фактов: кто, что, когда, где. "
        "Запрос — утверждение или развёрнутый вопрос, не ключевые слова: "
        "вместо 'Анна работа' пиши 'где работает Анна и кем'. "
        "Для сложных вопросов (анализ, сравнение, выводы из нескольких фактов) используй fact_reflect."
    )
    async def recall(
        self,
        query: Annotated[str, "Поисковый запрос — утверждение или вопрос, не набор ключевых слов"],
        max_tokens: Annotated[int, "Мягкий лимит токенов в ответе (по умолчанию 2000)"] = 2_000,
        budget: Annotated[str, "Глубина поиска: low / mid / high (по умолчанию mid)"] = "mid",
    ) -> dict:
        try:
            body = await self._recall_text(query[:1500], query_label="$TOOL_QUERY", max_tokens=max_tokens, budget=budget)
            return {"memories": body or "Ничего не найдено."}
        except Exception as e:
            log.warning("[FactProvider] recall tool failed: %s", e, exc_info=True)
            return {"error": str(e)}

    @tool("Получить полный текст документа из памяти по его document_id.")
    async def get_document(
        self,
        document_id: Annotated[str, "ID документа (из результатов factprovider_recall)"],
    ) -> dict:
        try:
            rows = await asyncio.to_thread(
                self.storage.conn.execute,
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
            log.warning("[FactProvider] get_document failed: %s", e, exc_info=True)
            return {"error": str(e)}

    @tool(
        "Глубокий анализ памяти с рассуждением через агентный цикл поиска. "
        "Используй когда нужно: сравнить факты, сделать вывод из нескольких событий, "
        "ответить на 'почему', 'как изменилось', 'что общего'. "
        "Медленнее fact_recall — не используй для простых фактических вопросов."
    )
    async def reflect(
        self,
        query: Annotated[str, "Вопрос для глубокого анализа"],
    ) -> dict:
        try:
            from src.memory.providers.fact.reflect import run_reflect_agent
            return await run_reflect_agent(
                query=query[:1500],
                storage=self.storage,
                llm_client=self._llm,
                model_name=self._model_name,
                rerank_model=self._rerank_model,
            )
        except Exception as e:
            log.warning("[FactProvider] reflect failed: %s", e, exc_info=True)
            return {"error": str(e)}
