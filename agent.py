import asyncio, base64, io, json, os, sys, inspect, logging
import httpx
import numpy as np
import soundfile as sf
from datetime import datetime
from typing import Annotated, get_type_hints, get_args, get_origin
from openai import AsyncOpenAI
from src.memory.memory import Memory




def tool(description: str):
    def decorator(fn):
        fn._is_tool = True
        fn._tool_description = description
        return fn
    return decorator


def bypass(command: str, description: str = "", standalone: bool = False):
    def decorator(fn):
        fn._is_bypass = True
        fn._bypass_command = command
        fn._bypass_description = description
        fn._bypass_standalone = standalone
        return fn
    return decorator


class Skill:
    def __init__(self):
        self.agent = None
        self._tool_map = {}  # tool_name → method_name
        self._bypass_handlers = {}
        self._bypass_descriptions: dict[str, str] = {}  # command → description (internal)
        self._bypass_standalone: set[str] = set()  # команды, работающие без параметров
        for name, fn in inspect.getmembers(type(self), predicate=inspect.isfunction):
            if getattr(fn, "_is_bypass", False):
                self._bypass_handlers[fn._bypass_command] = fn
                if fn._bypass_description:
                    self._bypass_descriptions[fn._bypass_command] = fn._bypass_description
                if fn._bypass_standalone:
                    self._bypass_standalone.add(fn._bypass_command)

        def param_schema(hint, desc) -> dict:
            _JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}
            if get_origin(hint) is list:
                schema = {"type": "array", "items": {"type": _JSON_TYPES.get(get_args(hint)[0], "string")}}
            else:
                schema = {"type": _JSON_TYPES.get(hint, "string")}
            if desc:
                schema["description"] = desc
            return schema

        class_name = type(self).__name__.removesuffix("Skill").removesuffix("Memory").removesuffix("Provider")
        self._tools = []
        for name, fn in inspect.getmembers(type(self), predicate=inspect.isfunction):
            if not getattr(fn, "_is_tool", False): continue

            hints = get_type_hints(fn, include_extras=True)
            sig = inspect.signature(fn)
            params = {k: v for k, v in sig.parameters.items() if k != "self"}

            properties = {
                k: param_schema(*get_args(hints[k])) if get_origin(hints.get(k)) is Annotated else param_schema(hints.get(k), "")
                for k in params
            }
            required = [k for k, p in params.items() if p.default is inspect.Parameter.empty]
            tool_name = f"{class_name}_{name}".lower()
            self._tools.append({
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": fn._tool_description,
                    "parameters": {"type": "object", "properties": properties, "required": required},
                },
            })
            self._tool_map[tool_name] = name

    def get_tools(self) -> list:
        """Возвращает список OpenAI-format tool dict для этого скилла. Переопределяй для динамических тулов."""
        return self._tools

    def get_bypass_commands(self, standalone_only: bool = False) -> dict[str, str]:
        """Возвращает {команда: описание} для bypass-обработчиков с описанием.

        standalone_only=True — только команды, работающие без параметров.
        """
        return {
            cmd: desc
            for cmd, desc in self._bypass_descriptions.items()
            if not standalone_only or cmd in self._bypass_standalone
        }

    def is_bypass_command(self, text: str) -> bool:
        cmd = text.strip().split()[0] if text.strip() else ""
        return cmd.startswith("/") and cmd[1:] in self._bypass_handlers

    async def dispatch_bypass(self, text: str) -> str:
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0][1:]
        args = parts[1] if len(parts) > 1 else ""
        fn = self._bypass_handlers[cmd]
        result = fn(self, args) if not inspect.iscoroutinefunction(fn) else await fn(self, args)
        return str(result)

    async def get_context_prompt(self, user_text: str = "") -> str:
        return ""

    async def get_tool_prompt(self, tool_name: str) -> str:
        return ""

    def register(self, agent):
        self.agent = agent

    async def start(self):
        pass

    async def dispatch_tool_call(self, tool_call: dict) -> dict:
        name = tool_call["function"]["name"]
        if name not in self._tool_map:
            return {"error": f"Unknown tool: {name}"}

        method_name = self._tool_map[name]
        method = getattr(self, method_name)
        try:
            args = json.loads(tool_call["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}

        try:
            if inspect.iscoroutinefunction(method):
                return await method(**args)
            return method(**args)
        except Exception as e:
            logging.exception("[skill] %s failed", name)
            return {"error": str(e)}


class AgentSkill(Skill):
    """Встроенный скилл агента: базовые управляющие команды."""

    @bypass("restart", "Перезапустить бота", standalone=True)
    def restart_command(self, args: str) -> str:
        asyncio.get_event_loop().call_later(1, os.execv, sys.executable, [sys.executable] + sys.argv)
        return "Перезапускаюсь..."
    
    @bypass("stop", "Остановить текущий ответ", standalone=True)
    def stop_command(self, args: str) -> str:
        if self.agent:
            self.agent.stop()
        return ""


class Agent:
    def __init__(self, model_name: str, api_key: str, base_url: str, agent_dir: str, memory_compressor = None, memory_providers: list | dict = None, skills: list = None, max_iterations: int = 20, transcription_model_name: str = "gemini-2.5-flash", transcription_api_key: str = None, transcription_base_url: str = None, transport=None):
        self.model_name = model_name
        self.api_key = api_key
        self.transcription_model_name = transcription_model_name
        self.agent_dir = agent_dir
        if isinstance(memory_providers, dict):
            memory_providers = list(memory_providers.values())
        memory_dir = os.path.join(agent_dir, "memory")
        self.memory = Memory(compressor=memory_compressor, providers=memory_providers or [], memory_dir=memory_dir)
        self.skills = ([memory_compressor] if memory_compressor else []) + self.memory.providers + (skills or []) + [AgentSkill()]
        self.max_iterations = max_iterations
        for skill in self.skills:
            skill.register(self)
        self.transport = transport
        if transport:
            transport.set_agent(self)

        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        http_client = httpx.AsyncClient(proxy=proxy_url, timeout=120.0) if proxy_url else None
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url, http_client=http_client)
        self.transcription_client = AsyncOpenAI(
            api_key=transcription_api_key or api_key,
            base_url=transcription_base_url or base_url,
            http_client=http_client,
        )
        self._process_message_lock = asyncio.Lock()
        self._pending_messages: list = []
        self._stop_event = asyncio.Event()
        self._restrictions_file = os.path.join(memory_dir, ".restrictions.json")
        self._restrictions: dict = self._load_restrictions()

    def _load_restrictions(self) -> dict:
        try:
            with open(self._restrictions_file, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _save_restriction(self, key: str, value):
        self._restrictions[key] = value
        try:
            with open(self._restrictions_file, "w", encoding="utf-8") as f:
                json.dump(self._restrictions, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.warning("[agent] не удалось сохранить restrictions: %s", e)

    def apply_error_restriction(self, model_name: str, e: Exception, messages: list) -> list:
        """Выставляет ограничение на основе ошибки и возвращает обновлённые messages."""
        err = str(e)
        if "image input" in err and "404" in err:
            logging.warning("[agent] модель %s не поддерживает картинки, сохраняю ограничение", model_name)
            self._save_restriction(f"{model_name}.no_images", True)
            return self.strip_contents_private(messages, model_name)
        return messages

    def stop(self):
        """Прервать текущий ответ. Частичный ответ не сохраняется в историю."""
        self._stop_event.set()

    def strip_contents_private(self, turns: list, model_name: str = None) -> list:
        model = model_name or self.model_name
        no_images = self._restrictions.get(f"{model}.no_images", False)
        result = []
        for t in turns:
            if not isinstance(t, dict):
                result.append(t)
                continue
            ts = ""
            ts_raw = t.get("_timestamp") or ""
            if ts_raw:
                try:
                    ts = datetime.fromisoformat(ts_raw).astimezone().strftime("%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    logging.warning("[agent] invalid _timestamp: %r", ts_raw)
            turn = {k: v for k, v in t.items() if not k.startswith("_")}
            if "parts" in turn:
                turn["parts"] = [
                    {k: v for k, v in p.items() if not k.startswith("_")} if isinstance(p, dict) else p
                    for p in turn["parts"]
                ]
                if ts and turn.get("role") == "user" and any("text" in p for p in turn["parts"] if isinstance(p, dict)):
                    turn["parts"] = [{"text": f"[{ts}]"}] + turn["parts"]
            elif isinstance(turn.get("content"), list):
                blocks = [{k: v for k, v in b.items() if not k.startswith("_")} if isinstance(b, dict) else b
                          for b in turn["content"]]
                if no_images:
                    blocks = [b for b in blocks if not (isinstance(b, dict) and b.get("type") == "image_url")]
                if ts and turn.get("role") == "user":
                    blocks = [{"type": "text", "text": f"[{ts}]"}] + blocks
                turn["content"] = blocks or None
            result.append(turn)
        return result


    async def _llm_stream(self, contents, tools, system_instruction, label: str = ""):
        messages = ([{"role": "system", "content": system_instruction}] if system_instruction else []) + contents
        max_retries, delay = 5, 0.5
        for attempt in range(max_retries):
            try:
                kwargs: dict = {"model": self.model_name, "messages": messages, "stream": True, "temperature": 1.0}
                if tools:
                    kwargs["tools"] = tools
                kwargs["extra_body"] = {"extra_body": {"google": {"thinking_config": {"include_thoughts": True}}}}

                stream = await self.client.chat.completions.create(**kwargs)
                text = ""
                thinking_text = ""
                thinking_id = None
                stream_id = None
                accumulated_calls: dict[int, dict] = {}

                async for chunk in stream:
                    if self._stop_event.is_set():
                        break
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta

                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            idx = tc.index if tc.index is not None else len(accumulated_calls)
                            entry = accumulated_calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                            if tc.id:
                                entry["id"] = tc.id
                            if tc.function and tc.function.name:
                                entry["name"] += tc.function.name
                            if tc.function and tc.function.arguments:
                                entry["arguments"] += tc.function.arguments
                            # Gemini thinking models attach a thought_signature to tool calls.
                            # Path: tc.model_extra['extra_content']['google']['thought_signature']
                            # It must be preserved and sent back in subsequent requests.
                            tc_extra = getattr(tc, "model_extra", None) or {}
                            sig = (tc_extra
                                   .get("extra_content", {})
                                   .get("google", {})
                                   .get("thought_signature"))
                            if sig:
                                entry["thought_signature"] = sig

                    delta_extra = getattr(delta, "model_extra", None) or {}
                    # Gemini: мысли приходят в delta.content с флагом extra_content.google.thought=True
                    # OpenAI o1: мысли в delta.model_extra.reasoning_content
                    # Gemini: thinking chunks помечены флагом extra_content.google.thought=True
                    # и приходят в delta.content
                    is_thought = delta_extra.get("extra_content", {}).get("google", {}).get("thought")
                    if delta.content:
                        if is_thought:
                            thinking_text += delta.content.removeprefix("<thought>")
                            thinking_id = await self.transport.send_thinking(thinking_text, thinking_id)
                        else:
                            content = delta.content.removeprefix("</thought>")
                            if content:
                                text += content
                                stream_id = await self.transport.send_message(text, stream_id)
                    # OpenAI o1: reasoning_content, OpenRouter: reasoning
                    thought_extra = delta_extra.get("reasoning_content") or delta_extra.get("reasoning")
                    if thought_extra and isinstance(thought_extra, str):
                        thinking_text += thought_extra
                        thinking_id = await self.transport.send_thinking(thinking_text, thinking_id)

                    known = {"content", "tool_calls", "role", "refusal"}
                    unexpected = (delta.model_fields_set or set()) - known
                    if unexpected:
                        logging.warning("[stream] unknown delta fields: %s", unexpected)

                tool_calls = []
                for idx in sorted(accumulated_calls):
                    call = accumulated_calls[idx]
                    logging.info("[stream] function_call: %s", call["name"])
                    tc_dict = {
                        "id": call["id"], "type": "function",
                        "function": {"name": call["name"], "arguments": call["arguments"]},
                    }
                    # Gemini thinking models return thought_signature in extra_content.google.
                    # Must be echoed back at the same level in subsequent requests.
                    if call.get("thought_signature"):
                        tc_dict["extra_content"] = {"google": {"thought_signature": call["thought_signature"]}}
                    tool_calls.append(tc_dict)

                return tool_calls, text
            except Exception as e:
                messages = self.apply_error_restriction(self.model_name, e, messages)
                if attempt + 1 == max_retries:
                    raise
                wait = delay * 2 ** attempt
                logging.warning("[agent] LLM %s error, retry %d/%d in %ds: %s", label, attempt + 1, max_retries, wait, e)
                await asyncio.sleep(wait)

    async def start(self):
        for skill in self.skills:
            await skill.start()

    async def transcribe_audio(self, data: bytes, mime_type: str) -> str:
        fmt = mime_type.split("/")[-1]
        if fmt not in ("wav", "mp3"):
            audio, sr = sf.read(io.BytesIO(data))
            wav_buf = io.BytesIO()
            sf.write(wav_buf, audio, sr, format='WAV', subtype='PCM_16')
            data, fmt = wav_buf.getvalue(), "wav"
        max_retries, delay = 5, 0.5
        for attempt in range(max_retries):
            try:
                resp = await self.transcription_client.chat.completions.create(
                    model=self.transcription_model_name,
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": "Transcribe the audio. Return only the transcript text."},
                        {"type": "input_audio", "input_audio": {"data": base64.b64encode(data).decode(), "format": fmt}},
                    ]}],
                )
                return resp.choices[0].message.content
            except Exception as e:
                if attempt + 1 == max_retries: raise
                wait = delay * 2 ** attempt
                logging.warning("[agent] transcribe_audio retry %d/%d in %ds: %s", attempt + 1, max_retries, wait, e)
                await asyncio.sleep(wait)

    async def describe_video(self, data: bytes, mime_type: str) -> str:
        if len(data) > 10 * 1024 * 1024:
            logging.warning("[agent] video >10MB отправляется inline, возможны ошибки")
        max_retries, delay = 5, 0.5
        for attempt in range(max_retries):
            try:
                resp = await self.transcription_client.chat.completions.create(
                    model=self.transcription_model_name,
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": "Describe the key events in this video, providing both audio and visual details. Include timestamps for salient moments."},
                        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64.b64encode(data).decode()}"}},
                    ]}],
                )
                return resp.choices[0].message.content
            except Exception as e:
                if attempt + 1 == max_retries: raise
                wait = delay * 2 ** attempt
                logging.warning("[agent] describe_video retry %d/%d in %ds: %s", attempt + 1, max_retries, wait, e)
                await asyncio.sleep(wait)

    async def process_message(self, content_parts: list, user_message_id=None):
        user_query = " ".join(p.get("text", "") for p in content_parts if isinstance(p, dict) and "text" in p).strip()
        for skill in self.skills:
            if skill.is_bypass_command(user_query):
                result = await skill.dispatch_bypass(user_query)
                if result:
                    await self.transport.send_message(result)
                return

        if self._process_message_lock.locked():
            logging.info("[agent] message queued (will be batched)")
            self._pending_messages.append((content_parts, user_message_id))
            return

        async with self._process_message_lock:
            await self._run_message(content_parts, user_message_id)
            while self._pending_messages:
                batch = self._pending_messages[:]
                self._pending_messages.clear()
                logging.info("[agent] processing batch of %d queued messages", len(batch))
                merged_parts = [p for i, (parts, _) in enumerate(batch) for p in ([{"type": "text", "text": "\n"}] if i > 0 else []) + list(parts)]
                merged_id = next((mid for _, mid in batch if mid is not None), None)
                await self._run_message(merged_parts, merged_id)

    async def _run_message(self, content_parts: list, user_message_id=None):
        user_query = " ".join(p.get("text", "") for p in content_parts if isinstance(p, dict) and "text" in p).strip()
        logging.info("[agent] incoming: %r", user_query)

        self._stop_event.clear()
        await self.memory.add_turn({"role": "user", "content": content_parts, "_user_message_id": user_message_id})

        tools = []
        tool_to_skill = {}

        def truncate(s: str, n: int) -> str:
            return s[:n] + "..." if len(s) > n else s

        system_parts = []
        tools_info = []
        for skill in self.skills:
            for decl in skill.get_tools():
                fn = decl["function"]
                extra = await skill.get_tool_prompt(fn["name"])
                if extra:
                    decl = {"type": "function", "function": {**fn, "description": fn["description"] + "\n\n---\n" + extra}}
                tool_to_skill[decl["function"]["name"]] = skill
                tools.append(decl)
                tools_info.append(f"{decl['function']['name']}: {truncate(decl['function']['description'].splitlines()[0], 100)}")

            skill_context = await skill.get_context_prompt(user_query)
            if skill_context:
                system_parts.append(skill_context)
                await self.transport.send_system_prompt(f"[{skill.__class__.__name__}]\n{skill_context}")

        if tools_info:
            await self.transport.send_system_prompt("[Инструменты модели]\n"+"\n".join(tools_info))


        system_instruction = "\n\n".join(system_parts)
        try:
            logging.info("[agent] → LLM %s", self.model_name)
            tool_calls, text = await self._llm_stream(self.strip_contents_private(await self.memory.get_contents()), tools, system_instruction)
            logging.info("[agent] ← LLM")

            if self._stop_event.is_set(): logging.info("[agent] stopped by user, response not saved"); return

            iteration = 0
            while tool_calls and iteration < self.max_iterations:
                extra_parts = []
                tool_turns = []
                for fc in tool_calls:
                    name = fc["function"]["name"]
                    args = json.loads(fc["function"].get("arguments") or "{}")
                    logging.info("Инструмент: %s", name)
                    skill = tool_to_skill.get(name)
                    if not skill:
                        logging.warning("Tool %s not found in skills", name)
                        tool_turns.append({"role": "tool", "tool_call_id": fc["id"], "name": name,
                                           "content": json.dumps({"error": f"Tool {name} not found"})})
                        continue

                    await self.transport.on_tool_call(name, args)
                    result = await skill.dispatch_tool_call(fc)
                    await self.transport.on_tool_result(name, result)
                    extra_parts.extend(result.pop("_parts", []) if isinstance(result, dict) else [])
                    tool_turns.append({
                        "role": "tool", "tool_call_id": fc["id"], "name": name,
                        "content": json.dumps(result if isinstance(result, dict) else {"result": result}, ensure_ascii=False),
                    })

                await self.memory.add_turn({"role": "assistant", "content": None, "tool_calls": tool_calls})
                for tool_turn in tool_turns:
                    await self.memory.add_turn(tool_turn)

                if extra_parts:
                    await self.memory.add_turn({"role": "user", "content": extra_parts})

                iteration += 1
                logging.info("[agent] → LLM iteration %d", iteration)
                tool_calls, text = await self._llm_stream(self.strip_contents_private(await self.memory.get_contents()), tools, system_instruction, str(iteration))
                logging.info("[agent] ← LLM iteration %d", iteration)

                if self._stop_event.is_set(): logging.info("[agent] stopped by user during iter %d, response not saved", iteration); return

            if tool_calls:
                logging.warning("[agent] max_iterations=%d reached", self.max_iterations)
                await self.transport.send_message(f"⚠️ Достигнут лимит итераций ({self.max_iterations}). Ответ может быть неполным.")

            await self.memory.add_turn({"role": "assistant", "content": text or ""})

        except Exception as e:
            logging.warning("Ошибка LLM: %s", e, exc_info=True)
            await self.transport.send_message(f"Ошибка LLM: {e}")
