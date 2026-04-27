import inspect, json, logging
from typing import Annotated, get_type_hints, get_args, get_origin


def tool(description):
    """description может быть str или callable(self) → str для динамических описаний."""
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

        class_name = type(self).__name__.removesuffix("Skill").removesuffix("Mode").removesuffix("Memory").removesuffix("Provider")
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
                    "description": fn._tool_description,  # str | callable(self) → str
                    "parameters": {"type": "object", "properties": properties, "required": required},
                },
            })
            self._tool_map[tool_name] = name

    def get_tools(self) -> list:
        """Возвращает список OpenAI-format tool dict для этого скилла. Переопределяй для динамических тулов."""
        result = []
        for t in self._tools:
            desc = t["function"]["description"]
            if callable(desc):
                t = {**t, "function": {**t["function"], "description": desc(self)}}
            result.append(t)
        return result

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
        try:
            result = fn(self, args) if not inspect.iscoroutinefunction(fn) else await fn(self, args)
        except Exception as e:
            logging.exception("[skill] bypass /%s failed", cmd)
            return f"⚠️ /{cmd}: {e}"
        return str(result) if result is not None else ""

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