import json, logging, os, sys, tempfile
from datetime import datetime, timezone

def save_turns_json(path, turns):
    dir_ = os.path.dirname(os.path.abspath(path))
    tmp = None
    try:
        data = [t for t in turns if isinstance(t, dict) and all(isinstance(p, dict) for p in t.get("parts", []))]
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=dir_, delete=False, suffix=".tmp") as f:
            json.dump(data, f, ensure_ascii=False)
            tmp = f.name
        os.replace(tmp, path)
    except Exception as e:
        logging.warning("save_turns_json %s: %s", path, e)
        if tmp:
            os.unlink(tmp)


def load_turns_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        logging.warning("load_turns_json %s: %s", path, e)
        return []


class Memory:
    memory_dir = os.path.join(os.path.dirname(os.path.abspath(sys.modules["__main__"].__file__)), "memory")

    def __init__(
        self,
        providers: list = None,
        hard_limit_tokens: int = 500_000,
        soft_limit_tokens: int = 50_000,
        min_user_turns: int = 10,
        compress_fn=None,
    ):
        self.hard_limit_tokens = hard_limit_tokens
        self.soft_limit_tokens = soft_limit_tokens
        self.min_user_turns = min_user_turns
        self.providers = providers or []
        # compress_fn: async (turns: list) -> list
        # Принимает полный _turns, возвращает сжатый список.
        # Все параметры сжатия (keep_recent_count и т.д.) — внутри compress_fn.
        # Если None — используется стратегия window (старое поведение).
        self.compress_fn = compress_fn
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

    def _get_contents_window(self) -> list:
        """Стратегия 1 (по умолчанию): скользящее окно по токен-бюджету.
        Старые сообщения молча удаляются при переполнении."""
        result, tokens, user_ids = [], 0, set()
        for turn in reversed(self._turns):
            tokens += Memory.count_tokens([turn])
            if isinstance(turn, dict) and (uid := turn.get("_user_message_id")) is not None:
                user_ids.add(uid)
            if tokens > self.hard_limit_tokens: break
            if tokens > self.soft_limit_tokens and len(user_ids) >= self.min_user_turns: break
            result.append(turn)
        result.reverse()

        if len(result) < len(self._turns):
            self._turns = result
            save_turns_json(self._state_file, self._turns)

        return result

    async def _get_contents_compress(self) -> list:
        """Стратегия 2 (compress): compress_fn получает весь _turns и возвращает сжатый список.
        Все решения о keep_recent_count, порогах и режиме — внутри compress_fn."""
        try:
            result = await self.compress_fn(self._turns)
        except Exception as e:
            logging.warning("[memory] compress_fn failed, falling back to window: %s", e)
            return self._get_contents_window()

        if len(result) < len(self._turns):
            self._turns = result
            save_turns_json(self._state_file, self._turns)
            logging.info("[memory] compress: %d → %d turns", len(self._turns), len(result))

        return self._turns

    async def get_contents(self) -> list:
        if self.compress_fn is not None:
            return await self._get_contents_compress()
        return self._get_contents_window()

    async def add_turn(self, turn):
        if isinstance(turn, dict) and "_timestamp" not in turn:
            turn["_timestamp"] = datetime.now(timezone.utc).isoformat()
        self._turns.append(turn)
        if isinstance(turn, dict) and turn.get("role") == "model":
            save_turns_json(self._state_file, self._turns)
        for provider in self.providers:
            await provider.add_turn(turn)

