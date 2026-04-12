import asyncio, base64, contextlib, hashlib, inspect, json, logging
from collections import deque
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from src.transport.base import BaseTransport

log = logging.getLogger(__name__)


async def start_tunnel(port: int, subdomain: str, sish_domain: str, sish_port: int, sish_key: str):
    """Start SSH tunnel via sish using asyncssh. Returns (public_url, connection)."""
    import asyncssh
    key = asyncssh.import_private_key(sish_key)
    conn = await asyncssh.connect(
        sish_domain, sish_port, known_hosts=None, client_keys=[key], username="tunnel",
    )
    await conn.forward_remote_port(subdomain, 80, "localhost", port)
    url = f"https://{subdomain}.{sish_domain}:8443"
    log.info("[tunnel] %s -> localhost:%d", url, port)
    return url, conn


class WebTransport(BaseTransport):
    """Base for transports that serve a chat UI over a shared HTTP server.

    Class-level: lazily starts one uvicorn server per process.

    Instance-level: each subclass gets, under /{agent_id}{prefix}/ —
    - static files with cascading lookup: subclass `ui/` first, then
      `WebTransport/ui/` (shared lib.js, Chat.js, etc.),
    - a WebSocket endpoint at `/ws` speaking the chat wire protocol,
    - buffered event replay on new client connections.

    Concrete subclasses pass `prefix` (empty for root mount). Anything
    specific to the subclass (extra skills, log handlers, additional routes)
    goes in the subclass's own `set_agent`.
    """

    _app: FastAPI | None = None
    _server_task: asyncio.Task | None = None
    _port: int = 8765
    _tunnel_conn = None
    _tunnel_url: str | None = None
    _tunnel_ready: asyncio.Event | None = None
    _sish_domain: str = ""
    _sish_port: int = 2222
    _sish_key: str = ""
    _password_hash: str = ""

    # Subclasses bound to a pre-accepted WebSocket (e.g. PageTransport, which
    # WebAgentTransport hands a ws on each new bookmarklet connection) set this
    # to False to skip HTTP-route registration in set_agent.
    _mount: bool = True

    _MIME = {"js": "application/javascript", "css": "text/css", "html": "text/html"}
    _STATIC_HEADERS = {
        "Cache-Control": "no-store",
        # Needed when a bookmarklet loads run.js cross-origin into a third-party page.
        "Access-Control-Allow-Origin": "*",
    }

    def __init__(self, prefix: str = "", verbose: bool = True):
        super().__init__()
        self._prefix = prefix
        self.verbose = verbose
        self._clients: set[WebSocket] = set()
        self._replay_buffer: deque = deque(maxlen=500)
        self._routes: list = []

    @staticmethod
    def set_server_config(
        port: int = 8765,
        sish_domain: str = "",
        sish_port: int = 2222,
        sish_key: str = "",
        password_hash: str = "",
    ):
        """Configure the shared uvicorn server. Call once from main.py before
        constructing any web transports. Can be invoked via any subclass."""
        WebTransport._port = port
        WebTransport._sish_domain = sish_domain
        WebTransport._sish_port = sish_port
        WebTransport._sish_key = sish_key
        WebTransport._password_hash = password_hash

    async def get_url(self, sub_path: str = "", force_localhost: bool = False) -> str:
        """Return a fully-qualified URL inside this transport's namespace
        (`/{agent_id}{prefix}{sub_path}`). Uses the tunnel URL if one is
        available; pass `force_localhost=True` to skip it. Awaits tunnel
        startup if it's still in progress."""
        path = f"/{self.agent.id}{self._prefix}{sub_path}"
        if not force_localhost and WebTransport._tunnel_ready is not None:
            await WebTransport._tunnel_ready.wait()
        if not force_localhost and WebTransport._tunnel_url:
            return f"{WebTransport._tunnel_url}{path}"
        return f"http://localhost:{WebTransport._port}{path}"

    @staticmethod
    def _ensure_server():
        if WebTransport._app is not None:
            return
        WebTransport._app = FastAPI()

        if WebTransport._password_hash:
            _REALM = "SlonAgent"
            _401 = Response(status_code=401, headers={"WWW-Authenticate": f'Basic realm="{_REALM}"'})

            @WebTransport._app.middleware("http")
            async def auth_middleware(request: Request, call_next):
                # Local access never needs auth (direct browser on the host).
                if request.url.hostname in ("localhost", "127.0.0.1"):
                    return await call_next(request)
                # JS is always public — bookmarklets import scripts cross-origin
                # and can't carry credentials. The actual gate is the WebSocket.
                if request.url.path.endswith(".js"):
                    return await call_next(request)
                # Cookie from a previous successful Basic prompt.
                if request.cookies.get("auth") == WebTransport._password_hash:
                    return await call_next(request)
                auth = request.headers.get("authorization", "")
                if auth.startswith("Basic "):
                    try:
                        decoded = base64.b64decode(auth[6:]).decode()
                        password = decoded.split(":", 1)[1]
                        if hashlib.sha256(password.encode()).hexdigest() == WebTransport._password_hash:
                            response = await call_next(request)
                            # SameSite=None + Secure so the cookie rides along
                            # cross-origin WebSocket handshakes from bookmarklets.
                            response.set_cookie(
                                "auth", WebTransport._password_hash,
                                max_age=30 * 24 * 3600,
                                httponly=True, secure=True, samesite="none", path="/",
                            )
                            return response
                    except Exception:
                        pass
                return _401

        @WebTransport._app.get("/")
        async def root():
            return RedirectResponse("/main/dashboard/")

        async def _run():
            import uvicorn
            uv_config = uvicorn.Config(
                WebTransport._app,
                host="0.0.0.0",
                port=WebTransport._port,
                ws="wsproto",
                log_config=None,
            )
            server = uvicorn.Server(uv_config)
            server.install_signal_handlers = lambda: None
            server.capture_signals = lambda: contextlib.nullcontext()
            log.info("WebTransport server: http://localhost:%d", WebTransport._port)
            await server.serve()

        logging.getLogger("asyncssh").setLevel(logging.WARNING)
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
        WebTransport._server_task = asyncio.create_task(_run())

        if WebTransport._sish_domain:
            import uuid, sys, os
            # Stable per-(machine, entry script) subdomain so bookmarklets
            # survive restarts, but two checkouts on the same box don't
            # collide on the same tunnel hostname.
            key = f"{uuid.getnode()}:{os.path.abspath(sys.argv[0])}"
            subdomain = "web-" + hashlib.sha1(key.encode()).hexdigest()[:6]
            WebTransport._tunnel_ready = asyncio.Event()
            async def _tunnel():
                try:
                    url, WebTransport._tunnel_conn = await start_tunnel(
                        WebTransport._port, subdomain, WebTransport._sish_domain,
                        WebTransport._sish_port, WebTransport._sish_key,
                    )
                    WebTransport._tunnel_url = url
                except Exception as e:
                    log.warning("Tunnel failed: %s", e)
                finally:
                    WebTransport._tunnel_ready.set()
            asyncio.create_task(_tunnel())

    def set_agent(self, agent):
        super().set_agent(agent)
        if not self._mount:
            return
        self._ensure_server()
        base = f"/{agent.id}{self._prefix}"
        self._app.websocket(f"{base}/ws")(self._ws)
        self._app.get(f"{base}/{{filename:path}}")(self._static)
        self._routes = self._app.router.routes[-2:]

    def remove_routes(self):
        for r in self._routes:
            self._app.router.routes.remove(r)
        self._routes = []

    # --- static serving ---

    _BASE_UI = Path(__file__).resolve().parent / "ui"

    @property
    def _ui_dirs(self) -> list[Path]:
        """Directories to search for static files, most-specific first.

        Subclass's `ui/` (next to the subclass source file) is checked first;
        `WebTransport/ui/` is the fallback with shared components (lib.js,
        Chat.js, etc.). Subclasses override individual files by placing them
        in their own `ui/` directory.
        """
        own = Path(inspect.getfile(type(self))).resolve().parent / "ui"
        dirs = []
        if own != self._BASE_UI and own.is_dir():
            dirs.append(own)
        dirs.append(self._BASE_UI)
        return dirs

    async def _static(self, filename: str = "index.html"):
        filename = filename or "index.html"
        for ui_dir in self._ui_dirs:
            path = ui_dir / filename
            if path.is_file() and path.resolve().is_relative_to(ui_dir.resolve()):
                mime = self._MIME.get(path.suffix.lstrip("."), "text/plain")
                return PlainTextResponse(
                    path.read_text(encoding="utf-8"),
                    media_type=mime,
                    headers=self._STATIC_HEADERS,
                )
        return PlainTextResponse("Not found", status_code=404)
    
    async def _ws(self, ws: WebSocket):
        # HTTP middleware doesn't run on WebSocket handshakes — enforce auth here.
        if WebTransport._password_hash:
            host = ws.headers.get("host", "").split(":")[0]
            if host not in ("localhost", "127.0.0.1") and \
                    ws.cookies.get("auth") != WebTransport._password_hash:
                await ws.close(code=4401)
                return
        await ws.accept()
        await self.ws_connect(ws)    

    async def ws_connect(self, ws: WebSocket):
        for event in list(self._replay_buffer):
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
                await self.ws_handle_message(msg)
        except WebSocketDisconnect:
            pass
        finally:
            self._clients.discard(ws)

    async def ws_handle_message(self, msg: dict):
        if msg.get("type") == "transport" and msg.get("method") == "process_message":
            # Echo back through send() so it lands in the buffer and gets
            # replayed on reconnect. Chat.js no longer adds user messages
            # to local state — it renders them when this event comes in.
            await self.send(msg, replay=True)
            await self.process_message(
                content_parts=msg.get("content_parts", []),
                user_message_id=msg.get("user_message_id"),
                trigger_answer=msg.get("trigger_answer", True),
            )

    async def send(self, event: dict, replay=False):
        if replay: self._replay_buffer.append(event)
        if not self._clients: return
        data = json.dumps(event, ensure_ascii=False)
        dead = set()
        for ws in list(self._clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    # --- BaseTransport interface ---

    async def _transport_event(self, method: str, replay=True, **kwargs):
        await self.send({"type": "transport", "method": method, **kwargs}, replay=replay)

    async def send_message(self, text: str, stream_id=None, final: bool = True):
        await self._transport_event("send_message", text=text, stream_id=stream_id, final=final)

    async def send_thinking(self, text: str, stream_id=None, final: bool = False):
        await self._transport_event("send_thinking", replay=final, text=text, stream_id=stream_id, final=final)

    async def send_system_prompt(self, text: str):
        if not self.verbose: return
        await self._transport_event("send_system_prompt", text=text)

    async def on_tool_call(self, name: str, args: dict):
        await self._transport_event("on_tool_call", name=name, args={k: str(v) for k, v in args.items()})

    async def on_tool_result(self, name: str, result):
        await self._transport_event("on_tool_result", name=name, result=result)

    async def send_processing(self, active: bool):
        await self._transport_event("send_processing", replay=False, active=active)

    async def inject_message(self, text: str):
        await self._transport_event("inject_message", text=text)
