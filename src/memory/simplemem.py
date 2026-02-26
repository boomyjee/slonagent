import logging, os
from typing import Annotated
from agent import tool
from src.memory.base import BaseProvider
from memory import Memory


class SimpleMemProvider(BaseProvider):
    def __init__(self, model_name: str, api_key: str, consolidate_tokens: int = 1_000):
        super().__init__(consolidate_tokens=consolidate_tokens)
        
        from simplemem import SimpleMemSystem
        self._simplemem = SimpleMemSystem(
            api_key=api_key,
            model=model_name,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            db_path=os.path.join(Memory.memory_dir, "simplemem", "lancedb"),
        )

    async def get_context_prompt(self, user_text: str = "") -> str:
        if not user_text:
            return ""
        try:
            entries = self._simplemem.vector_store.semantic_search(user_text, top_k=10)
            lines = [e.lossless_restatement for e in entries if e.lossless_restatement]
            if not lines:
                return ""
            return "## Релевантные факты из памяти\n" + "\n".join(f"- {l}" for l in lines)
        except Exception as e:
            logging.debug("[SimpleMemProvider] get_context_prompt: %s", e)
        return ""

    @tool("Семантический поиск по долгосрочной памяти прошлых диалогов.")
    def search_memory(self, query: Annotated[str, "Поисковый запрос на естественном языке"]) -> dict:
        try:
            entries = self._simplemem.vector_store.semantic_search(query, top_k=10)
            lines = [e.lossless_restatement for e in entries if e.lossless_restatement]
            if not lines:
                return {"result": "Ничего не найдено."}
            return {"results": [{"content": l} for l in lines]}
        except Exception as e:
            logging.error("[SimpleMemProvider] search_memory: %s", e)
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
            logging.info("[SimpleMemProvider] консолидация: %d сообщений.", len(messages))
        except Exception as e:
            logging.error("[SimpleMemProvider] ошибка консолидации: %s", e)
