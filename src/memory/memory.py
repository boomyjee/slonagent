import json, logging, os, sys, tempfile

log = logging.getLogger(__name__)
from datetime import datetime, timezone

from google.genai import types

def _part_to_jsonable(p) -> dict:
    """Part (dict или объект SDK) → dict для JSON. Snake_case, JSON-safe (bytes только из SDK → base64 через model_dump)."""
    if hasattr(p, "model_dump"):
        return p.model_dump(mode="json", exclude_none=True)
    if not isinstance(p, dict):
        return {}
    # Только поля Part — лишнее (transport _document_id и т.п.) отбрасываем
    allowed = {k: v for k, v in p.items() if k in types.Part.model_fields}
    if not allowed:
        text = p.get("content") or p.get("text") or ""
        return {"text": text} if text else {}
    return allowed

def _turn_to_dict(turn) -> dict:
    """Turn (dict или Content) → единый dict для хранения и провайдеров. Parts в snake_case (формат SDK), JSON-ready."""
    if isinstance(turn, dict):
        parts = [_part_to_jsonable(p) for p in turn.get("parts", [])]
        return {**turn, "parts": parts}
    role = getattr(turn, "role", None)
    role = getattr(role, "value", role) if role is not None and hasattr(role, "value") else role
    parts = [_part_to_jsonable(p) for p in (getattr(turn, "parts", None) or [])]
    return {"role": role or "model", "parts": parts}


def save_turns_json(path, turns):
    dir_ = os.path.dirname(os.path.abspath(path))
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=dir_, delete=False, suffix=".tmp") as f:
            for turn in turns:
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
            if not isinstance(turn, dict):
                continue
            for part in turn.get("parts", []):
                if not isinstance(part, dict):
                    continue
                if "text" in part:
                    total += len(part["text"]) // 4
                elif fc := part.get("function_call"):
                    total += (len(fc.get("name", "")) + len(json.dumps(fc.get("args", {}), ensure_ascii=False))) // 4
                elif fr := part.get("function_response"):
                    total += (len(fr.get("name", "")) + len(json.dumps(fr.get("response", {}), ensure_ascii=False))) // 4
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
        normalized = _turn_to_dict(turn)
        if "_timestamp" not in normalized:
            normalized["_timestamp"] = datetime.now(timezone.utc).isoformat()
        self._turns.append(normalized)
        if normalized.get("role") == "model":
            save_turns_json(self._state_file, self._turns)
        for provider in self.providers:
            await provider.add_turn(normalized)
