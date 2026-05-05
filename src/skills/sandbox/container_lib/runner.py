"""Tool entry-point для скриптов в песочнице.

Usage: python runner.py <script_path> <tool_name> <args_json> <thread_id>

Импортит скрипт, находит @tool-метод на Skill-классе по имени, создаёт
скилл (с _thread_id для лениво-резолвящегося self.agent), вызывает,
печатает результат в stdout с маркером.

Запускается host'ом через `podman exec`. Один tool-call = один процесс."""

import sys, json, asyncio, inspect, importlib.util

from agent import Skill

RESULT_MARKER = "__SLONAGENT_RESULT__"


def _load(path):
    spec = importlib.util.spec_from_file_location("_script", path)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = path
    sys.modules["_script"] = mod
    spec.loader.exec_module(mod)
    return mod


def _find_tool(mod, tool_name):
    for _, cls in inspect.getmembers(mod, inspect.isclass):
        if not issubclass(cls, Skill) or cls is Skill:
            continue
        prefix = cls.__name__.removesuffix("Skill").removesuffix("Memory").removesuffix("Provider").lower()
        for mname, fn in inspect.getmembers(cls, predicate=inspect.isfunction):
            if getattr(fn, "_is_tool", False) and f"{prefix}_{mname}" == tool_name:
                return cls, fn
    raise AttributeError(f"tool not found: {tool_name}")


async def main():
    script, tool_name, args_json, thread_id = sys.argv[1:5]
    mod = _load(script)
    cls, fn = _find_tool(mod, tool_name)
    skill = cls()
    skill._thread_id = thread_id
    result = fn(skill, **json.loads(args_json))
    if asyncio.iscoroutine(result):
        result = await result
    if not isinstance(result, dict):
        result = {"result": result}
    sys.stdout.buffer.write(
        (RESULT_MARKER + json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8")
    )
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    asyncio.run(main())
