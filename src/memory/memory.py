import json, logging, os, tempfile
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def _migrate_gemini_turns(turns: list) -> list:
    """Конвертирует старые Gemini-format turns в OpenAI-format.
    Вызывается при загрузке CONTEXT.json если там обнаружены старые туры.
    """
    result = []
    pending_call_ids: list[str] = []

    for turn in turns:
        if not isinstance(turn, dict) or "parts" not in turn:
            result.append(turn)
            continue

        private = {k: v for k, v in turn.items() if k.startswith("_")}
        role = turn.get("role")
        parts = turn.get("parts", [])

        text_parts = [p for p in parts if isinstance(p, dict) and "text" in p
                      and "function_call" not in p and "function_response" not in p]
        fc_parts   = [p for p in parts if isinstance(p, dict) and "function_call" in p]
        fr_parts   = [p for p in parts if isinstance(p, dict) and "function_response" in p]

        if role == "model":
            if fc_parts:
                thought_sig = turn.get("thought_signature")
                tool_calls, call_ids = [], []
                for i, p in enumerate(fc_parts):
                    fc = p["function_call"]
                    cid = f"call_{i}_{fc['name']}"
                    call_ids.append(cid)
                    tc_entry = {
                        "id": cid, "type": "function",
                        "function": {"name": fc["name"], "arguments": json.dumps(fc.get("args", {}), ensure_ascii=False)},
                    }
                    if thought_sig:
                        tc_entry["extra_content"] = {"google": {"thought_signature": thought_sig}}
                    tool_calls.append(tc_entry)
                pending_call_ids = call_ids
                msg = {"role": "assistant", "content": None, "tool_calls": tool_calls, **private}
                if text_parts:
                    msg["content"] = " ".join(p["text"] for p in text_parts)
                result.append(msg)
            else:
                content = " ".join(p.get("text", "") for p in text_parts)
                result.append({"role": "assistant", "content": content, **private})
                pending_call_ids = []

        elif role == "user":
            if fr_parts:
                if not pending_call_ids:
                    # Осиротевший function_response — соответствующий function_call
                    # был срезан компрессором. Пропускаем: tool-тур без парного
                    # assistant(tool_calls) сломает OpenAI-совместимый эндпоинт.
                    log.warning("_migrate_gemini_turns: пропускаем orphan function_response (%s)",
                                ", ".join(p["function_response"].get("name", "?") for p in fr_parts))
                else:
                    for i, p in enumerate(fr_parts):
                        fr = p["function_response"]
                        cid = pending_call_ids[i] if i < len(pending_call_ids) else f"call_{i}"
                        result.append({"role": "tool", "tool_call_id": cid, "name": fr.get("name", ""),
                                       "content": json.dumps(fr.get("response", {}), ensure_ascii=False), **private})
                    pending_call_ids = []
                if text_parts:
                    result.append({"role": "user", "content": [{"type": "text", "text": p["text"]} for p in text_parts], **private})
            else:
                content_blocks = []
                for p in parts:
                    if not isinstance(p, dict):
                        continue
                    if "text" in p:
                        content_blocks.append({"type": "text", "text": p["text"]})
                    elif "inline_data" in p:
                        d = p["inline_data"]
                        content_blocks.append({"type": "image_url",
                                               "image_url": {"url": f"data:{d['mime_type']};base64,{d['data']}"}})
                if content_blocks:
                    result.append({"role": "user", "content": content_blocks, **private})

    return result


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
            turns = json.loads(content)
        else:
            turns = [json.loads(line) for line in content.splitlines() if line.strip()]
        # fallback-конвертер: если туры в старом Gemini-формате (есть поле "parts")
        if any(isinstance(t, dict) and "parts" in t for t in turns):
            log.info("load_turns_json %s: обнаружен Gemini-формат, конвертирую в OpenAI", path)
            turns = _migrate_gemini_turns(turns)
            save_turns_json(path, turns)
        return turns
    except FileNotFoundError:
        return []
    except Exception as e:
        log.warning("load_turns_json %s: %s", path, e, exc_info=True)
        return []


class Memory:
    def __init__(self, compressor, providers: list = None, memory_dir: str = None):
        self.providers = providers or []
        self.compressor = compressor
        if memory_dir is None:
            memory_dir = os.path.join(os.getcwd(), "memory")
        self.memory_dir = memory_dir
        os.makedirs(self.memory_dir, exist_ok=True)
        self._state_file = os.path.join(self.memory_dir, "CONTEXT.json")
        self._turns = load_turns_json(self._state_file)

    def clear(self):
        self._turns = []
        save_turns_json(self._state_file, self._turns)

    def copy_from(self, src: "Memory"):
        import shutil
        if os.path.exists(src._state_file):
            shutil.copy2(src._state_file, self._state_file)
            self._turns = load_turns_json(self._state_file)
        for provider in self.providers:
            provider.copy_from(src.memory_dir)

    @staticmethod
    def count_tokens(turns: list) -> int:
        total = 0
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            content = turn.get("content")
            if isinstance(content, str):
                total += len(content) // 4
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        total += len(block["text"]) // 4
                    else:
                        total += 258  # image_url (~85-765 токенов в зависимости от размера)
            for tc in turn.get("tool_calls", []):
                fn = tc.get("function", {})
                total += (len(fn.get("name", "")) + len(fn.get("arguments", ""))) // 4
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

    async def add_turn(self, turn: dict):
        if "_timestamp" not in turn:
            turn = {**turn, "_timestamp": datetime.now(timezone.utc).isoformat()}
        self._turns.append(turn)
        if turn.get("role") == "assistant" and not turn.get("tool_calls"):
            save_turns_json(self._state_file, self._turns)
        for provider in self.providers:
            await provider.add_turn(turn)
