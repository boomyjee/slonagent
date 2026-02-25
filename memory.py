import json, logging, os, sys

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        logging.warning("load_json %s: %s", path, e)
        return default


def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        logging.warning("save_json %s: %s", path, e)


class Memory:
    memory_dir = os.path.join(os.path.dirname(os.path.abspath(sys.modules["__main__"].__file__)), "memory")

    def __init__(self, providers: list = [], hard_limit_tokens: int = 500_000, soft_limit_tokens: int = 50_000, min_user_turns: int = 10):
        self.hard_limit_tokens = hard_limit_tokens
        self.soft_limit_tokens = soft_limit_tokens
        self.min_user_turns = min_user_turns
        self.providers = providers
        os.makedirs(Memory.memory_dir, exist_ok=True)
        self._state_file = os.path.join(Memory.memory_dir, "CONTEXT.json")
        self._turns = load_json(self._state_file, [])

    @staticmethod
    def count_tokens(turns: list) -> int:
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
            tokens += Memory.count_tokens([turn])
            if isinstance(turn, dict) and (uid := turn.get("_user_message_id")) is not None:
                user_ids.add(uid)
            if tokens > self.hard_limit_tokens: break
            if tokens > self.soft_limit_tokens and len(user_ids) >= self.min_user_turns: break
            result.insert(0, turn)
        return [{k: v for k, v in t.items() if not k.startswith("_")} if isinstance(t, dict) else t for t in result]

    async def add_turn(self, turn):
        self._turns.append(turn)
        if isinstance(turn, dict) and turn.get("role") == "model":
            save_json(self._state_file, [t for t in self._turns if isinstance(t, dict)])
        for provider in self.providers:
            await provider.add_turn(turn)



