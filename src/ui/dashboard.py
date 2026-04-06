import asyncio
import contextlib
import json
import logging
import webbrowser
from collections import deque
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

agent_context: ContextVar[str] = ContextVar("agent_id", default="main")

_HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
_BUFFER_SIZE = 500
_LOG_FORMAT = "%(asctime)s [%(agent_id)s] %(name)s - %(levelname)s - %(message)s"


class UILogHandler(logging.Handler):
    def __init__(self, dashboard, level: int = logging.DEBUG) -> None:
        super().__init__(level)
        self._dashboard = dashboard

    def _category(self, name: str) -> str:
        if name.startswith("src.memory") or name.startswith("memory"):
            return "memory"
        if name.startswith("aiogram") or name.startswith("src.transport") or name.startswith("httpx") or name.startswith("uvicorn") or name.startswith("google_genai") or name.startswith("sentence_transformers") or name.startswith("huggingface_hub"):
            return "transport"
        return "agent"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            category = self._category(record.name)
            record.agent_id = agent_context.get()
            text = self.format(record)
            self._dashboard.add_log(category, record.levelname, text, agent_id=record.agent_id)
        except Exception:
            self.handleError(record)


class Dashboard:
    def __init__(self, port: int = 8765):
        self._port = port
        self._queue: asyncio.Queue = None
        self._loop: asyncio.AbstractEventLoop = None
        self._incoming: asyncio.Queue = asyncio.Queue()
        self._clients: set = set()
        self._buffer: deque = deque(maxlen=_BUFFER_SIZE)
        self._transports: dict[str, object] = {}

        root = logging.getLogger()
        root.setLevel(logging.INFO)
        ui_handler = UILogHandler(self)
        ui_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(ui_handler)
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
        root.addHandler(console_handler)

    async def start(self) -> None:
        asyncio.create_task(self.run_async())

    def wrap(self, transport_class):
        from src.ui.wrapper import UITransportWrapper
        return UITransportWrapper(transport_class, self)


    def register_transport(self, agent_id: str, transport) -> None:
        self._transports[agent_id] = transport

    async def _dispatch_web_chat(self) -> None:
        while True:
            item = await self._incoming.get()
            agent_id, text = item["agent_id"], item["text"]
            transport = self._transports.get(agent_id)
            if transport:
                self.add_chat("user", text, agent_id=agent_id)
                await transport.inject_message(text)
                await transport.process_message(content_parts=[{"type": "text", "text": text}])

    def _emit(self, event: dict) -> None:
        event["ts"] = datetime.now().strftime("%H:%M:%S")
        self._buffer.append(event)
        if self._queue is None or self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, event)
        except RuntimeError:
            pass

    def add_chat(self, role: str, text: str, chat_id=None, agent_id: str = "main") -> None:
        self._emit({"type": "chat", "role": role, "text": text, "chat_id": chat_id, "agent_id": agent_id})

    def add_collapsible(self, title: str, text: str, collapsible_id=None, agent_id: str = "main") -> None:
        self._emit({"type": "collapsible", "title": title, "text": text, "collapsible_id": collapsible_id, "agent_id": agent_id})

    def add_log(self, category: str, level: str, text: str, agent_id: str = "main") -> None:
        self._emit({"type": "log", "category": category, "level": level, "text": text, "agent_id": agent_id})

    async def _broadcaster(self) -> None:
        while True:
            event = await self._queue.get()
            if not self._clients:
                continue
            data = json.dumps(event, ensure_ascii=False)
            dead = set()
            for client in list(self._clients):
                try:
                    await client.send_text(data)
                except Exception:
                    dead.add(client)
            self._clients -= dead

    async def run_async(self) -> None:
        import uvicorn

        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=2000)
        asyncio.create_task(self._broadcaster())
        asyncio.create_task(self._dispatch_web_chat())

        app = FastAPI()

        @app.get("/")
        async def index():
            return HTMLResponse(_HTML)

        @app.websocket("/ws")
        async def ws(websocket: WebSocket):
            await websocket.accept()
            for event in self._buffer:
                await websocket.send_text(json.dumps(event, ensure_ascii=False))
            self._clients.add(websocket)
            try:
                while True:
                    data = await websocket.receive_text()
                    try:
                        msg = json.loads(data)
                        if msg.get("type") == "user_message" and msg.get("text", "").strip():
                            await self._incoming.put({"agent_id": msg.get("agent_id", "main"), "text": msg["text"].strip()})
                        elif msg.get("type") == "recall_query" and msg.get("text", "").strip():
                            query = msg["text"].strip()
                            agent_id = msg.get("agent_id", "main")
                            try:
                                from src.memory.providers.fact import FactProvider
                                transport = self._transports.get(agent_id)
                                provider = next((p for p in transport.agent.memory.providers if isinstance(p, FactProvider)), None) if transport and transport.agent else None
                                result = (await provider._recall_text(query, query_label="$DASHBOARD")) if provider else "Провайдер памяти не найден."
                            except Exception as e:
                                logging.warning("[recall] ошибка при выполнении запроса %r", query, exc_info=True)
                                result = f"Ошибка: {e}"
                            await websocket.send_text(json.dumps({"type": "recall_result", "query": query, "text": result or "Ничего не найдено.", "ts": datetime.now().strftime("%H:%M:%S"), "agent_id": agent_id}, ensure_ascii=False))
                    except Exception:
                        pass
            except WebSocketDisconnect:
                pass
            finally:
                self._clients.discard(websocket)

        async def open_browser():
            await asyncio.sleep(1.0)
            webbrowser.open(f"http://localhost:{self._port}")

        asyncio.create_task(open_browser())

        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=self._port,
            ws="wsproto",
            log_config=None,
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None
        server.capture_signals = lambda: contextlib.nullcontext()
        await server.serve()
