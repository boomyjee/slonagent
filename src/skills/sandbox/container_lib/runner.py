"""Runner for slonagent sandbox scripts.

Connects to host via bidirectional JSON-lines RPC (rpc.Channel).
Exposes a single run_tool(name, args) method for host to call.

Usage:
    python runner.py script.py   # started by host via podman exec
"""

import sys, json, asyncio, inspect, importlib.util
from agent import Skill
from src.transport.base import BaseTransport
from src.transport.web import WebTransport
from rpc import Channel


class Runner:
    def __init__(self, mod):
        self._mod = mod
        self._skills = {}  # cls → instance

    async def run_tool(self, name, args, agent):
        for _, cls in inspect.getmembers(self._mod, inspect.isclass):
            if not issubclass(cls, Skill) or cls is Skill:
                continue
            prefix = cls.__name__.removesuffix("Skill").removesuffix("Memory").removesuffix("Provider").lower()
            for mname, fn in inspect.getmembers(cls, predicate=inspect.isfunction):
                if not getattr(fn, "_is_tool", False) or f"{prefix}_{mname}" != name:
                    continue
                if cls not in self._skills:
                    skill = cls()
                    skill.agent = agent
                    self._skills[cls] = skill
                return await fn(self._skills[cls], **args) if asyncio.iscoroutinefunction(fn) else fn(self._skills[cls], **args)
        raise AttributeError(f"tool not found: {name}")


def main():
    # Write as UTF-8 bytes directly; avoids container locale (cp1252/etc.)
    # failing on non-ASCII characters like emojis in RPC payloads.
    def writeline(msg):
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    def readline():
        line = sys.stdin.buffer.readline()
        return line.decode("utf-8") if line else ""

    script_path = sys.argv[1]
    spec = importlib.util.spec_from_file_location("_script", script_path)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = script_path
    sys.modules["_script"] = mod
    spec.loader.exec_module(mod)

    ch = Channel(readline, writeline, ref_prefix="s", allowed={
        Skill: {"register", "start", "get_context_prompt", "get_tool_prompt", "get_tools", "is_bypass_command", "dispatch_bypass", "dispatch_tool_call"},
        Runner: {"run_tool"},
        BaseTransport: None,
        WebTransport: {"ws_handle_message", "handle_route"},
    })
    ch.register("runner", Runner(mod))
    ch.start()
    ch.join()


if __name__ == "__main__":
    main()
