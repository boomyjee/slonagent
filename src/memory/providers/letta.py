import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal, Optional

from google.genai import types

from letta.functions.function_sets.base import memory_insert, memory_replace, memory_rethink
from letta.schemas.block import Block
from letta.schemas.enums import AgentType
from letta.schemas.memory import Memory
from letta.services.memory_repo.block_markdown import parse_block_markdown, serialize_block

from agent import tool
from memory import Memory as AppMemory
from src.memory.providers.base import BaseProvider

DEFAULT_LIMIT = 20_000
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIM = 3072


class _State:
    """Минимальная обёртка, которую ожидают чистые функции Letta."""
    def __init__(self, memory: Memory):
        self.memory = memory


class LettaProvider(BaseProvider):
    """
    Провайдер памяти на базе классов Letta:
    - Memory + Block из letta.schemas — in-memory хранение блоков
    - memory_replace / memory_rethink / memory_insert из letta.functions — логика редактирования
    - Memory.compile() — рендеринг блоков в системный промпт
    - serialize_block / parse_block_markdown — персистентность в .md файлах
    - Sleeptime consolidation: отдельный LLM-вызов после накопления токенов
    """

    def __init__(self, consolidate_tokens: int = 2_000):
        super().__init__(consolidate_tokens=consolidate_tokens)
        self._dir = os.path.join(AppMemory.memory_dir, "letta")
        os.makedirs(self._dir, exist_ok=True)
        self._memory = self._load_memory()
        self._archival_table = None  # инициализируется лениво при первом использовании
        self._history_file = os.path.join(self._dir, "history.jsonl")

    # ── persistence ───────────────────────────────────────────────────────────

    def _path(self, label: str) -> str:
        return os.path.join(self._dir, f"{label}.md")

    def _load_memory(self) -> Memory:
        """Загружает все .md файлы из директории в Memory объект Letta."""
        blocks = []
        for filename in sorted(os.listdir(self._dir)):
            if not filename.endswith(".md"):
                continue
            label = filename[:-3]
            with open(os.path.join(self._dir, filename), encoding="utf-8") as f:
                parsed = parse_block_markdown(f.read())
            blocks.append(Block(
                label=label,
                value=parsed.get("value", ""),
                description=parsed.get("description"),
                limit=parsed.get("limit", DEFAULT_LIMIT),
                read_only=parsed.get("read_only", False),
            ))

        if not blocks:
            blocks = [
                Block(label="human", value="", description="Key details about the person you're talking with", limit=DEFAULT_LIMIT),
                Block(label="persona", value="", description="Your personality, values and communication style", limit=DEFAULT_LIMIT),
            ]

        memory = Memory(blocks=blocks, agent_type=AgentType.memgpt_agent)
        self._save_all(memory)
        return memory

    def _save_block(self, block: Block):
        content = serialize_block(
            value=block.value or "",
            description=block.description,
            limit=block.limit,
            read_only=block.read_only,
        )
        with open(self._path(block.label), "w", encoding="utf-8") as f:
            f.write(content)

    def _save_all(self, memory: Memory = None):
        for block in (memory or self._memory).blocks:
            self._save_block(block)

    # ── archival memory (LanceDB + Gemini embeddings) ─────────────────────────

    def _get_archival_table(self):
        """Ленивая инициализация таблицы LanceDB для архивной памяти."""
        if self._archival_table is not None:
            return self._archival_table
        import lancedb
        import pyarrow as pa
        db_path = os.path.join(self._dir, "archival.lancedb")
        db = lancedb.connect(db_path)
        schema = pa.schema([
            pa.field("id", pa.string()),
            pa.field("content", pa.string()),
            pa.field("tags", pa.string()),  # JSON list
            pa.field("created_at", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), EMBEDDING_DIM)),
        ])
        if "archival" not in db.table_names():
            self._archival_table = db.create_table("archival", schema=schema)
        else:
            self._archival_table = db.open_table("archival")
        return self._archival_table

    def _embed(self, text: str) -> list[float]:
        """Получить эмбеддинг через Gemini (синхронно)."""
        resp = self.agent.client.models.embed_content(model=EMBEDDING_MODEL, contents=text)
        return list(resp.embeddings[0].values)

    async def _embed_async(self, text: str) -> list[float]:
        return await asyncio.to_thread(self._embed, text)

    def _archival_count(self) -> int:
        try:
            return len(self._get_archival_table())
        except Exception:
            return 0

    # ── recall memory (conversation history) ──────────────────────────────────

    async def add_turn(self, turn: dict):
        """Записывает каждый ход в history.jsonl, затем вызывает базовую консолидацию."""
        if isinstance(turn, dict):
            role = turn.get("role", "")
            parts = turn.get("parts", [])
            text = " ".join(
                p.get("text", "") for p in parts
                if isinstance(p, dict) and "text" in p
            ).strip()
            if text:
                entry = {
                    "role": role,
                    "text": text,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
                with open(self._history_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        await super().add_turn(turn)

    def _recall_count(self) -> int:
        if not os.path.exists(self._history_file):
            return 0
        with open(self._history_file, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())

    def _recall_load(self) -> list[dict]:
        if not os.path.exists(self._history_file):
            return []
        entries = []
        with open(self._history_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return entries

    # ── context ───────────────────────────────────────────────────────────────

    async def get_context_prompt(self, user_text: str = "") -> str:
        # Memory.compile() из Letta — рендерит все блоки в XML
        compiled = self._memory.compile()

        recall_count = self._recall_count()
        archival_count = self._archival_count()

        stats = []
        if recall_count > 0:
            stats.append(f"{recall_count} сообщений в истории (letta_conversation_search)")
        if archival_count > 0:
            stats.append(f"{archival_count} фактов в архиве (letta_archival_memory_search)")

        memory_stats = ""
        if stats:
            memory_stats = (
                "\n\n<memory_metadata>\n"
                + "\n".join(f"- {s}" for s in stats)
                + "\n</memory_metadata>"
            )

        return (
            f"{compiled}{memory_stats}\n\n"
            "Управляй памятью по ходу разговора:\n"
            "Блоки (всегда в контексте): letta_memory_replace, letta_memory_rethink, "
            "letta_memory_insert, letta_memory_create, letta_memory_delete\n"
            "Архив (семантический поиск): letta_archival_memory_insert, letta_archival_memory_search\n"
            "История диалогов: letta_conversation_search"
        )

    # ── tools ─────────────────────────────────────────────────────────────────

    @tool("Точечная замена строки в блоке памяти. Строка должна встречаться ровно один раз.")
    def memory_replace(
        self,
        label: Annotated[str, "Имя блока"],
        old_string: Annotated[str, "Точный текст для замены"],
        new_string: Annotated[str, "Новый текст (пустая строка — удалить фрагмент)"],
    ) -> dict:
        try:
            # Используем функцию из letta.functions.function_sets.base напрямую
            memory_replace(_State(self._memory), label=label, old_string=old_string, new_string=new_string)
            self._save_block(self._memory.get_block(label))
            logging.info("[LettaProvider] memory_replace: %s", label)
            return {"ok": label}
        except (ValueError, KeyError) as e:
            return {"error": str(e)}

    @tool("Полностью перезаписать блок памяти. Используй для крупных изменений или реорганизации.")
    def memory_rethink(
        self,
        label: Annotated[str, "Имя блока (если не существует — создаётся автоматически)"],
        new_memory: Annotated[str, "Новое полное содержимое блока"],
    ) -> dict:
        try:
            # memory_rethink из base.py создаёт блок если не существует
            memory_rethink(_State(self._memory), new_memory=new_memory, target_block_label=label)
            self._save_block(self._memory.get_block(label))
            logging.info("[LettaProvider] memory_rethink: %s", label)
            return {"ok": label}
        except (ValueError, KeyError) as e:
            return {"error": str(e)}

    @tool("Добавить строку в блок памяти.")
    def memory_insert(
        self,
        label: Annotated[str, "Имя блока"],
        new_string: Annotated[str, "Текст для добавления"],
        insert_line: Annotated[int, "Позиция строки (0 = в начало, -1 = в конец)"] = -1,
    ) -> dict:
        try:
            # memory_insert из base.py
            memory_insert(_State(self._memory), label=label, new_string=new_string, insert_line=insert_line)
            self._save_block(self._memory.get_block(label))
            logging.info("[LettaProvider] memory_insert: %s", label)
            return {"ok": label}
        except (ValueError, KeyError) as e:
            return {"error": str(e)}

    @tool("Создать новый блок памяти.")
    def memory_create(
        self,
        label: Annotated[str, "Имя блока (snake_case, без пробелов)"],
        description: Annotated[str, "Короткое описание назначения блока (1 строка)"],
        content: Annotated[str, "Начальное содержимое (можно пустое)"] = "",
    ) -> dict:
        try:
            self._memory.get_block(label)
            return {"error": f"Блок '{label}' уже существует. Для обновления используй memory_rethink."}
        except KeyError:
            pass
        block = Block(label=label, value=content, description=description, limit=DEFAULT_LIMIT)
        self._memory.blocks.append(block)
        self._save_block(block)
        logging.info("[LettaProvider] memory_create: %s", label)
        return {"created": label}

    @tool("Удалить блок памяти.")
    def memory_delete(
        self,
        label: Annotated[str, "Имя блока для удаления"],
    ) -> dict:
        try:
            self._memory.get_block(label)
        except KeyError:
            return {"error": f"Блок '{label}' не найден. Доступные: {[b.label for b in self._memory.blocks]}"}
        self._memory.blocks = [b for b in self._memory.blocks if b.label != label]
        path = self._path(label)
        if os.path.exists(path):
            os.remove(path)
        logging.info("[LettaProvider] memory_delete: %s", label)
        return {"deleted": label}

    @tool("Сохранить факт в архивную память (долгосрочное хранилище с семантическим поиском).")
    def archival_memory_insert(
        self,
        content: Annotated[str, "Текст факта или наблюдения для сохранения"],
        tags: Annotated[list[str], "Теги для категоризации (например: ['user', 'preference'])"] = None,
    ) -> dict:
        try:
            vector = self._embed(content)
            table = self._get_archival_table()
            row = {
                "id": str(uuid.uuid4()),
                "content": content,
                "tags": json.dumps(tags or [], ensure_ascii=False),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "vector": vector,
            }
            table.add([row])
            return {"ok": True, "archived": content[:80] + ("..." if len(content) > 80 else "")}
        except Exception as e:
            logging.error("[LettaProvider] archival_memory_insert: %s", e)
            return {"error": str(e)}

    @tool("Семантический поиск по архивной памяти.")
    def archival_memory_search(
        self,
        query: Annotated[str, "Поисковый запрос"],
        top_k: Annotated[int, "Количество результатов (по умолчанию 5)"] = 5,
    ) -> dict:
        try:
            count = self._archival_count()
            if count == 0:
                return {"results": [], "note": "Архивная память пуста."}
            vector = self._embed(query)
            table = self._get_archival_table()
            rows = table.search(vector).limit(top_k).to_list()
            results = [
                {"content": r["content"], "tags": json.loads(r["tags"]), "created_at": r["created_at"]}
                for r in rows
            ]
            return {"results": results}
        except Exception as e:
            logging.error("[LettaProvider] archival_memory_search: %s", e)
            return {"error": str(e)}

    @tool("Поиск по истории диалогов (recall memory). Фильтрация по тексту, роли и дате.")
    def conversation_search(
        self,
        query: Annotated[Optional[str], "Текст для поиска (подстрока, регистронезависимо)"] = None,
        roles: Annotated[Optional[list[str]], "Фильтр по роли: ['user'], ['model'], ['user','model']"] = None,
        start_date: Annotated[Optional[str], "Начало периода ISO 8601, например '2024-01-15'"] = None,
        end_date: Annotated[Optional[str], "Конец периода ISO 8601 (включительно)"] = None,
        limit: Annotated[int, "Максимум результатов"] = 20,
    ) -> dict:
        try:
            entries = self._recall_load()
            if not entries:
                return {"results": [], "note": "История диалогов пуста."}

            results = []
            for e in entries:
                if roles and e.get("role") not in roles:
                    continue
                ts = e.get("ts", "")
                if start_date and ts < start_date:
                    continue
                if end_date:
                    end_inclusive = end_date if "T" in end_date else end_date + "T23:59:59"
                    if ts > end_inclusive:
                        continue
                if query and query.lower() not in e.get("text", "").lower():
                    continue
                results.append({"role": e["role"], "text": e["text"], "ts": ts})

            results = results[-limit:]
            return {"found": len(results), "results": results}
        except Exception as e:
            logging.error("[LettaProvider] conversation_search: %s", e)
            return {"error": str(e)}

    # ── sleeptime consolidation ───────────────────────────────────────────────

    async def _consolidate(self, pending: list):
        """
        Sleeptime agent: отдельный LLM-вызов, который смотрит на накопленный
        разговор и сам решает что и как обновить в блоках памяти.
        Тот же паттерн что SleeptimeMultiAgentV4 в Letta.
        """
        if not self.agent:
            return

        transcript = self._build_transcript(pending)
        if not transcript.strip():
            return

        logging.info("[LettaProvider] consolidate: %d turns", len(pending))

        # Промпт аналогичен SleeptimeMultiAgentV4._participant_agent_step()
        user_message = (
            "<system-reminder>\n"
            "Ты — фоновый агент-консолидатор памяти. "
            "Тебе показан последний фрагмент разговора.\n"
            "Твоя задача: сохранить всё важное в память.\n"
            "Три вида памяти:\n"
            "1. Блоки (всегда в контексте) — ключевые факты, портрет пользователя, "
            "настройки. Инструменты: memory_replace, memory_rethink, "
            "memory_insert, memory_create, memory_delete.\n"
            "2. Архив (семантический поиск) — детали, события, длинные факты. "
            "Инструмент: archival_memory_insert.\n"
            "3. История диалогов — только для поиска: conversation_search.\n"
            "Если ничего важного нет — не делай ничего.\n"
            "</system-reminder>\n\n"
            f"Фрагмент разговора:\n{transcript}"
        )

        # Системный промпт = текущее состояние Memory (через Memory.compile())
        system_prompt = self._memory.compile()
        tools = [types.Tool(function_declarations=self._tool_declarations())]
        contents = [{"role": "user", "parts": [{"text": user_message}]}]

        for _ in range(10):
            resp = await asyncio.to_thread(
                self.agent.client.models.generate_content,
                model=self.agent.model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    tools=tools,
                ),
            )

            calls = resp.function_calls or []
            if not calls:
                break

            contents.append(resp.candidates[0].content)
            fn_parts = []
            for call in calls:
                result = self._dispatch(call.name, dict(call.args or {}))
                logging.info("[LettaProvider] sleeptime %s → %s", call.name, result)
                fn_parts.append(types.Part.from_function_response(name=call.name, response=result))
            contents.append({"role": "user", "parts": fn_parts})

    def _build_transcript(self, pending: list) -> str:
        lines = []
        for turn in pending:
            role = turn.get("role", "")
            parts = turn.get("parts", [])
            if isinstance(parts, list):
                text = " ".join(p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p)
            else:
                text = str(parts)
            if text.strip():
                lines.append(f"{role}: {text.strip()}")
        return "\n".join(lines)

    def _tool_declarations(self) -> list:
        """FunctionDeclarations для mini-loop консолидации (без префикса класса)."""
        s = types.Schema(type=types.Type.STRING)
        i = types.Schema(type=types.Type.INTEGER)

        def fn(name, desc, props, required):
            return types.FunctionDeclaration(
                name=name, description=desc,
                parameters=types.Schema(type=types.Type.OBJECT, properties=props, required=required),
            )

        arr = types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING))
        return [
            fn("memory_replace", "Точечная замена строки в блоке",
               {"label": s, "old_string": s, "new_string": s}, ["label", "old_string", "new_string"]),
            fn("memory_rethink", "Полная перезапись блока",
               {"label": s, "new_memory": s}, ["label", "new_memory"]),
            fn("memory_insert", "Добавить строку в блок",
               {"label": s, "new_string": s, "insert_line": i}, ["label", "new_string"]),
            fn("memory_create", "Создать новый блок",
               {"label": s, "description": s, "content": s}, ["label", "description"]),
            fn("memory_delete", "Удалить блок",
               {"label": s}, ["label"]),
            fn("archival_memory_insert",
               "Сохранить факт/деталь в архивную память (семантический поиск)",
               {"content": s, "tags": arr}, ["content"]),
            fn("conversation_search",
               "Поиск по истории диалогов",
               {"query": s, "roles": arr, "start_date": s, "end_date": s, "limit": i}, []),
        ]

    def _dispatch(self, name: str, args: dict) -> dict:
        return {
            "memory_replace": self.memory_replace,
            "memory_rethink": self.memory_rethink,
            "memory_insert": self.memory_insert,
            "memory_create": self.memory_create,
            "memory_delete": self.memory_delete,
            "archival_memory_insert": self.archival_memory_insert,
            "conversation_search": self.conversation_search,
        }.get(name, lambda **_: {"error": f"Unknown tool: {name}"})(**args)
