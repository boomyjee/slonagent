"""Runner for slonagent sandbox scripts.

Discovers all Skill subclasses in a given module, like pytest discovers Test classes.

Usage:
    python -m runner script.py   # run RPC loop
"""

import sys, json, asyncio, inspect, importlib.util


async def main():
    from agent import Skill

    spec = importlib.util.spec_from_file_location("_script", sys.argv[1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    tool_map = {}
    for cls in inspect.getmembers(mod, inspect.isclass):
        cls = cls[1]
        if not issubclass(cls, Skill) or cls is Skill:
            continue
        skill = cls()
        prefix = type(skill).__name__.removesuffix("Skill").removesuffix("Memory").removesuffix("Provider").lower()
        for mname, fn in inspect.getmembers(type(skill), predicate=inspect.isfunction):
            if getattr(fn, "_is_tool", False):
                tool_map[f"{prefix}_{mname}"] = (skill, fn)

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
    asyncio.run(main())
