"""SummaryProvider — текстовая долгосрочная память.

Хранит два файла:
- MEMORY.md — живой документ с актуальными фактами о пользователе, обновляется LLM при каждой консолидации.
- HISTORY.md — хронологический архив выжимок диалогов (append-only).

Контекст инжектируется автоматически через get_context_prompt.
Поиск по архиву — через инструмент read_history.
"""
import asyncio, json, logging, os, httpx

log = logging.getLogger(__name__)
from typing import Annotated
from agent import tool
from openai import AsyncOpenAI
from src.memory.providers.base import BaseProvider


class SummaryProvider(BaseProvider):
    def __init__(self, model_name: str, api_key: str, base_url: str, consolidate_tokens: int = 1_000):
        super().__init__(consolidate_tokens=consolidate_tokens)
        self.memory_file: str = ""
        self.history_file: str = ""
        self.model_name = model_name
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        http_client = httpx.AsyncClient(proxy=proxy_url, timeout=120.0)
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, http_client=http_client)

    async def start(self):
        await super().start()
        file_dir = os.path.join(self.agent.memory.memory_dir, "summary")
        os.makedirs(file_dir, exist_ok=True)
        self.memory_file = os.path.join(file_dir, "MEMORY.md")
        self.history_file = os.path.join(file_dir, "HISTORY.md")

    async def get_context_prompt(self, user_text: str = "") -> str:
        memory = ""
        if os.path.exists(self.memory_file):
            with open(self.memory_file, encoding="utf-8") as f: memory = f.read()
        return (
            "## Долгосрочная память\n"
            f"{memory if memory else '(пока пусто)'}\n\n"
            "Используй инструмент read_history, чтобы искать в архиве прошлых диалогов."
        )

    @tool("Поиск по архиву прошлых диалогов (HISTORY.md). Используй, если пользователь спрашивает о чём-то из прошлого.")
    def read_history(self, query: Annotated[str, "Слово или фраза для поиска."]):
        if not os.path.exists(self.history_file):
            return {"result": "История пока пуста."}
        with open(self.history_file, encoding="utf-8") as f:
            matches = [line.strip() for line in f if query.lower() in line.lower()]
        return {"result": "\n".join(matches[-10:]) if matches else f"Ничего не найдено по запросу: {query}"}

    async def _consolidate(self, pending):
        log.info("Запускаю консолидацию: %d сообщений...", len(pending))
        current_memory = ""
        if os.path.exists(self.memory_file):
            with open(self.memory_file, encoding="utf-8") as f: current_memory = f.read()

        system = (
            "Ты — агент консолидации памяти. Вызови инструмент save_memory.\n\n"
            f"## Текущая память\n{current_memory or '(пусто)'}\n\n"
            "Передай в save_memory:\n"
            "- history_entry: краткая выжимка диалога (2–5 предложений, начни с [YYYY-MM-DD HH:MM]).\n"
            "- memory_update: полный обновлённый текст MEMORY.md (вплети новые факты в старые)."
        )

        save_memory_tool = {
            "type": "function",
            "function": {
                "name": "save_memory",
                "description": "Сохранить результат консолидации.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "history_entry": {"type": "string", "description": "Краткая выжимка диалога."},
                        "memory_update": {"type": "string", "description": "Обновлённый текст MEMORY.md."},
                    },
                    "required": ["history_entry", "memory_update"],
                },
            },
        }

        text_only = []
        for t in pending:
            role = t.get("role")
            if role not in ("user", "assistant"):
                continue
            content = t.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
            else:
                text = ""
            if not text.strip():
                continue
            text_only.append({"role": role, "content": text})

        messages = [
            {"role": "system", "content": system},
            *self.agent.strip_contents_private(text_only, self.model_name),
            {"role": "user", "content": "Вызови save_memory."},
        ]
        max_retries, delay = 5, 1.0
        for attempt in range(max_retries):
            try:
                response = await self._client.chat.completions.create(
                    model=self.model_name,
                    messages=messages,
                    tools=[save_memory_tool],
                    tool_choice={"type": "function", "function": {"name": "save_memory"}},
                )
                msg = response.choices[0].message
                if not msg.tool_calls:
                    log.warning("Консолидация: LLM не вызвала save_memory.")
                    return

                args = json.loads(msg.tool_calls[0].function.arguments)
                if entry := args.get("history_entry"):
                    with open(self.history_file, "a", encoding="utf-8") as f:
                        f.write(entry.strip() + "\n\n")
                if update := args.get("memory_update"):
                    with open(self.memory_file, "w", encoding="utf-8") as f:
                        f.write(update)

                log.info("Консолидация завершена.")
                return
            except Exception as e:
                messages = self.agent.apply_error_restriction(self.model_name, e, messages)
                if attempt + 1 == max_retries:
                    log.error("Ошибка консолидации: %s", e, exc_info=True)
                    return
                wait = delay * 2 ** attempt
                log.warning("Консолидация retry %d/%d in %ds: %s", attempt + 1, max_retries, wait, e)
                await asyncio.sleep(wait)

