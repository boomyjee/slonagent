import asyncio, logging, os
from contextvars import ContextVar
from pathlib import Path

from fastapi import Request
from fastapi.responses import Response

from typing import Annotated

from agent import Skill, tool
from src.transport.web import WebTransport, WebFork
from src.transport.dashboard.files import FilesAPI
from src.transport.dashboard.git import GitAPI
from src.transport.dashboard.sandbox_proxy import SandboxProxy
from src.transport.dashboard.watcher import WatcherPool

log = logging.getLogger(__name__)

agent_context: ContextVar[str] = ContextVar("agent_id", default="main")


class _LogHandler(logging.Handler):
    """Process-wide stdlib log handler — fan-out to per-fork dashboards.
    Routing key — `agent_context` (ContextVar set in DashboardTransport.set_agent).
    Без активного фока с этим id запись игнорируется."""
    _instances: dict[str, "DashboardFork"] = {}
    _installed: "logging.Handler | None" = None

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
            fork = self._instances.get(agent_id)
            if not fork:
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
                lambda: asyncio.ensure_future(fork.send(event, replay=True))
            )
        except Exception:
            self.handleError(record)


class DashboardSkill(Skill):
    """Per-agent skill with dashboard-only knowledge: open_tab tool and
    web-hosting context prompt. Reaches the shared fork via `transport.fork`."""

    def __init__(self, transport: "DashboardTransport"):
        self.transport = transport
        super().__init__()

    async def get_context_prompt(self, user_text: str = "") -> str:
        fork = self.transport.fork
        if not fork.ref_agent.sandbox:
            return ""
        url = await fork.get_url("")
        return (
            f"У тебя есть веб-доступ к файлам песочницы по схеме {url}/~<container-path>: "
            f"например {url}/~/workspace/index.html отдаст /workspace/index.html, "
            f"{url}/~/mnt/ro/foo.txt — файл из ro-маунта /mnt/ro/foo.txt. "
            "Любой .py-файл по такому URL исполняется как CGI-скрипт в песочнице — "
            "доступны globals `request` (.method/.path/.query/.headers/.cookies/.body), "
            "`header(name, value)` для ответных заголовков, всё из `print()` уходит в body. "
            f"Там же есть вебхук {url}/web-hook — POST-запрос на него придёт тебе как сообщение. "
            "Используй это для создания интерактивных веб-приложений. "
            f"Если запустишь внутри сандбокса свой сервер на 127.0.0.1:PORT "
            f"(http/https или ws/wss), он доступен снаружи по {url}/sandbox/PORT/ — "
            "порты контейнера наружу не проброшены, но dashboard проксирует HTTP и WebSocket "
            "через обратный туннель автоматически."
        )

    @tool(
        "Открыть вкладку в дашборде у пользователя. Если такая уже есть — "
        "переключиться на неё и обновить содержимое. URI-схема:\n"
        "  /<path>                       — файл (Editor)\n"
        "  git:<path>                    — git-панель (diff, log, status)\n"
        "  git-show:<repo>:<ref>:<file>  — файл на конкретном коммите\n"
        "  git-blame:<path>              — blame\n"
        "  url:<full url>                — встраиваемая страница (iframe)\n"
        "  logs                          — лог-панель"
    )
    async def open_tab(
        self,
        uri: Annotated[str, "URI вкладки по схеме выше."],
    ) -> str:
        try:
            await self.transport.fork.send_targeted({"type": "open_tab", "uri": uri})
        except RuntimeError as e:
            return f"Не получилось: {e}"
        return f"Открыта вкладка {uri}"


class DashboardFork(WebFork):
    """Per-agent.id fork: chat + logs + IDE-like file/git/web-hosting/sandbox-proxy.
    Mounted под /{agent_id}/dashboard/."""

    prefix = "/dashboard"

    _WEB_INDEX_NAMES = ("index.html", "index.htm", "index.py")

    def __init__(self, ref_agent):
        self._proxy = SandboxProxy(self)
        self._files = FilesAPI(self)
        self._git = GitAPI(self)
        self._watchers = WatcherPool(self._files)
        self._default_root: str | None = None
        super().__init__(ref_agent)
        # Singleton stdlib log handler — install once, register fork in
        # _instances so emit() can route by agent_context.
        if _LogHandler._installed is None:
            handler = _LogHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s %(name)s - %(levelname)s - %(message)s"))
            logging.getLogger().addHandler(handler)
            _LogHandler._installed = handler
        _LogHandler._instances[ref_agent.id] = self

    def cleanup(self):
        _LogHandler._instances.pop(self.ref_agent.id, None)
        super().cleanup()

    @property
    def default_root(self) -> str:
        if self._default_root is None:
            sandbox = self.ref_agent.sandbox
            self._default_root = sandbox.workspace_dir if sandbox \
                else os.path.join(self.ref_agent.memory.memory_dir, "workspace")
            os.makedirs(self._default_root, exist_ok=True)
        return self._default_root

    # ─── routes ────────────────────────────────────────────────────────────

    def register_routes(self):
        self._files.register()
        self._git.register()
        # Все методы — для CGI (.py) скриптов могут понадобиться POST/PUT/etc.
        # Не-.py файлы вне GET отвечают 405 внутри хендлера.
        for m in ("get", "post", "put", "patch", "delete", "options", "head"):
            self.register_route(m, "/~/{filepath:path}", self._serve_sandbox_fs)
        self.register_route("post", "/web-hook", self._web_hook)
        # Reverse-tunnel proxy for ports bound inside the sandbox container.
        # Tunnel endpoint must be registered before the generic /sandbox/{port}
        # proxy so its static path wins over path-capture routing.
        # auth=False: воркер коннектится из контейнера (host=podman-bridge,
        # не localhost), cookie приложить не может; защищён сетевой
        # изоляцией — порт хоста для контейнера наружу не пробрасывается.
        self.register_route("websocket", "/sandbox-tunnel", self._proxy.handle_tunnel, auth=False)
        self.register_route("websocket", "/sandbox/{port:int}/{filepath:path}", self._proxy.handle_ws, auth=False)
        for m in ("get", "post", "put", "patch", "delete", "options", "head"):
            self.register_route(m, "/sandbox/{port:int}/{filepath:path}", self._proxy.handle_http)
        super().register_routes()

    async def _serve_sandbox_fs(self, filepath: str, request: Request):
        sandbox = self.ref_agent.sandbox
        if not sandbox:
            return Response("No sandbox", status_code=404)
        container_path = "/" + filepath.rstrip("/")
        host_path = sandbox.resolve_path(container_path)
        if host_path is None:
            return Response("Not allowed", status_code=403)
        path = Path(host_path)
        if path.is_dir():
            for name in self._WEB_INDEX_NAMES:
                cand = path / name
                if cand.is_file():
                    path = cand
                    container_path = container_path.rstrip("/") + "/" + name
                    break
            else:
                return Response("Not found", status_code=404)
        if not path.is_file():
            return Response("Not found", status_code=404)
        if path.suffix == ".py":
            return await self._proxy.handle_cgi(container_path, request)
        if request.method != "GET":
            return Response("Method not allowed", status_code=405)
        mime = self._MIME.get(path.suffix.lstrip("."), "text/plain")
        headers = {"Cache-Control": "no-store"} if path.suffix.lstrip(".") in self._MIME else {}
        return Response(path.read_bytes(), media_type=mime, headers=headers)

    async def _web_hook(self, request: Request):
        body = (await request.body()).decode()
        text = f"[web-hook] {body}"
        await self.send({
            "type": "transport", "method": "inject_message",
            "thread_id": "", "text": text,
        }, replay=True)
        if "" not in self.transports:
            from agent import Agent
            await Agent.get(self.ref_agent.id, "")
        await self.transports[""].process_message(
            content_parts=[{"type": "text", "text": text}],
            trigger_answer=True,
        )
        return Response("ok")

    # ─── ws extensions ─────────────────────────────────────────────────────

    async def ws_handle_message(self, msg: dict, ws=None):
        await self._watchers.ws_handle_message(msg, ws)
        await super().ws_handle_message(msg, ws)

    def on_ws_close(self, ws):
        self._watchers.on_ws_close(ws)


class DashboardTransport(WebTransport):
    """Thin BaseTransport adapter — owns DashboardSkill (per-agent), spawns
    a DashboardFork on first set_agent of a given agent.id."""

    _fork_cls = DashboardFork


    def create_fork(self, agent):
        return DashboardFork(ref_agent=agent)

    def get_skills(self):
        return [*super().get_skills(), DashboardSkill(self)]

    def set_agent(self, agent):
        super().set_agent(agent)
        # ContextVar-binding — лог-категории и таргетинг _LogHandler идут
        # по agent_context (read in emit()). Set per-agent set_agent чтобы
        # каждый task внутри этого агента нашёл своё значение.
        agent_context.set(agent.id)
