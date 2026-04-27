import asyncio, base64, contextlib, hashlib, inspect, json, logging
from collections import deque
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from src.transport.base import BaseTransport

log = logging.getLogger(__name__)


async def start_tunnel(port: int, subdomain: str, sish_domain: str, sish_port: int, sish_key: str):
    """Start SSH tunnel via sish using asyncssh. Returns (public_url, connection)."""
    import asyncssh
    key = asyncssh.import_private_key(sish_key)
    conn = await asyncssh.connect(
        sish_domain, sish_port, known_hosts=None, client_keys=[key], username="tunnel",
        keepalive_interval=15, keepalive_count_max=4,
    )
    # Force IPv4 — passing "localhost" makes asyncssh try ::1 first; if
    # uvicorn binds to 0.0.0.0 (v4 only), the v6 connect hangs for ~2s
    # before falling back, which is the exact cold-start TTFB we kept
    # papering over with HTTP-level keepalives.
    await conn.forward_remote_port(subdomain, 80, "127.0.0.1", port)
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
    # Loop on which the uvicorn server lives. Captured at set_server_config()
    # so _ensure_server can schedule its server/tunnel tasks here regardless
    # of which thread first triggers it (e.g. an RPC worker calling into a
    # sandbox-bridge factory).
    _loop: asyncio.AbstractEventLoop | None = None

    @staticmethod
    def make_auth_token(day: date = None) -> str:
        d = day or date.today()
        return hashlib.sha256(f"{d.isoformat()}{WebTransport._password_hash}".encode()).hexdigest()[:16]

    @staticmethod
    def check_auth_token(token: str) -> bool:
        today = date.today()
        return token in (WebTransport.make_auth_token(today), WebTransport.make_auth_token(today - timedelta(days=1)))

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
        self._replay_transport: deque = deque(maxlen=100)
        self._replay_other: deque = deque(maxlen=400)
        self._routes: list = []
        self._mount_id: str | None = None
        # Monotonic id stamped on every outgoing event. Clients track the
        # highest id they've seen and ask for "give me everything since X"
        # via {type:"replay", last_seen_id: X} on ws.open — that way a
        # mobile reconnect into a still-alive page doesn't redraw history.
        self._message_id_counter: int = 0

    @staticmethod
    def set_server_config(
        port: int = 8765,
        sish_domain: str = "",
        sish_port: int = 2222,
        sish_key: str = "",
        password_hash: str = "",
        **_ignored,
    ):
        """Configure the shared uvicorn server. Call once from main.py before
        constructing any web transports. Can be invoked via any subclass."""
        WebTransport._port = port
        WebTransport._sish_domain = sish_domain
        WebTransport._sish_port = sish_port
        WebTransport._sish_key = sish_key
        WebTransport._password_hash = password_hash
        WebTransport._loop = asyncio.get_running_loop()

    async def get_auth_url(self, sub_path: str = "") -> str:
        """URL with a daily auth token appended (for sharing in Telegram etc)."""
        url = await self.get_url(sub_path)
        if self._password_hash:
            sep = "&" if "?" in url else "?"
            url += f"{sep}token={self.make_auth_token()}"
        return url

    async def get_url(self, sub_path: str = "", force_localhost: bool = False) -> str:
        """Return a fully-qualified URL inside this transport's namespace
        (`/{agent_id}{prefix}{sub_path}`). Uses the tunnel URL if one is
        available; pass `force_localhost=True` to skip it. Awaits tunnel
        startup if it's still in progress."""
        if self._mount_id is None:
            raise RuntimeError("get_url called before set_agent (or transport has _mount=False and no URL)")
        path = f"/{self._mount_id}{self._prefix}{sub_path}"
        if not force_localhost and WebTransport._tunnel_ready is not None:
            await WebTransport._tunnel_ready.wait()
        if not force_localhost and WebTransport._tunnel_url:
            return f"{WebTransport._tunnel_url}{path}"
        return f"http://localhost:{WebTransport._port}{path}"

    @staticmethod
    def _ensure_server():
        # May be called from any thread (e.g. an RPC worker building a
        # sandbox-bridge transport). All loop-bound operations are scheduled
        # onto WebTransport._loop, captured at set_server_config(), so this
        # function has no thread affinity of its own.
        if WebTransport._app is not None:
            return
        if WebTransport._loop is None:
            raise RuntimeError("WebTransport._loop not set; call set_server_config() from the main loop first")
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
                # Cookie from a previous successful auth.
                if request.cookies.get("auth") == WebTransport._password_hash:
                    return await call_next(request)
                # Daily token in query string (for Telegram WebApp etc).
                token = request.query_params.get("token", "")
                if token and WebTransport.check_auth_token(token):
                    response = await call_next(request)
                    response.set_cookie(
                        "auth", WebTransport._password_hash,
                        max_age=30 * 24 * 3600,
                        httponly=True, secure=True, samesite="none", path="/",
                    )
                    return response
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

        # asyncssh logs connection lifecycle (keepalive timeouts, server
        # disconnect reasons) at INFO. Keeping it on so when the tunnel
        # dies we can see why instead of guessing.
        logging.getLogger("asyncssh").setLevel(logging.INFO)
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

        def _spawn_server():
            WebTransport._server_task = asyncio.create_task(_run())
        WebTransport._loop.call_soon_threadsafe(_spawn_server)

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
                    WebTransport._tunnel_ready.set()
                    return
                WebTransport._tunnel_ready.set()
                # Watch for the SSH conn dying so we don't silently keep
                # serving a dead URL. wait_closed returns when asyncssh tears
                # the conn down (keepalive timeout, server disconnect, etc).
                try:
                    await WebTransport._tunnel_conn.wait_closed()
                except Exception as e:
                    log.warning("Tunnel watcher error: %s", e)
                log.warning("Tunnel closed: was %s", WebTransport._tunnel_url)
                WebTransport._tunnel_url = None
                WebTransport._tunnel_conn = None
            WebTransport._loop.call_soon_threadsafe(asyncio.create_task, _tunnel())

    def register_route(self, method, path, handler):
        url = f"/{self.agent.id}{self._prefix}{path}"
        getattr(self._app, method)(url)(handler)
        self._routes.append(self._app.router.routes[-1])

    def register_json_route(self, method, path, handler):
        """Register a handler with contract (query, body, path_params) -> dict|list."""
        async def wrapped(request: Request):
            body = None
            if request.method in ("POST", "PUT", "PATCH"):
                try: body = await request.json()
                except Exception: pass
            result = await handler(dict(request.query_params),body,dict(request.path_params))
            if isinstance(result, str):
                return PlainTextResponse(result)
            return JSONResponse(result)
        wrapped.__name__ = f"json_{handler.__name__ if hasattr(handler, '__name__') else id(handler)}"
        self.register_route(method, path, wrapped)

    def set_agent(self, agent):
        super().set_agent(agent)
        if not self._mount or self._routes: return
        self._mount_id = agent.id
        self._ensure_server()
        self.register_routes()

    def register_routes(self):
        self.register_route("websocket", "/ws", self._ws)
        self.register_route("get", "/{filename:path}", self._static)

    def remove_routes(self):
        for r in self._routes:
            self._app.router.routes.remove(r)
        self._routes = []

    def cleanup(self):
        self.remove_routes()

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
        # No auto-replay — clients ask for it explicitly via a "replay"
        # message after they connect (so reconnects on a live page can
        # specify last_seen_id and skip what they already have).
        self._clients.add(ws)
        try:
            while True:
                data = await ws.receive_text()
                try:
                    msg = json.loads(data)
                except json.JSONDecodeError:
                    log.warning("ws: invalid JSON: %s", data[:200])
                    continue
                await self.ws_handle_message(msg, ws)
        except WebSocketDisconnect:
            pass
        finally:
            self._clients.discard(ws)
            self.on_ws_close(ws)

    def on_ws_close(self, ws):
        """Hook for subclasses to clean up per-connection state. Base no-op."""
        pass

    async def ws_handle_message(self, msg: dict, ws=None):
        if msg.get("type") == "replay" and ws is not None:
            last_seen = msg.get("last_seen_id", -1)
            import heapq
            stream = heapq.merge(self._replay_transport, self._replay_other,key=lambda e: e.get("id", 0))
            for event in stream:
                if event.get("id", 0) > last_seen:
                    await ws.send_text(json.dumps(event, ensure_ascii=False))
            return
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
        self._message_id_counter += 1
        event = {**event, "id": self._message_id_counter}
        if replay:
            buf = self._replay_transport if event.get("type") == "transport" else self._replay_other
            buf.append(event)
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
        await self._transport_event("send_message", replay=final, text=text, stream_id=stream_id, final=final)

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
        await self._transport_event("send_processing", active=active)

    async def inject_message(self, text: str):
        await self._transport_event("inject_message", text=text)
