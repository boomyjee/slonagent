import json, logging, os, sys
from agent import Skill


class BaseMemory(Skill):
    def __init__(self, hard_limit_tokens: int = 500_000, soft_limit_tokens: int = 50_000, min_user_turns: int = 10, consolidate_tokens: int = 20_000, memory_dir: str = None):
        super().__init__()
        self.hard_limit_tokens = hard_limit_tokens
        self.soft_limit_tokens = soft_limit_tokens
        self.min_user_turns = min_user_turns
        self.consolidate_tokens = consolidate_tokens
        if memory_dir is None:
            memory_dir = os.path.join(os.path.dirname(os.path.abspath(sys.modules["__main__"].__file__)), "memory")
        os.makedirs(memory_dir, exist_ok=True)
        self._state_file = os.path.join(memory_dir, "CONTEXT.json")
        self._turns, self._pending = self._load_context()

    def _load_context(self) -> tuple[list, list]:
        if not self._state_file or not os.path.exists(self._state_file):
            return [], []
        try:
            with open(self._state_file, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("turns", []), data.get("pending", [])
        except Exception as e:
            logging.warning("[BaseMemory] не удалось загрузить state: %s", e)
            return [], []

    def _save_context(self):
        if not self._state_file:
            return
        try:
            turns = [t for t in self._turns if isinstance(t, dict)]
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump({"turns": turns, "pending": self._pending}, f, ensure_ascii=False)
        except Exception as e:
            logging.warning("[BaseMemory] не удалось сохранить state: %s", e)

    @staticmethod
    def _count_tokens(turns: list) -> int:
        total = 0
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            for part in turn.get("parts", []):
                if isinstance(part, dict) and "text" in part:
                    total += len(part["text"]) // 4
        return total

    def get_contents(self) -> list:
        result, tokens, user_ids = [], 0, set()
        for turn in reversed(self._turns):
            tokens += self._count_tokens([turn])
            if isinstance(turn, dict) and (uid := turn.get("_user_message_id")) is not None:
                user_ids.add(uid)
            if tokens > self.hard_limit_tokens: break
            if tokens > self.soft_limit_tokens and len(user_ids) >= self.min_user_turns: break
            result.insert(0, turn)
        return [{k: v for k, v in t.items() if not k.startswith("_")} if isinstance(t, dict) else t for t in result]

    async def add_turn(self, turn):
        self._turns.append(turn)
        if isinstance(turn, dict):
            self._pending.append(turn)
            if turn.get("role") == "model":
                if self._count_tokens(self._pending) >= self.consolidate_tokens:
                    await self._consolidate(self._pending)
                    self._pending = []
                self._save_context()

    async def _consolidate(self, pending):
        pass
