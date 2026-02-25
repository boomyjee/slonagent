import asyncio, os, inspect, logging, httpx
from typing import Annotated, get_type_hints, get_args, get_origin
from google import genai
from google.genai import types


def tool(description: str):
    def decorator(fn):
        fn._is_tool = True
        fn._tool_description = description
        return fn
    return decorator


def bypass(command: str):
    def decorator(fn):
        fn._is_bypass = True
        fn._bypass_command = command
        return fn
    return decorator


class Skill:
    def __init__(self):
        self.agent = None
        self._tool_names = set()
        self._bypass_handlers = {}
        for name, fn in inspect.getmembers(type(self), predicate=inspect.isfunction):
            if getattr(fn, "_is_bypass", False):
                self._bypass_handlers[fn._bypass_command] = fn

        def param_schema(hint, desc):
            _GEMINI_TYPES = { str: types.Type.STRING, int: types.Type.INTEGER, float: types.Type.NUMBER, bool: types.Type.BOOLEAN }
            if get_origin(hint) is list:
                return types.Schema(type=types.Type.ARRAY, description=desc,
                                    items=types.Schema(type=_GEMINI_TYPES.get(get_args(hint)[0], types.Type.STRING)))
            return types.Schema(type=_GEMINI_TYPES.get(hint, types.Type.STRING), description=desc)

        class_name = type(self).__name__.removesuffix("Skill").removesuffix("Memory").removesuffix("Provider")
        self.tools = []
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
            self.tools.append(types.FunctionDeclaration(
                name=tool_name,
                description=fn._tool_description,
                parameters=types.Schema(type=types.Type.OBJECT, properties=properties, required=required),
            ))
            self._tool_names.add(tool_name)

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

    def get_context_prompt(self, user_text: str = "") -> str:
        return ""

    def register(self, agent):
        self.agent = agent

    async def dispatch_tool_call(self, tool_call) -> dict:
        if tool_call.name not in self._tool_names:
            return {"error": f"Unknown tool: {tool_call.name}"}

        method_name = tool_call.name.split("_", 1)[1]
        method = getattr(self, method_name)
        args = dict(tool_call.args or {})

        if inspect.iscoroutinefunction(method):
            return await method(**args)
        return method(**args)


class Agent:
    def __init__(self, model_name: str, api_key: str, memory, skills: list = [], include_thoughts: bool = False, max_iterations: int = 20):
        self.model_name = model_name
        self.include_thoughts = include_thoughts
        self.memory = memory
        self.skills = memory.providers + skills
        self.max_iterations = max_iterations
        for skill in self.skills:
            skill.register(self)

        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        http_client = httpx.Client(proxy=proxy_url) if proxy_url else None
        http_options = {"httpx_client": http_client, "api_version": "v1alpha"} if http_client else {"api_version": "v1alpha"}
        self.client = genai.Client(api_key=api_key, http_options=http_options)
        self._process_message_lock = asyncio.Lock()

    async def process_message(self, message_parts: list, transport=None, user_message_id=None):
        async with self._process_message_lock:
            await self._process_message(message_parts, transport, user_message_id)

    async def _process_message(self, message_parts: list, transport=None, user_message_id=None):
        self.transport = transport
        text = next((p["text"] for p in message_parts if isinstance(p, dict) and "text" in p), "")
        logging.info("[agent] incoming: %r", text)

        for skill in self.skills:
            if skill.is_bypass_command(text):
                if transport: await transport.send_message(await skill.dispatch_bypass(text))
                return

        await self.memory.add_turn({"role": "user", "parts": message_parts, "_user_message_id": user_message_id})


        user_text = " ".join(p.get("text", "") for p in message_parts if isinstance(p, dict) and "text" in p).strip()
        tools = []
        tool_to_skill = {}

        system_parts = []
        for skill in self.skills:
            for f in skill.tools:
                tools.append(types.Tool(function_declarations=[f]))
                tool_to_skill[f.name] = skill

            skill_context = skill.get_context_prompt(user_text)
            tools_info = "\n".join(f"⚙️ {t.name}: {t.description}" for t in skill.tools)

            if skill_context: system_parts.append(skill_context)
            if skill_context or tools_info:
                await transport.send_system_prompt(
                    f"[{skill.__class__.__name__}]\n"
                    +skill_context+"\n"
                    +tools_info
                )
                
        system = "\n\n".join(system_parts)

        async def send_thinking(response):
            if not transport: return
            parts = response.candidates[0].content.parts or []
            thought_parts = [p.text for p in parts if getattr(p, "thought", False) and p.text]
            if thought_parts:
                await transport.send_thinking("\n\n".join(thought_parts))

        try:
            config = types.GenerateContentConfig(
                system_instruction="\n\n".join(system_parts),
                temperature=1.0,
                tools=tools,
                thinking_config=types.ThinkingConfig(include_thoughts=self.include_thoughts),
            )
            logging.info("[agent] → LLM %s", self.model_name)
            response = await asyncio.to_thread(
                self.client.models.generate_content,
                model=self.model_name, contents=self.memory.get_contents(), config=config,
            )
            logging.info("[agent] ← LLM")
            await send_thinking(response)

            iteration = 0
            while response.function_calls and iteration < self.max_iterations:
                await self.memory.add_turn(response.candidates[0].content)
                for tool_call in response.function_calls:
                    logging.info("Инструмент: %s", tool_call.name)
                    skill = tool_to_skill.get(tool_call.name)
                    if not skill:
                        logging.warning("Tool %s not found in skills", tool_call.name)
                        continue

                    if transport: await transport.on_tool_call(tool_call.name, dict(tool_call.args or {}))
                    result = await skill.dispatch_tool_call(tool_call)
                    if transport: await transport.on_tool_result(tool_call.name, result)
                    extra = result.pop("_parts", []) if isinstance(result, dict) else []
                    await self.memory.add_turn({"role": "user", "parts": [{"text": f"Результат {tool_call.name}:\n{result}"}, *extra]})

                iteration += 1
                logging.info("[agent] → LLM iteration %d", iteration)
                response = await asyncio.to_thread(
                    self.client.models.generate_content,
                    model=self.model_name, contents=self.memory.get_contents(), config=config,
                )
                logging.info("[agent] ← LLM iteration %d", iteration)
                await send_thinking(response)

            if transport: await transport.send_message(response.text or "")
            await self.memory.add_turn({"role": "model", "parts": [{"text": response.text or ""}]})

        except Exception as e:
            logging.exception("Ошибка при обращении к Gemini")
            if transport: await transport.send_message(f"Ошибка: {e}")
