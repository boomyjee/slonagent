import asyncio, json, logging
from collections import deque
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from contextvars import ContextVar
from agent import Skill, bypass
from src.transport.web import WebTransport

log = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).parent / "web"

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
    def dashboard_command(self, args: str) -> str:
        t = self.transport
        path = f"/{t.agent.id}/"
        lines = [f"🖥 http://localhost:{t._port}{path}"]
        if t._tunnel_url:
            lines.append(f"🌐 {t._tunnel_url}{path}")
        return "\n".join(lines)


class DashboardTransport(WebTransport):
    _log_handler: _LogHandler | None = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._clients: set[WebSocket] = set()
        self._buffer: deque = deque(maxlen=500)
        self._skill = DashboardSkill(self)

    def get_skills(self):
        return [self._skill]

    def set_agent(self, agent):
        super().set_agent(agent)
        self.register_route("get", "/{filename:path}", self._static)
        self.register_websocket("/ws", self._ws)

        if DashboardTransport._log_handler is None:
            handler = _LogHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s %(name)s - %(levelname)s - %(message)s"))
            logging.getLogger().addHandler(handler)
            DashboardTransport._log_handler = handler

        _LogHandler._instances[agent.id] = self
        agent_context.set(agent.id)

    _NO_CACHE = {"Cache-Control": "no-store"}
    _MIME = {"js": "application/javascript", "css": "text/css", "html": "text/html"}

    async def _static(self, filename: str = "index.html"):
        path = _WEB_DIR / (filename or "index.html")
        if not path.is_file() or not path.resolve().is_relative_to(_WEB_DIR.resolve()):
            return PlainTextResponse("Not found", status_code=404)
        mime = self._MIME.get(path.suffix.lstrip("."), "text/plain")
        return PlainTextResponse(path.read_text(encoding="utf-8"), media_type=mime, headers=self._NO_CACHE)

    async def _ws(self, ws: WebSocket):
        await ws.accept()
        for event in list(self._buffer):
            await ws.send_text(json.dumps(event, ensure_ascii=False))
        self._clients.add(ws)
        try:
            while True:
                data = await ws.receive_text()
                try:
                    msg = json.loads(data)
                except json.JSONDecodeError:
                    log.warning("ws: invalid JSON: %s", data[:200])
                    continue
                if msg.get("type") == "transport" and msg.get("method") == "process_message":
                    await self.process_message(
                        content_parts=msg.get("content_parts", []),
                        user_message_id=msg.get("user_message_id"),
                        trigger_answer=msg.get("trigger_answer", True),
                    )
                else:
                    log.warning("ws: unknown message: %s", msg)
        except WebSocketDisconnect:
            pass
        finally:
            self._clients.discard(ws)

    async def send(self, event: dict):
        self._buffer.append(event)
        if not self._clients:
            return
        data = json.dumps(event, ensure_ascii=False)
        dead = set()
        for ws in list(self._clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    # --- BaseTransport interface ---

    async def _transport_event(self, method: str, **kwargs):
        await self.send({"type": "transport", "method": method, **kwargs})

    async def send_message(self, text: str, stream_id=None, final: bool = True):
        await self._transport_event("send_message", text=text, stream_id=stream_id, final=final)

    async def send_thinking(self, text: str, stream_id=None, final: bool = False):
        await self._transport_event("send_thinking", text=text, stream_id=stream_id, final=final)

    async def send_system_prompt(self, text: str):
        await self._transport_event("send_system_prompt", text=text)

    async def on_tool_call(self, name: str, args: dict):
        await self._transport_event("on_tool_call", name=name, args={k: str(v) for k, v in args.items()})

    async def on_tool_result(self, name: str, result):
        await self._transport_event("on_tool_result", name=name, result=result)

    async def send_processing(self, active: bool):
        await self._transport_event("send_processing", active=active)

    async def inject_message(self, text: str):
        await self._transport_event("inject_message", text=text)
