"""Singleton-ish HTTP-сервер для WebTransport: один FastAPI/uvicorn на процесс,
общий auth-middleware, опциональный sish-tunnel.

Конфигурируется через `WebTransportServer.start(config)` из main.py до создания
любых WebTransport-инстансов. Дальше первый WebTransport.set_agent через
`WebTransportServer.ensure_server()` лениво поднимает uvicorn.

WebTransport-инстансы регистрируют свои URL через `register_route` (с refcount
по url, чтоб одни и те же fork-уровневые роуты делил несколько инстансов
одного форка)."""

import asyncio, base64, contextlib, hashlib, logging, os, sys
from datetime import date, timedelta

from fastapi import FastAPI, Request
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
    app: FastAPI | None = None
    port: int = 8765
    password_hash: str = ""
    make_agent = None

    _tunnel_url: str | None = None
    _tunnel_ready: asyncio.Event | None = None

    @classmethod
    def _make_auth_token(cls, day: date = None) -> str:
        d = day or date.today()
        return hashlib.sha256(f"{d.isoformat()}{cls.password_hash}".encode()).hexdigest()[:16]

    @classmethod
    def _check_auth_token(cls, token: str) -> bool:
        today = date.today()
        return token in (cls._make_auth_token(today), cls._make_auth_token(today - timedelta(days=1)))

    @classmethod
    async def get_url(cls, path: str = "", force_localhost: bool = False) -> str:
        """Полный URL: host (tunnel или localhost) + path. Ждёт готовности
        туннеля если он есть."""
        if not force_localhost and cls._tunnel_ready is not None:
            await cls._tunnel_ready.wait()
        if not force_localhost and cls._tunnel_url:
            return f"{cls._tunnel_url}{path}"
        return f"http://localhost:{cls.port}{path}"

    @classmethod
    async def get_auth_url(cls, path: str = "") -> str:
        """Как get_url, но дописывает ?token=... если в конфиге задан пароль —
        для шеринга в Telegram WebApp итд."""
        url = await cls.get_url(path)
        if cls.password_hash:
            sep = "&" if "?" in url else "?"
            url += f"{sep}token={cls._make_auth_token()}"
        return url

    @classmethod
    def start(cls, config: dict, make_agent=None):
        """Поднимает uvicorn-сервер процесса. Должен вызываться один раз из
        main.py до создания WebTransport-инстансов. `make_agent` —
        опциональная фабрика тред-агентов: `(agent_id, thread_id) -> Agent`."""
        if make_agent is not None:
            cls.make_agent = staticmethod(make_agent)
        if cls.app is not None:
            return
        cls.port = config.get("port", 8765)
        cls.password_hash = config.get("password_hash", "")
        sish_domain = config.get("sish_domain", "")
        sish_port = config.get("sish_port", 2222)
        sish_key = config.get("sish_key", "")
        loop = asyncio.get_running_loop()
        cls.app = FastAPI()

        _REALM = "SlonAgent"
        _401 = Response(status_code=401, headers={"WWW-Authenticate": f'Basic realm="{_REALM}"'})
        _NO_PASSWORD = PlainTextResponse(
            "No password configured. Set password_hash in config.", status_code=503,
        )

        @cls.app.middleware("http")
        async def auth_middleware(request: Request, call_next):
            # Local access never needs auth (direct browser on the host).
            if request.url.hostname in ("localhost", "127.0.0.1"):
                return await call_next(request)
            if not cls.password_hash:
                return _NO_PASSWORD
            # Static assets that can't carry credentials are public:
            # - .js: bookmarklets import scripts cross-origin (no cookie).
            # - manifest.json / icons: <link rel="manifest"> fetches without
            #   credentials by default, blocking PWA install behind auth.
            # The actual gate for sensitive data is the WebSocket.
            if request.url.path.endswith((".js", ".json", ".svg", ".png", ".ico")):
                return await call_next(request)
            # Cookie from a previous successful auth.
            if request.cookies.get("auth") == cls.password_hash:
                return await call_next(request)
            # Daily token in query string (for Telegram WebApp etc).
            token = request.query_params.get("token", "")
            if token and cls._check_auth_token(token):
                response = await call_next(request)
                response.set_cookie(
                    "auth", cls.password_hash,
                    max_age=30 * 24 * 3600,
                    httponly=True, secure=True, samesite="none", path="/",
                )
                return response
            auth = request.headers.get("authorization", "")
            if auth.startswith("Basic "):
                try:
                    decoded = base64.b64decode(auth[6:]).decode()
                    password = decoded.split(":", 1)[1]
                    if hashlib.sha256(password.encode()).hexdigest() == cls.password_hash:
                        response = await call_next(request)
                        response.set_cookie(
                            "auth", cls.password_hash,
                            max_age=30 * 24 * 3600,
                            httponly=True, secure=True, samesite="none", path="/",
                        )
                        return response
                except Exception:
                    pass
            return _401

        @cls.app.get("/")
        async def root():
            return RedirectResponse("/main/dashboard/")

        async def _run():
            import uvicorn
            uv_config = uvicorn.Config(
                cls.app,
                host="0.0.0.0",
                port=cls.port,
                ws="wsproto",
                log_config=None,
            )
            server = uvicorn.Server(uv_config)
            server.install_signal_handlers = lambda: None
            server.capture_signals = lambda: contextlib.nullcontext()
            log.info("WebTransportServer: http://localhost:%d", cls.port)
            await server.serve()

        logging.getLogger("asyncssh").setLevel(logging.WARNING)
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

        loop.call_soon_threadsafe(lambda: asyncio.create_task(_run()))

        if sish_domain:
            import uuid
            # Stable per-(machine, entry script) subdomain so bookmarklets
            # survive restarts, but two checkouts on the same box don't
            # collide on the same tunnel hostname.
            key = f"{uuid.getnode()}:{os.path.abspath(sys.argv[0])}"
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
                            cls.port, subdomain, sish_domain, sish_port, sish_key,
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
