import asyncio, os, sys, httpx, logging
from typing import Annotated
from agent import Skill, tool
from google import genai
from google.genai import types


class MemorySkill(Skill):
    def __init__(self, consolidation_model_name: str, api_key: str, memory_dir: str = None, max_turns: int = 100, consolidate_every: int = 10):
        if memory_dir is None:
            root = os.path.dirname(os.path.abspath(sys.modules["__main__"].__file__))
            memory_dir = os.path.join(root, "memory")
        os.makedirs(memory_dir, exist_ok=True)
        self.memory_file = os.path.join(memory_dir, "MEMORY.md")
        self.history_file = os.path.join(memory_dir, "HISTORY.md")
        self.consolidation_model_name = consolidation_model_name

        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        http_client = httpx.Client(proxy=proxy_url) if proxy_url else None
        http_options = {"httpx_client": http_client, "api_version": "v1alpha"} if http_client else {"api_version": "v1alpha"}
        self._client = genai.Client(api_key=api_key, http_options=http_options)
        self._turns: list = []
        self._pending: list = []
        self.max_turns = max_turns
        self.consolidate_every = consolidate_every
        super().__init__()

    def get_context_prompt(self) -> str:
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

    def get_contents(self) -> list:
        self._turns = self._turns[-self.max_turns:]
        return self._turns

    async def add_turn(self, turn):
        self._turns.append(turn)

        if isinstance(turn, dict):
            self._pending.append(turn)
            if turn.get("role") == "model" and len(self._pending) >= self.consolidate_every:
                await self._consolidate(self._pending)
                self._pending = []

    async def _consolidate(self, pending):
        logging.info("Запускаю консолидацию: %d сообщений...", len(pending))
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
                model=self.consolidation_model_name, contents=contents, config=config,
            )
            if not response.function_calls:
                logging.warning("Консолидация: LLM не вызвала save_memory.")
                return

            args = response.function_calls[0].args
            if entry := args.get("history_entry"):
                with open(self.history_file, "a", encoding="utf-8") as f:
                    f.write(entry.strip() + "\n\n")
            if update := args.get("memory_update"):
                with open(self.memory_file, "w", encoding="utf-8") as f:
                    f.write(update)

            logging.info("Консолидация завершена.")
        except Exception as e:
            logging.error("Ошибка консолидации: %s", e)
