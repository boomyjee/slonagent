import os, inspect, logging, httpx
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
    def __init__(self, model_name: str, api_key: str, skills: list = None, max_iterations: int = 20):
        self.model_name = model_name
        self.skills = skills or []
        self.messages = []
        self.max_iterations = max_iterations
        for skill in self.skills:
            skill.register(self)

        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        http_client = httpx.Client(proxy=proxy_url) if proxy_url else None
        http_options = {"httpx_client": http_client, "api_version": "v1alpha"} if http_client else {"api_version": "v1alpha"}
        self.client = genai.Client(api_key=api_key, http_options=http_options)

    async def process_message(self, message_parts: list, instructions: str = "", transport=None):
        self.transport = transport
        text = next((p["text"] for p in message_parts if isinstance(p, dict) and "text" in p), "")
        logging.info("[agent] incoming: %r", text)

        for skill in self.skills:
            if hasattr(skill, "is_bypass_command") and skill.is_bypass_command(text):
                if transport: await transport.send_message(skill.handle_bypass_command(text))
                return

        self.messages.append({"role": "user", "parts": message_parts})

        tools = []
        tool_to_skill = {}
        for s in self.skills:
            for f in s.tools:
                tools.append(types.Tool(function_declarations=[f]))
                tool_to_skill[f.name] = s

        contents = self.messages[-20:]
        skill_context = "\n\n".join(s.get_context_prompt() for s in self.skills if hasattr(s, "get_context_prompt"))
        system = "\n\n".join(filter(None, [instructions, skill_context]))

        async def send_thinking(response):
            if not transport: return
            parts = response.candidates[0].content.parts
            thought_parts = [p.text for p in parts if getattr(p, "thought", False) and p.text]
            if thought_parts:
                await transport.send_thinking("\n\n".join(thought_parts))

        try:
            config = types.GenerateContentConfig(system_instruction=system, temperature=0.7, tools=tools)
            response = self.client.models.generate_content(model=self.model_name, contents=contents, config=config)
            await send_thinking(response)

            iteration = 0
            while response.function_calls and iteration < self.max_iterations:
                contents.append(response.candidates[0].content)
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
                    contents.append({"role": "user", "parts": [{"text": f"Результат {tool_call.name}:\n{result}"}, *extra]})

                iteration += 1
                response = self.client.models.generate_content(model=self.model_name, contents=contents, config=config)
                await send_thinking(response)

            self.messages.append({"role": "model", "parts": [{"text": response.text}]})
            if transport: await transport.send_message(response.text)

            for s in self.skills:
                if hasattr(s, "on_message_processed"): await s.on_message_processed(self.messages)

        except Exception as e:
            logging.exception("Ошибка при обращении к Gemini")
            if transport: await transport.send_message(f"Ошибка: {e}")
