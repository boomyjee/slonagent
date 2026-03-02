import json, logging, os, sys, tempfile

log = logging.getLogger(__name__)
from datetime import datetime, timezone

def save_turns_json(path, turns):
    dir_ = os.path.dirname(os.path.abspath(path))
    tmp = None
    try:
        data = [t for t in turns if isinstance(t, dict) and all(isinstance(p, dict) for p in t.get("parts", []))]
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=dir_, delete=False, suffix=".tmp") as f:
            for turn in data:
                f.write(json.dumps(turn, ensure_ascii=False) + "\n")
            tmp = f.name
        os.replace(tmp, path)
    except Exception as e:
        log.warning("save_turns_json %s: %s", path, e, exc_info=True)
        if tmp:
            os.unlink(tmp)


def load_turns_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return []
        # поддержка старого формата (JSON array)
        if content.startswith("["):
            return json.loads(content)
        return [json.loads(line) for line in content.splitlines() if line.strip()]
    except FileNotFoundError:
        return []
    except Exception as e:
        log.warning("load_turns_json %s: %s", path, e, exc_info=True)
        return []


class Memory:
    memory_dir = os.path.join(os.path.dirname(os.path.abspath(sys.modules["__main__"].__file__)), "memory")

    def __init__(self, compressor, providers: list = None):
        self.providers = providers or []
        self.compressor = compressor
        os.makedirs(Memory.memory_dir, exist_ok=True)
        self._state_file = os.path.join(Memory.memory_dir, "CONTEXT.json")
        self._turns = load_turns_json(self._state_file)

    @staticmethod
    def count_tokens(turns: list) -> int:
        total = 0
        for turn in turns:
            if isinstance(turn, dict):
                parts = turn.get("parts", [])
                for part in parts:
                    if isinstance(part, dict) and "text" in part:
                        total += len(part["text"]) // 4
            else:
                for part in getattr(turn, "parts", None) or []:
                    total += len(getattr(part, "text", "") or "") // 4
        return total

    async def get_contents(self) -> list:
        try:
            result = await self.compressor.compress(self._turns)
        except Exception as e:
            log.warning("[memory] compressor failed: %s", e, exc_info=True)
            result = self._turns

        if len(result) < len(self._turns):
            old_count = len(self._turns)
            self._turns = result
            save_turns_json(self._state_file, self._turns)
            log.info("[memory] compressor: %d → %d turns", old_count, len(result))

        return self._turns

    async def add_turn(self, turn):
        if isinstance(turn, dict) and "_timestamp" not in turn:
            turn["_timestamp"] = datetime.now(timezone.utc).isoformat()
        self._turns.append(turn)
        if isinstance(turn, dict) and turn.get("role") == "model":
            save_turns_json(self._state_file, self._turns)
        for provider in self.providers:
            await provider.add_turn(turn)
