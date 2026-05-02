import asyncio, os, shutil
from agent import Skill
from src.memory.memory import load_turns_json, save_turns_json, Memory

class BaseProvider(Skill):
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._pending_by_dir = {}
        cls._locks = {}

    def __init__(self, consolidate_tokens: int = 1_000):
        super().__init__()
        self.folder_name = type(self).__name__.lower().removesuffix("provider").removesuffix("compressor")
        self.consolidate_tokens = consolidate_tokens
        self._pending = None
        self._pending_file: str | None = None
        self.cls = type(self)

    @property
    def provider_dir(self) -> str:
        return os.path.join(self.agent.memory.memory_dir, self.folder_name)

    def copy_from(self, src_memory_dir: str):
        src = os.path.join(src_memory_dir, self.folder_name)
        if os.path.exists(src):
            shutil.copytree(src, self.provider_dir, dirs_exist_ok=True)
        pending_name = f"PENDING_{type(self).__name__.lower()}.json"
        src_pending = os.path.join(src_memory_dir, pending_name)
        if os.path.exists(src_pending):
            shutil.copy2(src_pending, os.path.join(self.agent.memory.memory_dir, pending_name))

    async def start(self):
        if self.consolidate_tokens > 0:
            self._pending_file = os.path.join(
                self.agent.memory.memory_dir,
                f"PENDING_{type(self).__name__.lower()}.json",
            )
            md = self.agent.memory.memory_dir
            if md not in self.cls._pending_by_dir:
                self.cls._pending_by_dir[md] = load_turns_json(self._pending_file)
            self._pending = self.cls._pending_by_dir[md]

    async def add_turn(self, turn):
        if self.consolidate_tokens == 0 or not isinstance(turn, dict): return
        self._pending.append(turn)
        if turn.get("role") == "assistant" and not turn.get("tool_calls"):
            if Memory.count_tokens(self._pending) >= self.consolidate_tokens:
                snapshot = list(self._pending)
                self._pending.clear()
                md = self.agent.memory.memory_dir
                async with self.cls._locks.setdefault(md, asyncio.Lock()):
                    await self._consolidate(snapshot)
            save_turns_json(self._pending_file, self._pending)

    async def _consolidate(self, pending):
        pass
