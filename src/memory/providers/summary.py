"""SummaryProvider — текстовая долгосрочная память.

Хранит два файла:
- MEMORY.md — живой документ с актуальными фактами о пользователе, обновляется LLM при каждой консолидации.
- HISTORY.md — хронологический архив выжимок диалогов (append-only).

Контекст инжектируется автоматически через get_context_prompt.
Поиск по архиву — через инструмент read_history.
"""
import asyncio, logging, os, httpx

log = logging.getLogger(__name__)
from typing import Annotated
from agent import tool, Agent
from google import genai
from google.genai import types
from src.memory.providers.base import BaseProvider


class SummaryProvider(BaseProvider):
    def __init__(self, model_name: str, api_key: str, consolidate_tokens: int = 1_000):
        super().__init__(consolidate_tokens=consolidate_tokens)
        from src.memory.memory import Memory
        file_dir = os.path.join(Memory.memory_dir, "summary")
        os.makedirs(file_dir, exist_ok=True)
        self.memory_file = os.path.join(file_dir, "MEMORY.md")
        self.history_file = os.path.join(file_dir, "HISTORY.md")
        self.model_name = model_name
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        http_client = httpx.Client(proxy=proxy_url) if proxy_url else None
        http_options = {"httpx_client": http_client, "api_version": "v1alpha"} if http_client else {"api_version": "v1alpha"}
        self._client = genai.Client(api_key=api_key, http_options=http_options)

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

        save_memory_tool = types.FunctionDeclaration(
            name="save_memory",
            description="Сохранить результат консолидации.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "history_entry": types.Schema(type=types.Type.STRING, description="Краткая выжимка диалога."),
                    "memory_update": types.Schema(type=types.Type.STRING, description="Обновлённый текст MEMORY.md.")
                },
                required=["history_entry", "memory_update"]
            )
        )

        try:
            config = types.GenerateContentConfig(
                system_instruction=system,
                tools=[types.Tool(function_declarations=[save_memory_tool])],
            )
            text_only = [
                {"role": t["role"], "parts": [p for p in t["parts"] if "text" in p]}
                for t in pending
            ]
            contents = [
                *text_only,
                {"role": "user", "parts": [{"text": "Вызови save_memory."}]},
            ]
            response = await asyncio.to_thread(
                self._client.models.generate_content,
                model=self.model_name, contents=Agent.strip_contents_private(contents), config=config,
            )
            if not response.function_calls:
                log.warning("Консолидация: LLM не вызвала save_memory.")
                return

            args = response.function_calls[0].args
            if entry := args.get("history_entry"):
                with open(self.history_file, "a", encoding="utf-8") as f:
                    f.write(entry.strip() + "\n\n")
            if update := args.get("memory_update"):
                with open(self.memory_file, "w", encoding="utf-8") as f:
                    f.write(update)

            log.info("Консолидация завершена.")
        except Exception as e:
            log.error("Ошибка консолидации: %s", e, exc_info=True)

