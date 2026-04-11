import asyncio, logging
from contextvars import ContextVar

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
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(asyncio.ensure_future, transport.send(event))
        except Exception:
            self.handleError(record)


class DashboardSkill(Skill):
    def __init__(self, transport: "DashboardTransport"):
        self.transport = transport
        super().__init__()

    @bypass("dashboard", "Ссылка на веб-дашборд", standalone=True)
    async def dashboard_command(self, args: str) -> str:
        return f"🖥 {await self.transport.get_url('/')}"


class DashboardTransport(WebTransport):
    """Full agent dashboard: chat + logs tab, mounted at /{agent_id}/dashboard/."""

    _log_handler: _LogHandler | None = None

    def __init__(self):
        super().__init__(prefix="/dashboard")
        self._skill = DashboardSkill(self)

    def get_skills(self):
        return [self._skill]

    def set_agent(self, agent):
        super().set_agent(agent)

        if DashboardTransport._log_handler is None:
            handler = _LogHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s %(name)s - %(levelname)s - %(message)s"))
            logging.getLogger().addHandler(handler)
            DashboardTransport._log_handler = handler

        _LogHandler._instances[agent.id] = self
        agent_context.set(agent.id)
