"""Runner for slonagent sandbox scripts.

Discovers all Skill subclasses in a given module, like pytest discovers Test classes.

Usage:
    python -m runner script.py --introspect   # list tools as JSON
    python -m runner script.py                # run RPC loop
"""

import sys, json, asyncio, inspect, importlib.util
from typing import Annotated, get_type_hints, get_args, get_origin

_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _load_module(path):
    spec = importlib.util.spec_from_file_location("_script", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _find_skills(mod):
    from slonagent import Skill
    return [
        obj for _, obj in inspect.getmembers(mod, inspect.isclass)
        if issubclass(obj, Skill) and obj is not Skill
    ]


def _introspect(skill_cls):
    tools = []
    class_name = skill_cls.__name__.removesuffix("Skill").removesuffix("Memory").removesuffix("Provider")

    for name, fn in inspect.getmembers(skill_cls, predicate=inspect.isfunction):
        if not getattr(fn, "_is_tool", False):
            continue

        hints = get_type_hints(fn, include_extras=True)
        sig = inspect.signature(fn)
        params = {k: v for k, v in sig.parameters.items() if k != "self"}

        properties = {}
        required = []
        for k, p in params.items():
            hint = hints.get(k)
            if get_origin(hint) is Annotated:
                args = get_args(hint)
                base_type = args[0]
                desc = args[1] if len(args) > 1 and isinstance(args[1], str) else ""
                if get_origin(base_type) is list:
                    schema = {"type": "array", "items": {"type": _JSON_TYPES.get(get_args(base_type)[0], "string")}}
                else:
                    schema = {"type": _JSON_TYPES.get(base_type, "string")}
                if desc:
                    schema["description"] = desc
            else:
                schema = {"type": _JSON_TYPES.get(hint, "string")}

            properties[k] = schema
            if p.default is inspect.Parameter.empty:
                required.append(k)

        tools.append({
            "type": "function",
            "function": {
                "name": f"{class_name.lower()}_{name}",
                "description": fn._tool_description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                }
            }
        })

    return tools


async def _main(skill_classes):
    tool_map = {}
    for cls in skill_classes:
        skill = cls()
        class_name = type(skill).__name__.removesuffix("Skill").removesuffix("Memory").removesuffix("Provider")
        for mname, fn in inspect.getmembers(type(skill), predicate=inspect.isfunction):
            if getattr(fn, "_is_tool", False):
                tool_map[f"{class_name.lower()}_{mname}"] = (skill, fn)

    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        msg = json.loads(line)

        if msg["method"] == "call":
            tool_name = msg["args"][0]
            kwargs = msg.get("kwargs", {})
            entry = tool_map.get(tool_name)
            if entry:
                skill, method = entry
                if asyncio.iscoroutinefunction(method):
                    result = await method(skill, **kwargs)
                else:
                    result = method(skill, **kwargs)
            else:
                result = {"error": f"not found: {tool_name}"}

            sys.stdout.write(json.dumps({"method": "result", "args": [result], "kwargs": {}}) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    script_path = sys.argv[1]
    mod = _load_module(script_path)
    skills = _find_skills(mod)

    if "--introspect" in sys.argv:
        tools = []
        for cls in skills:
            tools.extend(_introspect(cls))
        print(json.dumps({"type": "tools", "tools": tools}))
    else:
        asyncio.run(_main(skills))
