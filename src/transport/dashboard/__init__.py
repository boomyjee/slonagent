import asyncio, logging, os
from contextvars import ContextVar
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from agent import Skill
from src.transport.web import WebTransport
from src.transport.dashboard.files import FilesAPI
from src.transport.dashboard.git import GitAPI
from src.transport.dashboard.sandbox_proxy import SandboxProxy
from src.transport.dashboard.watcher import WatcherPool

log = logging.getLogger(__name__)

agent_context: ContextVar[str] = ContextVar("agent_id", default="main")


class _LogHandler(logging.Handler):
    _instances: dict[str, "DashboardTransport"] = {}

    def _category(self, name: str) -> str:
        if name.startswith(("src.memory", "memory")):
            return "memory"
        if name.startswith(("aiogram", "src.transport", "httpx", "uvicorn", "asyncssh",
                            "google_genai", "sentence_transformers", "huggingface_hub")):
            return "transport"
        return "agent"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            agent_id = agent_context.get()
            transport = self._instances.get(agent_id)
            if not transport:
                return
            category = self._category(record.name)
            record.agent_id = agent_id
            text = self.format(record)
            event = {"type": "log", "category": category, "level": record.levelname, "text": text}
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            # Defer coroutine creation into the closure so a loop that's
            # already shutting down doesn't leave an un-awaited coroutine
            # behind ("RuntimeWarning: coroutine ... was never awaited").
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(transport.send(event, replay=True))
            )
        except Exception:
            self.handleError(record)


class DashboardSkill(Skill):
    """Dashboard-specific context: web hosting под /web/, /web-hook,
    /sandbox/PORT/-проксирование. Скачивание файлов и /web bypass — на
    общем WebTransportSkill, у Dashboard-only функционала тут не нужны."""

    def __init__(self, transport: "DashboardTransport"):
        self.transport = transport
        super().__init__()

    async def get_context_prompt(self, user_text: str = "") -> str:
        if not self.transport._sandbox:
            return ""
        url = await self.transport.get_url("")
        return (
            f"У тебя есть веб-хостинг: файлы в /workspace/web/ доступны по URL {url}/web/. "
            f"Там же есть вебхук {url}/web-hook — POST-запрос на него придёт тебе как сообщение. "
            "Используй это для создания интерактивных веб-приложений. "
            f"Если запустишь внутри сандбокса свой сервер на 127.0.0.1:PORT "
            f"(http/https или ws/wss), он доступен снаружи по {url}/sandbox/PORT/ — "
            "порты контейнера наружу не проброшены, но dashboard проксирует HTTP и WebSocket "
            "через обратный туннель автоматически."
        )


class DashboardTransport(WebTransport):
    """Full agent dashboard: chat + logs tab, mounted at /{agent_id}/dashboard/."""

    _log_handler: _LogHandler | None = None

    def __init__(self, verbose: bool = True):
        super().__init__(prefix="/dashboard", verbose=verbose)
        self._skill = DashboardSkill(self)
        self._proxy = SandboxProxy(self)
        self._files = FilesAPI(self)
        self._git = GitAPI(self)
        self._watchers = WatcherPool(self._files)
        self._default_root: str | None = None

    @property
    def _sandbox(self):
        from src.skills.sandbox import SandboxSkill
        return next((s for s in self.agent.skills if isinstance(s, SandboxSkill)), None)

    @property
    def default_root(self) -> str:
        if self._default_root is None:
            sandbox = self._sandbox
            self._default_root = sandbox.workspace_dir if sandbox \
                else os.path.join(self.agent.memory.memory_dir, "workspace")
            os.makedirs(self._default_root, exist_ok=True)
        return self._default_root

    def get_skills(self):
        return [*super().get_skills(), self._skill]

    def register_routes(self):
        self._files.register()
        self._git.register()
        self.register_route("get", "/web/{filepath:path}", self._serve_web)
        self.register_route("post", "/web-hook", self._web_hook)
        # Reverse-tunnel proxy for ports bound inside the sandbox container.
        # Tunnel endpoint must be registered before the generic /sandbox/{port}
        # proxy so its static path wins over path-capture routing.
        self.register_route("websocket", "/sandbox-tunnel", self._proxy.handle_tunnel)
        self.register_route("websocket", "/sandbox/{port:int}/{filepath:path}", self._proxy.handle_ws)
        for m in ("get", "post", "put", "patch", "delete", "options", "head"):
            self.register_route(m, "/sandbox/{port:int}/{filepath:path}", self._proxy.handle_http)
        super().register_routes()


    async def ws_handle_message(self, msg: dict, ws=None):
        await self._watchers.ws_handle_message(msg, ws)
        await super().ws_handle_message(msg, ws)

    def on_ws_close(self, ws):
        self._watchers.on_ws_close(ws)

    async def _serve_web(self, filepath: str):
        sandbox = self._sandbox
        if not sandbox:
            return Response("No sandbox", status_code=404)
        web_dir = Path(sandbox.workspace_dir) / "web"
        path = (web_dir / filepath).resolve()
        if not path.is_file() or not path.is_relative_to(web_dir.resolve()):
            return Response("Not found", status_code=404)
        mime = self._MIME.get(path.suffix.lstrip("."), "text/plain")
        headers = {"Cache-Control": "no-store"} if path.suffix.lstrip(".") in self._MIME else {}
        return Response(path.read_bytes(), media_type=mime, headers=headers)

    async def _web_hook(self, request: Request):
        body = (await request.body()).decode()
        text = f"[web-hook] {body}"
        await self.inject_message(text)
        await self.process_message(
            content_parts=[{"type": "text", "text": text}],
            trigger_answer=True,
        )
        return Response("ok")

    def set_agent(self, agent):
        super().set_agent(agent)

        if DashboardTransport._log_handler is None:
            handler = _LogHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s %(name)s - %(levelname)s - %(message)s"))
            logging.getLogger().addHandler(handler)
            DashboardTransport._log_handler = handler

        _LogHandler._instances[agent.id] = self
        agent_context.set(agent.id)
