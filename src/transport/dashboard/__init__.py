import asyncio, logging
from contextvars import ContextVar
from pathlib import Path

from fastapi import Request
from fastapi.responses import Response

from agent import Skill, bypass
from src.transport.web import WebTransport

log = logging.getLogger(__name__)

agent_context: ContextVar[str] = ContextVar("agent_id", default="main")


class _LogHandler(logging.Handler):
    _instances: dict[str, "DashboardTransport"] = {}

    def _category(self, name: str) -> str:
        if name.startswith("src.memory") or name.startswith("memory"):
            return "memory"
        if name.startswith("aiogram") or name.startswith("src.transport") or name.startswith("httpx") or name.startswith("uvicorn") or name.startswith("google_genai") or name.startswith("sentence_transformers") or name.startswith("huggingface_hub"):
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
            loop.call_soon_threadsafe(asyncio.ensure_future, transport.send(event, replay=True))
        except Exception:
            self.handleError(record)


class DashboardSkill(Skill):
    def __init__(self, transport: "DashboardTransport"):
        self.transport = transport
        super().__init__()

    @bypass("dashboard", "Ссылка на веб-дашборд", standalone=True)
    async def dashboard_command(self, args: str) -> str:
        return f"🖥 {await self.transport.get_url('/')}"

    async def get_context_prompt(self, user_text: str = "") -> str:
        if not self.transport._sandbox:
            return ""
        url = await self.transport.get_url("")
        return (
            f"У тебя есть веб-хостинг: файлы в /workspace/web/ доступны по URL {url}/web/. "
            f"Там же есть вебхук {url}/web-hook — POST-запрос на него придёт тебе как сообщение. "
            "Используй это для создания интерактивных веб-приложений."
        )


class DashboardTransport(WebTransport):
    """Full agent dashboard: chat + logs tab, mounted at /{agent_id}/dashboard/."""

    _log_handler: _LogHandler | None = None

    def __init__(self):
        super().__init__(prefix="/dashboard")
        self._skill = DashboardSkill(self)

    @property
    def _sandbox(self):
        from src.skills.sandbox import SandboxSkill
        return next((s for s in self.agent.skills if isinstance(s, SandboxSkill)), None)

    def get_skills(self):
        return [self._skill]

    def register_routes(self):
        self.register_route("get", "/web/{filepath:path}", self._serve_web)
        self.register_route("post", "/web-hook", self._web_hook)
        super().register_routes()

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
        await self.agent.transport.inject_message(text)
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
