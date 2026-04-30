"""End-to-end dashboard tests against a real sandbox container.

Covers /web/ static serving, /web/*.py CGI-style execution via the in-container
sandbox-proxy worker, and /sandbox/{port}/... TCP forwarding through the same
tunnel. Single module-scoped fixture sets up uvicorn + agent + container; tests
share state but use unique filenames to avoid collisions.
"""
import asyncio
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent import Agent, Skill
from src.skills.config import ConfigSkill
from src.skills.sandbox import SandboxSkill
from src.transport.dashboard import DashboardTransport
from src.transport.web import WebTransport


pytestmark = pytest.mark.integration


def _podman_available() -> bool:
    try:
        r = subprocess.run(["podman", "version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


if not _podman_available():
    pytest.skip("podman not available", allow_module_level=True)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class PassthroughCompressor(Skill):
    async def compress(self, turns): return turns


_SHARED_WORKDIR = os.path.join(
    os.environ.get("TEMP", "/tmp"), "slonagent_dash_test_workdir",
)


# Function-scoped — pytest-asyncio default test loop scope is function, and
# uvicorn task created here must share that loop with the test. Container и
# committed image переиспользуются между тестами через постоянный agent_dir,
# поэтому websockets ставится один раз на весь прогон, не на каждый тест.
@pytest.fixture
async def dash():
    os.makedirs(_SHARED_WORKDIR, exist_ok=True)
    workdir = _SHARED_WORKDIR
    port = _free_port()

    # Reset class-level WebTransport state in case prior tests touched it.
    WebTransport._app = None
    WebTransport._server_task = None
    WebTransport._tunnel_url = None
    WebTransport._tunnel_ready = None
    WebTransport.set_server_config(port=port, password_hash="")

    sb = SandboxSkill()
    cs = ConfigSkill()
    transport = DashboardTransport(verbose=False)
    agent = Agent(
        id="dashtest",
        model_name="dummy",
        api_key="",
        base_url="",
        agent_dir=str(workdir),
        memory_compressor=PassthroughCompressor(),
        skills=[sb, cs],
        transport=transport,
    )
    await agent.start(run_loop=False)

    base = f"http://127.0.0.1:{port}"
    # Wait for uvicorn to bind.
    async with httpx.AsyncClient(timeout=5.0) as cl:
        for _ in range(60):
            try:
                r = await cl.get(f"{base}/dashtest/dashboard/web/__probe__")
                if r.status_code in (200, 404):
                    break
            except Exception:
                pass
            await asyncio.sleep(0.25)
        else:
            pytest.fail("uvicorn didn't come up")

    web_dir = os.path.join(sb.workspace_dir, "web")
    os.makedirs(web_dir, exist_ok=True)

    yield {
        "agent": agent, "sandbox": sb, "config": cs,
        "transport": transport, "port": port,
        "web_dir": web_dir, "base": f"{base}/dashtest/dashboard",
    }

    transport.cleanup()
    if WebTransport._server_task is not None:
        WebTransport._server_task.cancel()
        try:
            await WebTransport._server_task
        except (asyncio.CancelledError, Exception):
            pass
    # Контейнер и образ оставляем — следующий тест переиспользует через тот же
    # _SHARED_WORKDIR, экономим pip install websockets и старт sandbox_proxy.


@pytest.fixture
async def http_client(dash):
    async with httpx.AsyncClient(timeout=30.0) as cl:
        yield cl


# ─── /web/ static ─────────────────────────────────────────────────────────────

class TestStatic:

    async def test_serves_html(self, dash, http_client):
        with open(os.path.join(dash["web_dir"], "static_html.html"), "w", encoding="utf-8") as f:
            f.write("<h1>hello-static</h1>")
        r = await http_client.get(f"{dash['base']}/web/static_html.html")
        assert r.status_code == 200
        assert "hello-static" in r.text
        assert r.headers["content-type"].startswith("text/html")

    async def test_serves_json(self, dash, http_client):
        with open(os.path.join(dash["web_dir"], "static_data.json"), "w", encoding="utf-8") as f:
            f.write('{"x": 1}')
        r = await http_client.get(f"{dash['base']}/web/static_data.json")
        assert r.status_code == 200
        assert r.json() == {"x": 1}

    async def test_404_for_missing(self, dash, http_client):
        r = await http_client.get(f"{dash['base']}/web/does_not_exist.html")
        assert r.status_code == 404

    async def test_blocks_path_traversal(self, dash, http_client):
        # Попытка вылезти из web/ — должна 404'ить, не отдать чужой файл.
        r = await http_client.get(f"{dash['base']}/web/../../../etc/passwd")
        assert r.status_code == 404


# ─── /web/*.py CGI ────────────────────────────────────────────────────────────

class TestCGI:

    async def test_basic_print(self, dash, http_client):
        with open(os.path.join(dash["web_dir"], "cgi_basic.py"), "w", encoding="utf-8") as f:
            f.write("print('hi-from-cgi')\n")
        r = await http_client.get(f"{dash['base']}/web/cgi_basic.py")
        assert r.status_code == 200
        assert "hi-from-cgi" in r.text

    async def test_request_query(self, dash, http_client):
        script = (
            "import json\n"
            "header('Content-Type', 'application/json')\n"
            "print(json.dumps({'name': request.query.get('name', '?'), 'method': request.method}))\n"
        )
        with open(os.path.join(dash["web_dir"], "cgi_query.py"), "w", encoding="utf-8") as f:
            f.write(script)
        r = await http_client.get(f"{dash['base']}/web/cgi_query.py?name=Slon")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        assert r.json() == {"name": "Slon", "method": "GET"}

    async def test_post_body(self, dash, http_client):
        script = (
            "header('Content-Type', 'text/plain')\n"
            "print('got:', request.body.decode())\n"
        )
        with open(os.path.join(dash["web_dir"], "cgi_post.py"), "w", encoding="utf-8") as f:
            f.write(script)
        r = await http_client.post(f"{dash['base']}/web/cgi_post.py",
                                   content=b"hello-body")
        assert r.status_code == 200
        assert "got: hello-body" in r.text

    async def test_custom_header(self, dash, http_client):
        script = (
            "header('X-Custom', 'totally-custom')\n"
            "header('Content-Type', 'text/plain')\n"
            "print('ok')\n"
        )
        with open(os.path.join(dash["web_dir"], "cgi_header.py"), "w", encoding="utf-8") as f:
            f.write(script)
        r = await http_client.get(f"{dash['base']}/web/cgi_header.py")
        assert r.status_code == 200
        assert r.headers.get("x-custom") == "totally-custom"

    async def test_edit_reflected_immediately(self, dash, http_client):
        path = os.path.join(dash["web_dir"], "cgi_live.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write("print('v1')\n")
        r1 = await http_client.get(f"{dash['base']}/web/cgi_live.py")
        assert "v1" in r1.text

        with open(path, "w", encoding="utf-8") as f:
            f.write("print('v2')\n")
        r2 = await http_client.get(f"{dash['base']}/web/cgi_live.py")
        assert "v2" in r2.text
        assert "v1" not in r2.text

    async def test_script_exception_returns_500(self, dash, http_client):
        with open(os.path.join(dash["web_dir"], "cgi_boom.py"), "w", encoding="utf-8") as f:
            f.write("raise ValueError('nope')\n")
        r = await http_client.get(f"{dash['base']}/web/cgi_boom.py")
        assert r.status_code == 500
        assert "ValueError" in r.text
        assert "nope" in r.text


# ─── /sandbox/{port}/... port forwarding ──────────────────────────────────────

class TestPortForward:

    async def test_http_to_inside_container(self, dash, http_client):
        # Запускаем простейший HTTP-сервер внутри контейнера на 127.0.0.1:9876.
        sb = dash["sandbox"]
        await sb.exec(
            "cat > /tmp/tiny_server.py <<'PY'\n"
            "from http.server import HTTPServer, BaseHTTPRequestHandler\n"
            "class H(BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        self.send_response(200)\n"
            "        self.send_header('Content-Type', 'text/plain')\n"
            "        self.end_headers()\n"
            "        self.wfile.write(b'inside-container')\n"
            "    def log_message(self, *a, **kw): pass\n"
            "HTTPServer(('127.0.0.1', 9876), H).serve_forever()\n"
            "PY\n"
            "nohup python3 /tmp/tiny_server.py > /tmp/tiny.log 2>&1 &\n"
            "echo $! > /tmp/tiny.pid"
        )
        # Дать серверу подняться.
        await asyncio.sleep(1.0)

        r = await http_client.get(f"{dash['base']}/sandbox/9876/anything")
        assert r.status_code == 200
        assert "inside-container" in r.text

        await sb.exec("kill $(cat /tmp/tiny.pid) 2>/dev/null; rm -f /tmp/tiny.pid /tmp/tiny_server.py")

    async def test_unreachable_port_returns_502(self, dash, http_client):
        # Несуществующий порт внутри контейнера → proxy worker возвращает 502.
        r = await http_client.get(f"{dash['base']}/sandbox/19999/whatever")
        assert r.status_code == 502
