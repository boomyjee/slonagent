"""Обобщённый скилл «запустить одну зарегистрированную программу на хосте».

Регистрируется юзером в конфиге, по инстансу на программу:

    {"__class__": "src.skills.run.RunSkill", "name": "separator",
     "path": "E:/path/to/separator.bat", "cwd": "E:/work", "timeout": 900}

Даёт тул `run_<name>` (например `run_separator`), доступный и LLM-агенту,
и программно из песочницы через RPC:

    get_agent().dispatch_tool_calls(
        {"tool_calls": [{"id": "1", "function": {"name": "run_separator",
                         "arguments": json.dumps({"args": [...]})}}]},
        emit_transport_events=False)

Безопасность: программа фиксирована конфигом (песочница выбирает только
аргументы), запуск без shell — инъекции через аргументы невозможны.
"""

import asyncio
import logging
from typing import Annotated

from agent import Skill, tool

_OUTPUT_LIMIT = 100_000  # символов stdout/stderr в ответе тула


class RunSkill(Skill):
    def __init__(self, name: str, path: str, cwd: str | None = None, timeout: int = 900):
        super().__init__()
        self.name, self.path, self.cwd, self.timeout = name, path, cwd, timeout
        # Имя тула генерится из имени класса ("run_run") — одинаково для всех
        # инстансов. Переименовываем в run_<name>, чтобы инстансы не коллидили.
        tool_name = f"run_{name}"
        self._tools[0]["function"]["name"] = tool_name
        self._tools[0]["function"]["description"] = f"Запустить {name} ({path}) с аргументами командной строки."
        self._tool_map = {tool_name: "run"}

    @tool("описание подставляется в __init__")
    async def run(self, args: Annotated[list[str], "Аргументы командной строки"] = []):
        argv = [self.path, *[str(a) for a in args]]
        logging.info("RunSkill %s: %s", self.name, argv)
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=self.cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return {"error": f"timeout {self.timeout}s"}
        return {
            "exit_code": proc.returncode,
            "stdout": stdout.decode("utf-8", "replace")[-_OUTPUT_LIMIT:],
            "stderr": stderr.decode("utf-8", "replace")[-_OUTPUT_LIMIT:],
        }
