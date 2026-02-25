import os
from agent import Skill
from memory import load_json, save_json, Memory

class BaseProvider(Skill):
    def __init__(self, consolidate_tokens: int = 20_000):
        super().__init__()
        self.consolidate_tokens = consolidate_tokens
        self._pending_file = os.path.join(Memory.memory_dir, f"PENDING_{type(self).__name__.lower()}.json")
        self._pending = load_json(self._pending_file,[])

    async def add_turn(self, turn):
        if isinstance(turn, dict):
            self._pending.append(turn)
            if turn.get("role") == "model":
                if Memory.count_tokens(self._pending) >= self.consolidate_tokens:
                    await self._consolidate(self._pending)
                    self._pending = []
                save_json(self._pending_file, self._pending)

    async def _consolidate(self, pending):
        pass
