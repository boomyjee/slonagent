"""Singleton-ish HTTP-сервер для WebTransport: один FastAPI/uvicorn на процесс,
общий auth-middleware, опциональный sish-tunnel.

Конфигурируется через `WebTransportServer.start(config)` из main.py до создания
любых WebTransport-инстансов. Дальше первый WebTransport.set_agent через
`WebTransportServer.ensure_server()` лениво поднимает uvicorn.

WebTransport-инстансы регистрируют свои URL через `register_route` (с refcount
по url, чтоб одни и те же fork-уровневые роуты делил несколько инстансов
одного форка)."""

import asyncio, base64, contextlib, hashlib, hmac, logging, os, re, secrets, sys
from datetime import date, timedelta

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

log = logging.getLogger(__name__)


async def start_tunnel(port: int, subdomain: str, sish_domain: str, sish_port: int, sish_key: str):
    """Start SSH tunnel via sish using asyncssh. Returns (public_url, connection)."""
    import asyncssh
    key = asyncssh.import_private_key(sish_key)
    conn = await asyncssh.connect(
        sish_domain, sish_port, known_hosts=None, client_keys=[key], username="tunnel",
        keepalive_interval=15, keepalive_count_max=4,
    )
    # Force IPv4 — the v6 connect hangs for ~2s before fail
    await conn.forward_remote_port(subdomain, 80, "127.0.0.1", port)
    url = f"https://{subdomain}.{sish_domain}:8443"
    log.info("[tunnel] %s -> localhost:%d", url, port)
    return url, conn


class WebTransportServer:
    _app: FastAPI | None = None
    _port: int = 8765
    _password_hash: str = ""
    _tunnel_url: str | None = None
    _tunnel_ready: asyncio.Event | None = None
    _spawn_locks: dict[str, asyncio.Lock] = {}
    # agent-rpc токен: HMAC(_secret_seed, agent_id|password_hash). Минтится
    # при agent_token(), запоминается в _token_to_agent для O(1) резолва.
    _secret_seed: bytes | None = None
    _token_to_agent: dict[str, str] = {}
    _server = None  # uvicorn.Server — для тестов которым нужно остановить процесс

    @classmethod
    def agent_token(cls, agent_id: str) -> str:
        if cls._secret_seed is None:
            cls._secret_seed = secrets.token_bytes(32)
        msg = f"{agent_id}|{cls._password_hash}".encode()
        token = hmac.new(cls._secret_seed, msg, hashlib.sha256).hexdigest()[:32]
        cls._token_to_agent[token] = agent_id
        return token

    @classmethod
    def register_route(cls, method: str, url: str, handler, auth: bool = True):
        if method == "websocket" and auth:
            handler = cls._with_ws_auth(handler)
        getattr(cls._app, method)(url)(handler)
        return cls._app.router.routes[-1]

    @classmethod
    def remove_route(cls, route):
        try: cls._app.router.routes.remove(route)
        except ValueError: pass

    @classmethod
    def _with_ws_auth(cls, handler):
        async def secured(ws: WebSocket):
            # HTTP-middleware на WS-handshake не запускается — auth дублируется тут.
            # Пароль нужен всегда, включая локальный доступ: localhost-байпаса нет,
            # потому что через туннель Host/loopback подделываются.
            pw = cls._password_hash
            if not pw or ws.cookies.get("auth") != pw:
                await ws.close(code=4401)
                return
            await handler(ws)
        return secured

    @classmethod
    def _make_auth_token(cls, day: date = None) -> str:
        d = day or date.today()
        return hashlib.sha256(f"{d.isoformat()}{cls._password_hash}".encode()).hexdigest()[:16]

    @classmethod
    def _check_auth_token(cls, token: str) -> bool:
        today = date.today()
        return token in (cls._make_auth_token(today), cls._make_auth_token(today - timedelta(days=1)))

    @classmethod
    async def get_url(cls, path: str = "", force_localhost: bool = False,
                      host: str | None = None, scheme: str = "http") -> str:
        if host is not None:
            return f"{scheme}://{host}:{cls._port}{path}"
        if not force_localhost and cls._tunnel_ready is not None:
            await cls._tunnel_ready.wait()
        if not force_localhost and cls._tunnel_url:
            secure = {"http": "https", "ws": "wss"}.get(scheme, scheme)
            authority = cls._tunnel_url.split("://", 1)[1]
            return f"{secure}://{authority}{path}"
        return f"{scheme}://localhost:{cls._port}{path}"

    @classmethod
    async def get_auth_url(cls, path: str = "") -> str:
        """Как get_url, но дописывает ?token=... если в конфиге задан пароль —
        для шеринга в Telegram WebApp итд."""
        url = await cls.get_url(path)
        if cls._password_hash:
            sep = "&" if "?" in url else "?"
            url += f"{sep}token={cls._make_auth_token()}"
        return url

    @classmethod
    def start(cls, config: dict):
        """Поднимает uvicorn-сервер процесса. Должен вызываться один раз из
        main.py до создания WebTransport-инстансов."""
        if cls._app is not None:
            return
        cls._port = config.get("port", 8765)
        cls._password_hash = config.get("password_hash", "")
        sish_domain = config.get("sish_domain", "")
        sish_port = config.get("sish_port", 2222)
        sish_key = config.get("sish_key", "")
        sish_subdomain = config.get("sish_subdomain", "")
        loop = asyncio.get_running_loop()
        cls._app = FastAPI()

        _REALM = "SlonAgent"
        _401 = Response(status_code=401, headers={"WWW-Authenticate": f'Basic realm="{_REALM}"'})
        _NO_PASSWORD = PlainTextResponse(
            "No password configured. Set password_hash in config.", status_code=503,
        )

        def _lazy_spawn(app):
            async def lazy_spawn(scope, receive, send):
                if scope["type"] in ("http", "websocket"):
                    m = re.match(r"^/([^/]+)/", scope.get("path", ""))
                    if m and (agent_id := m.group(1)) != "main":
                        from agent import Agent
                        lock = cls._spawn_locks.setdefault(agent_id, asyncio.Lock())
                        async with lock:
                            try:
                                await Agent.get(agent_id, "")
                            except Exception:
                                log.exception("[lazy-spawn] failed for %s", agent_id)
                await app(scope, receive, send)
            return lazy_spawn
        cls._app.add_middleware(_lazy_spawn)

        @cls._app.middleware("http")
        async def auth_middleware(request: Request, call_next):
            if not cls._password_hash:
                return _NO_PASSWORD
            # Static assets that can't carry credentials are public:
            # - .js: bookmarklets import scripts cross-origin (no cookie).
            # - manifest.json / icons: <link rel="manifest"> fetches without
            #   credentials by default, blocking PWA install behind auth.
            # The actual gate for sensitive data is the WebSocket.
            if request.url.path.endswith((".js", ".json", ".svg", ".png", ".ico")):
                return await call_next(request)
            # Cookie from a previous successful auth.
            if request.cookies.get("auth") == cls._password_hash:
                return await call_next(request)
            # Daily token in query string (for Telegram WebApp etc).
            token = request.query_params.get("token", "")
            if token and cls._check_auth_token(token):
                response = await call_next(request)
                response.set_cookie(
                    "auth", cls._password_hash,
                    max_age=30 * 24 * 3600,
                    httponly=True, secure=True, samesite="none", path="/",
                )
                return response
            auth = request.headers.get("authorization", "")
            if auth.startswith("Basic "):
                try:
                    decoded = base64.b64decode(auth[6:]).decode()
                    password = decoded.split(":", 1)[1]
                    if hashlib.sha256(password.encode()).hexdigest() == cls._password_hash:
                        response = await call_next(request)
                        response.set_cookie(
                            "auth", cls._password_hash,
                            max_age=30 * 24 * 3600,
                            httponly=True, secure=True, samesite="none", path="/",
                        )
                        return response
                except Exception:
                    pass
            return _401

        @cls._app.get("/")
        async def root():
            return RedirectResponse("/main/dashboard/")

        # /agent-rpc/{token} — единственный agent-rpc эндпоинт на процесс.
        from src.transport.web.agent_rpc import handle_agent_rpc

        @cls._app.websocket("/agent-rpc/{token}")
        async def _agent_rpc(ws: WebSocket, token: str):
            agent_id = cls._token_to_agent.get(token)
            if agent_id is None:
                await ws.close(code=4401)
                return
            await handle_agent_rpc(agent_id, ws)

        async def _run():
            import uvicorn
            uv_config = uvicorn.Config(
                cls._app,
                host="0.0.0.0",
                port=cls._port,
                ws="wsproto",
                log_config=None,
            )
            server = uvicorn.Server(uv_config)
            server.install_signal_handlers = lambda: None
            server.capture_signals = lambda: contextlib.nullcontext()
            cls._server = server
            log.info("WebTransportServer: http://localhost:%d", cls._port)
            await server.serve()

        logging.getLogger("asyncssh").setLevel(logging.WARNING)
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

        loop.call_soon_threadsafe(lambda: asyncio.create_task(_run()))

        if sish_domain:
            if sish_subdomain:
                subdomain = sish_subdomain
            else:
                # platform.node() (hostname), а не uuid.getnode() (MAC) — последний
                # на машине с кучей виртуальных адаптеров меняется между ребутами.
                import platform
                key = f"{platform.node()}:{os.path.abspath(sys.argv[0])}"
                subdomain = "web-" + hashlib.sha1(key.encode()).hexdigest()[:6]
            cls._tunnel_ready = asyncio.Event()
            async def _tunnel():
                # Ретраи только на стартовых ошибках. Успешный коннект (даже если
                # сразу разорвался) сбрасывает счётчик, поэтому сетевые мерцания
                # не выжирают бюджет, а реальная поломка (sish мёртв, auth не
                # пускает) останавливается на 5-й подряд неудачной попытке.
                fails, MAX_FAILS = 0, 5
                tunnel_conn = None
                while True:
                    try:
                        url, tunnel_conn = await start_tunnel(
                            cls._port, subdomain, sish_domain, sish_port, sish_key,
                        )
                        cls._tunnel_url = url
                        fails = 0
                    except Exception as e:
                        fails += 1
                        log.warning("Tunnel start failed (%d/%d): %s", fails, MAX_FAILS, e)
                        cls._tunnel_ready.set()
                        if fails >= MAX_FAILS:
                            log.warning("Giving up on tunnel after %d consecutive failures", MAX_FAILS)
                            return
                        await asyncio.sleep(5)
                        continue
                    cls._tunnel_ready.set()
                    try:
                        await tunnel_conn.wait_closed()
                    except Exception as e:
                        log.warning("Tunnel watcher error: %s", e)
                    log.warning("Tunnel closed: was %s", cls._tunnel_url)
                    cls._tunnel_url = None
                    await asyncio.sleep(5)
            loop.call_soon_threadsafe(asyncio.create_task, _tunnel())
