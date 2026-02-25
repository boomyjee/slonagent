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

class Skill:
    def __init__(self):
        self.agent = None
        self._tool_names = set()

        def param_schema(hint, desc):
            _GEMINI_TYPES = { str: types.Type.STRING, int: types.Type.INTEGER, float: types.Type.NUMBER, bool: types.Type.BOOLEAN }
            if get_origin(hint) is list:
                return types.Schema(type=types.Type.ARRAY, description=desc,
                                    items=types.Schema(type=_GEMINI_TYPES.get(get_args(hint)[0], types.Type.STRING)))
            return types.Schema(type=_GEMINI_TYPES.get(hint, types.Type.STRING), description=desc)

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
            self.tools.append(types.FunctionDeclaration(
                name=name,
                description=fn._tool_description,
                parameters=types.Schema(type=types.Type.OBJECT, properties=properties, required=required),
            ))
            self._tool_names.add(name)

    def get_context_prompt(self, user_text: str = "") -> str:
        return ""

    def register(self, agent):
        self.agent = agent

    async def dispatch_tool_call(self, tool_call) -> dict:
        if tool_call.name not in self._tool_names:
            return {"error": f"Unknown tool: {tool_call.name}"}

        method = getattr(self, tool_call.name)
        args = dict(tool_call.args or {})

        if inspect.iscoroutinefunction(method):
            return await method(**args)
        return method(**args)


class Agent:
    def __init__(self, model_name: str, api_key: str, memory, skills: list = [], include_thoughts: bool = False, max_iterations: int = 20):
        self.model_name = model_name
        self.include_thoughts = include_thoughts
        self.memory = memory
        self.skills = [memory] + skills
        self.max_iterations = max_iterations
        for skill in self.skills:
            skill.register(self)

        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        http_client = httpx.Client(proxy=proxy_url) if proxy_url else None
        http_options = {"httpx_client": http_client, "api_version": "v1alpha"} if http_client else {"api_version": "v1alpha"}
        self.client = genai.Client(api_key=api_key, http_options=http_options)
        self._process_message_lock = asyncio.Lock()

    async def process_message(self, message_parts: list, instructions: str = "", transport=None, user_message_id=None):
        async with self._process_message_lock:
            await self._process_message(message_parts, instructions, transport, user_message_id)

    async def _process_message(self, message_parts: list, instructions: str = "", transport=None, user_message_id=None):
        self.transport = transport
        text = next((p["text"] for p in message_parts if isinstance(p, dict) and "text" in p), "")
        logging.info("[agent] incoming: %r", text)

        for skill in self.skills:
            if hasattr(skill, "is_bypass_command") and skill.is_bypass_command(text):
                if transport: await transport.send_message(skill.handle_bypass_command(text))
                return

        await self.memory.add_turn({"role": "user", "parts": message_parts, "_user_message_id": user_message_id})

        tools = []
        tool_to_skill = {}
        for s in self.skills:
            for f in s.tools:
                tools.append(types.Tool(function_declarations=[f]))
                tool_to_skill[f.name] = s

        user_text = " ".join(p.get("text", "") for p in message_parts if isinstance(p, dict) and "text" in p).strip()
        skill_contexts = [s.get_context_prompt(user_text) for s in self.skills]
        system = "\n\n".join(filter(None, [instructions, *skill_contexts]))

        await transport.send_thinking(system)

        async def send_thinking(response):
            if not transport: return
            parts = response.candidates[0].content.parts or []
            thought_parts = [p.text for p in parts if getattr(p, "thought", False) and p.text]
            if thought_parts:
                await transport.send_thinking("\n\n".join(thought_parts))

        try:
            config = types.GenerateContentConfig(
                system_instruction=system,
                temperature=1.0,
                tools=tools,
                thinking_config=types.ThinkingConfig(include_thoughts=self.include_thoughts),
            )
            response = await asyncio.to_thread(
                self.client.models.generate_content,
                model=self.model_name, contents=self.memory.get_contents(), config=config,
            )
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
                response = await asyncio.to_thread(
                    self.client.models.generate_content,
                    model=self.model_name, contents=self.memory.get_contents(), config=config,
                )
                await send_thinking(response)

            await self.memory.add_turn({"role": "model", "parts": [{"text": response.text or ""}]})
            if transport: await transport.send_message(response.text or "")

        except Exception as e:
            logging.exception("Ошибка при обращении к Gemini")
            if transport: await transport.send_message(f"Ошибка: {e}")
