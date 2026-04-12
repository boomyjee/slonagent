"""CodingMode — IDE sub-agent with Monaco editor, file tree, and shared chat.

Spawns a sub-agent whose transport is a MultiTransport fanning out to both
the parent's transport (Telegram etc.) and a CodingTransport (web IDE).
CodingTransport inherits from WebTransport so it reuses the shared server,
tunnel, auth, static serving, and the chat wire protocol. On top of that
it adds file API routes and a file watcher.
"""
import asyncio, logging, os
from pathlib import Path
from typing import Annotated

from fastapi import Query, Request
from fastapi.responses import JSONResponse

from agent import Skill, tool
from src.transport.multi import MultiTransport
from src.transport.web import WebTransport

log = logging.getLogger(__name__)


class CodingTransport(WebTransport):
    """Web IDE transport: file API + file watcher on top of WebTransport."""

    def __init__(self, root_path: str):
        super().__init__(prefix="/coding", verbose=False)
        self.root_path = root_path
        self.resolve_path = None
        self.workspace_host_dir = None
        self._watch_task = None

    def set_agent(self, agent):
        # API routes must be registered before the catch-all static route
        # that super().set_agent adds, so we ensure server + agent first,
        # register API routes, then let super add ws + static.
        self.agent = agent
        self._ensure_server()
        self.register_route("get", "/api/config", self._api_config)
        self.register_route("get", "/api/files", self._api_list_files)
        self.register_route("get", "/api/file", self._api_read_file)
        self.register_route("put", "/api/file", self._api_write_file)
        super().set_agent(agent)

    def start_watcher(self):
        if self.workspace_host_dir:
            self._watch_task = asyncio.create_task(self._watch_files())

    def cleanup(self):
        if self._watch_task:
            self._watch_task.cancel()
        super().cleanup()

    async def _api_config(self):
        return JSONResponse({"root_path": self.root_path})

    async def _api_list_files(self, path: str = Query("/")):
        host_path = self.resolve_path(path)
        if host_path is None:
            return JSONResponse({"error": f"Access denied: {path}"}, 403)
        if not os.path.isdir(host_path):
            return JSONResponse({"error": f"Not a directory: {path}"}, 400)
        entries = []
        for name in sorted(os.listdir(host_path)):
            if name.startswith("."):
                continue
            full = os.path.join(host_path, name)
            entries.append({
                "name": name,
                "is_dir": os.path.isdir(full),
                "path": path.rstrip("/") + "/" + name,
            })
        return JSONResponse({"entries": entries})

    async def _api_read_file(self, path: str = Query(...)):
        host_path = self.resolve_path(path)
        if host_path is None:
            return JSONResponse({"error": f"Access denied: {path}"}, 403)
        if not os.path.isfile(host_path):
            return JSONResponse({"error": f"Not a file: {path}"}, 400)
        try:
            with open(host_path, encoding="utf-8", errors="replace") as f:
                content = f.read()
            return JSONResponse({"path": path, "content": content})
        except Exception as e:
            return JSONResponse({"error": str(e)}, 500)

    async def _api_write_file(self, request: Request):
        data = await request.json()
        path, content = data.get("path"), data.get("content")
        host_path = self.resolve_path(path)
        if host_path is None:
            return JSONResponse({"error": f"Access denied: {path}"}, 403)
        try:
            os.makedirs(os.path.dirname(host_path), exist_ok=True)
            with open(host_path, "w", encoding="utf-8") as f:
                f.write(content)
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return JSONResponse({"error": str(e)}, 500)

    async def _watch_files(self):
        from watchfiles import awatch, Change
        try:
            host_base = Path(self.workspace_host_dir)
            async for changes in awatch(self.workspace_host_dir):
                paths = []
                has_create_delete = False
                for change_type, path in changes:
                    try:
                        rel = "/" + Path(path).relative_to(host_base).as_posix()
                    except ValueError:
                        continue
                    paths.append(rel)
                    if change_type in (Change.added, Change.deleted):
                        has_create_delete = True
                if paths:
                    await self.send({"type": "files_changed", "paths": paths, "tree": has_create_delete})
        except asyncio.CancelledError:
            return
        except Exception:
            log.warning("[coding] file watcher crashed", exc_info=True)


class CodingModeSkill(Skill):
    @tool("Запустить кодинг режим с веб-интерфейсом для работы с кодом")
    async def launch(
        self,
        task: Annotated[str, "Задача для кодинг-агента"] = "",
        project_path: Annotated[str, "Путь к проекту"] = "/workspace",
    ) -> dict:
        from src.modes.coding.coding_skill import CodingSkill
        from src.skills.sandbox import SandboxSkill
        from src.skills.web import WebSkill

        parent_sandbox = next((s for s in self.agent.skills if isinstance(s, SandboxSkill)), None)
        parent_web = next((s for s in self.agent.skills if isinstance(s, WebSkill)), None)
        workspace_dir = parent_sandbox.workspace_dir if parent_sandbox else None

        coding_transport = CodingTransport(project_path)
        coding_skill = CodingSkill()
        sub = await self.agent.spawn_subagent(
            "coding_mode",
            memory_providers=[],
            skills=[
                coding_skill,
                SandboxSkill(workspace_dir=workspace_dir),
                WebSkill(parent_web.api_key if parent_web else ""),
            ],
            transport=MultiTransport([self.agent.transport, coding_transport]),
        )

        sub_sandbox = next(s for s in sub.skills if isinstance(s, SandboxSkill))
        coding_transport.resolve_path = sub_sandbox.resolve_path
        coding_transport.workspace_host_dir = sub_sandbox.workspace_dir
        coding_transport.start_watcher()

        initial = f"Project root: {project_path}"
        if task:
            initial += f"\n\nTask: {task}"
        await sub.memory.add_turn({"role": "user", "content": initial})

        url = await coding_transport.get_url('/')
        await self.agent.transport.send_message(
            f"\U0001f4bb Coding mode: {url}\nДля выхода: /stop"
        )

        from src.agent.agent import stoppable
        try:
            await stoppable(sub.loop(), coding_skill.done)
        finally:
            coding_transport.cleanup()

        return {"result": coding_skill.result} if coding_skill.done.is_set() else {"status": "interrupted"}
