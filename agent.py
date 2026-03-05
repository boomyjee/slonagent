import asyncio, os, sys, inspect, logging, httpx
from typing import Annotated, get_type_hints, get_args, get_origin
from google import genai
from google.genai import types
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

        def param_schema(hint, desc):
            _GEMINI_TYPES = { str: types.Type.STRING, int: types.Type.INTEGER, float: types.Type.NUMBER, bool: types.Type.BOOLEAN }
            if get_origin(hint) is list:
                return types.Schema(type=types.Type.ARRAY, description=desc,
                                    items=types.Schema(type=_GEMINI_TYPES.get(get_args(hint)[0], types.Type.STRING)))
            return types.Schema(type=_GEMINI_TYPES.get(hint, types.Type.STRING), description=desc)

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
            self._tools.append(types.FunctionDeclaration(
                name=tool_name,
                description=fn._tool_description,
                parameters=types.Schema(type=types.Type.OBJECT, properties=properties, required=required),
            ))
            self._tool_map[tool_name] = name

    def get_tools(self) -> list:
        """Возвращает список FunctionDeclaration для этого скилла. Переопределяй для динамических тулов."""
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

    async def dispatch_tool_call(self, tool_call) -> dict:
        if tool_call.name not in self._tool_map:
            return {"error": f"Unknown tool: {tool_call.name}"}

        method_name = self._tool_map[tool_call.name]
        method = getattr(self, method_name)
        args = dict(tool_call.args or {})

        try:
            if inspect.iscoroutinefunction(method):
                return await method(**args)
            return method(**args)
        except Exception as e:
            logging.exception("[skill] %s failed", tool_call.name)
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
    def __init__(self, model_name: str, api_key: str, memory_compressor, memory_providers: list = None, skills: list = None, include_thoughts: bool = False, max_iterations: int = 20, transcription_model_name: str = "gemini-2.5-flash", transport=None):
        self.model_name = model_name
        self.include_thoughts = include_thoughts
        self.transcription_model_name = transcription_model_name
        self.memory = Memory(compressor=memory_compressor, providers=memory_providers or [])
        self.skills = self.memory.providers + (skills or []) + [AgentSkill()]
        self.max_iterations = max_iterations
        for skill in self.skills:
            skill.register(self)
        self.transport = transport
        if transport:
            transport.set_agent(self)

        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        http_client = httpx.Client(proxy=proxy_url) if proxy_url else None
        http_options = {"httpx_client": http_client, "api_version": "v1alpha"} if http_client else {"api_version": "v1alpha"}
        self.client = genai.Client(api_key=api_key, http_options=http_options)
        self._process_message_lock = asyncio.Lock()
        self._pending_messages: list = []
        self._stop_event = asyncio.Event()

    def stop(self):
        """Прервать текущий ответ. Частичный ответ не сохраняется в историю."""
        self._stop_event.set()

    @staticmethod
    def strip_contents_private(turns: list) -> list:
        result = []
        for t in turns:
            if not isinstance(t, dict):
                result.append(t)
                continue
            turn = {k: v for k, v in t.items() if not k.startswith("_")}
            if "parts" in turn:
                turn["parts"] = [
                    {k: v for k, v in p.items() if not k.startswith("_")} if isinstance(p, dict) else p
                    for p in turn["parts"]
                ]
            result.append(turn)
        return result


    async def _llm_stream(self, contents, config, label: str = ""):
        max_retries, delay = 5, 0.5
        for attempt in range(max_retries):
            try:
                stream = await self.client.aio.models.generate_content_stream(
                    model=self.model_name, contents=contents, config=config,
                )
                text, thinking_text = "", ""
                stream_id, thinking_id = None, None
                function_call_parts = []
                
                last_chunk = None
                async for chunk in stream:
                    if self._stop_event.is_set(): break
                    parts = chunk.candidates[0].content.parts or [] if chunk.candidates else []
                    for p in parts:
                        if getattr(p, "thought", False) and getattr(p, "text", None):
                            thinking_text += p.text
                            thinking_id = await self.transport.send_thinking(thinking_text, thinking_id)
                        elif getattr(p, "function_call", None):
                            function_call_parts.append(p)
                        elif getattr(p, "text", None) and not getattr(p, "function_call", None):
                            text += p.text
                            stream_id = await self.transport.send_message(text, stream_id)
                    last_chunk = chunk
                if last_chunk and (u := last_chunk.usage_metadata):
                    logging.info("[agent] tokens: in=%s out=%s total=%s %s",
                        u.prompt_token_count, u.candidates_token_count, u.total_token_count,
                        f"(iter {label})" if label else "")
                return function_call_parts, text
            except Exception as e:
                if attempt + 1 == max_retries or "503" not in str(e) and "UNAVAILABLE" not in str(e): raise
                wait = delay * 2 ** attempt
                logging.warning("[agent] LLM %s unavailable, retry %d/%d in %ds", label, attempt + 1, max_retries, wait)
                await asyncio.sleep(wait)

    async def start(self):
        for skill in self.skills:
            await skill.start()
        if self.transport:
            await self.transport.start()

    async def transcribe_audio(self, data: bytes, mime_type: str) -> str:
        max_retries, delay = 5, 0.5
        for attempt in range(max_retries):
            try:
                resp = await asyncio.to_thread(
                    self.client.models.generate_content,
                    model=self.transcription_model_name,
                    contents=types.Content(role="user", parts=[
                        types.Part.from_bytes(data=data, mime_type=mime_type),
                        types.Part.from_text(text="Transcribe the audio. Return only the transcript text."),
                    ]),
                )
                return resp.text
            except Exception as e:
                if attempt + 1 == max_retries: raise
                wait = delay * 2 ** attempt
                logging.warning("[agent] transcribe_audio retry %d/%d in %ds: %s", attempt + 1, max_retries, wait, e)
                await asyncio.sleep(wait)

    async def process_message(self, message_parts: list, user_message_id=None, user_query: str = ""):
        for skill in self.skills:
            if skill.is_bypass_command(user_query):
                result = await skill.dispatch_bypass(user_query)
                if result:
                    await self.transport.send_message(result)
                return

        if self._process_message_lock.locked():
            logging.info("[agent] message queued (will be batched)")
            self._pending_messages.append((message_parts, user_message_id, user_query))
            return

        async with self._process_message_lock:
            await self._run_message(message_parts, user_message_id, user_query)
            while self._pending_messages:
                batch = self._pending_messages[:]
                self._pending_messages.clear()
                logging.info("[agent] processing batch of %d queued messages", len(batch))
                merged_parts = [p for i, (parts, _, _) in enumerate(batch) for p in ([{"text": "\n"}] if i > 0 else []) + list(parts)]
                merged_query = "\n".join(q for _, _, q in batch if q)
                merged_id = next((mid for _, mid, _ in batch if mid is not None), None)
                await self._run_message(merged_parts, merged_id, merged_query)

    async def _run_message(self, message_parts: list, user_message_id=None, user_query: str = ""):
        logging.info("[agent] incoming: %r", user_query)

        self._stop_event.clear()
        await self.memory.add_turn({"role": "user", "parts": message_parts, "_user_message_id": user_message_id})


        user_text = " ".join(p.get("text", "") for p in message_parts if isinstance(p, dict) and "text" in p).strip()
        tools = []
        tool_to_skill = {}

        def truncate(s: str, n: int) -> str:
            return s[:n] + "..." if len(s) > n else s

        system_parts = []
        tools_info = []
        for skill in self.skills:
            if skill.get_tools():
                augmented = []
                for decl in skill.get_tools():
                    extra = await skill.get_tool_prompt(decl.name)
                    if extra:
                        augmented.append(types.FunctionDeclaration(
                            name=decl.name,
                            description=(decl.description or "") + "\n\n---\n" + extra,
                            parameters=decl.parameters,
                        ))
                    else:
                        augmented.append(decl)
                for f in augmented: tool_to_skill[f.name] = skill
                tools.append(types.Tool(function_declarations=augmented))
                tools_info.extend(f"{f.name}: {truncate((f.description or '').splitlines()[0], 100)}" for f in augmented)

            skill_context = await skill.get_context_prompt(user_query)
            if skill_context:
                system_parts.append(skill_context)
                await self.transport.send_system_prompt(f"[{skill.__class__.__name__}]\n{skill_context}")

        if tools_info:
            await self.transport.send_system_prompt("[Инструменты модели]\n"+"\n".join(tools_info))


        try:
            config = types.GenerateContentConfig(
                system_instruction="\n\n".join(system_parts),
                temperature=1.0,
                tools=tools,
                thinking_config=types.ThinkingConfig(include_thoughts=self.include_thoughts),
            )
            logging.info("[agent] → LLM %s", self.model_name)
            function_call_parts, text = await self._llm_stream(self.strip_contents_private(await self.memory.get_contents()), config)
            logging.info("[agent] ← LLM")

            if self._stop_event.is_set(): logging.info("[agent] stopped by user, response not saved"); return

            iteration = 0
            while function_call_parts and iteration < self.max_iterations:
                await self.memory.add_turn({"role": "model", "parts": function_call_parts})

                fn_response_parts = []
                extra_parts = []
                for function_call_part in function_call_parts:
                    tool_call = function_call_part.function_call
                    logging.info("Инструмент: %s", tool_call.name)
                    skill = tool_to_skill.get(tool_call.name)
                    if not skill:
                        logging.warning("Tool %s not found in skills", tool_call.name)
                        continue

                    await self.transport.on_tool_call(tool_call.name, dict(tool_call.args or {}))
                    result = await skill.dispatch_tool_call(tool_call)
                    await self.transport.on_tool_result(tool_call.name, result)
                    extra_parts.extend(result.pop("_parts", []) if isinstance(result, dict) else [])
                    fn_response_parts.append(types.Part.from_function_response(
                        name=tool_call.name,
                        response=result if isinstance(result, dict) else {"result": result},
                    ))

                if fn_response_parts:
                    await self.memory.add_turn({"role": "user", "parts": [*fn_response_parts, *extra_parts]})

                iteration += 1
                logging.info("[agent] → LLM iteration %d", iteration)
                function_call_parts, text = await self._llm_stream(self.strip_contents_private(await self.memory.get_contents()), config, str(iteration))
                logging.info("[agent] ← LLM iteration %d", iteration)

                if self._stop_event.is_set(): logging.info("[agent] stopped by user during iter %d, response not saved", iteration); return

            if function_call_parts:
                logging.warning("[agent] max_iterations=%d reached", self.max_iterations)
                await self.transport.send_message(f"⚠️ Достигнут лимит итераций ({self.max_iterations}). Ответ может быть неполным.")

            await self.memory.add_turn({"role": "model", "parts": [{"text": text or ""}]})

        except Exception as e:
            logging.warning("Ошибка при обращении к Gemini: %s", e, exc_info=True)
            await self.transport.send_message(f"Ошибка при обращении к Gemini: {e}")
