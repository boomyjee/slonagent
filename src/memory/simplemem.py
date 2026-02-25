import logging, os, sys
from typing import Annotated
from agent import tool
from src.memory.base import BaseMemory

_LIB = os.path.join(os.path.dirname(__file__), "..", "..", "lib", "SimpleMem")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)


class SimpleMemMemory(BaseMemory):
    def __init__(self, model_name: str, api_key: str, memory_dir: str = None, hard_limit_tokens: int = 500_000, soft_limit_tokens: int = 50_000, min_user_turns: int = 10, consolidate_tokens: int = 20_000):
        memory_dir = memory_dir or os.path.join(os.path.dirname(os.path.abspath(sys.modules["__main__"].__file__)), "memory", "simplemem")
        super().__init__(hard_limit_tokens=hard_limit_tokens, soft_limit_tokens=soft_limit_tokens, min_user_turns=min_user_turns, consolidate_tokens=consolidate_tokens, memory_dir=memory_dir)

        from main import SimpleMemSystem
        self._simplemem = SimpleMemSystem(
            api_key=api_key,
            model=model_name,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            db_path=os.path.join(memory_dir, "lancedb"),
            enable_planning=True,
        )

    def _search(self, query: str) -> list[str]:
        entries = self._simplemem.hybrid_retriever.retrieve(query)
        return [e.lossless_restatement for e in entries if e.lossless_restatement]

    def get_context_prompt(self, user_text: str = "") -> str:
        if not user_text:
            return ""
        try:
            lines = self._search(user_text)
            if not lines:
                return ""
            return "## Релевантные факты из памяти\n" + "\n".join(f"- {l}" for l in lines)
        except Exception as e:
            logging.debug("[SimpleMemMemory] get_context_prompt: %s", e)
        return ""

    @tool("Семантический поиск по долгосрочной памяти прошлых диалогов.")
    def search_memory(self, query: Annotated[str, "Поисковый запрос на естественном языке"]) -> dict:
        try:
            lines = self._search(query)
            if not lines:
                return {"result": "Ничего не найдено."}
            return {"results": [{"content": l} for l in lines]}
        except Exception as e:
            logging.error("[SimpleMemMemory] search_memory: %s", e)
            return {"error": str(e)}

    async def _consolidate(self, pending):
        from models.memory_entry import Dialogue
        messages = [
            (t["role"], " ".join(p.get("text", "") for p in t.get("parts", []) if isinstance(p, dict) and "text" in p).strip())
            for t in pending
            if isinstance(t, dict) and t.get("role") in ("user", "model")
        ]
        messages = [(role, text) for role, text in messages if text]
        if not messages:
            return
        try:
            dialogues = [
                Dialogue(dialogue_id=i, speaker=role, content=text)
                for i, (role, text) in enumerate(messages)
            ]
            self._simplemem.add_dialogues(dialogues)
            self._simplemem.finalize()
            logging.info("[SimpleMemMemory] консолидация: %d сообщений.", len(messages))
        except Exception as e:
            logging.error("[SimpleMemMemory] ошибка консолидации: %s", e)
