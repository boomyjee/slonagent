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
from src.memory.providers.base import BaseProvider
from src.skills.config import ConfigSkill
from src.skills.sandbox import SandboxSkill
from src.transport.dashboard import DashboardTransport
from src.transport.web import WebTransport, WebTransportServer


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


class PassthroughCompressor(BaseProvider):
    def __init__(self): super().__init__(consolidate_tokens=0)
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

    # Reset process-wide WebTransportServer state in case prior tests touched it.
    WebTransportServer._app = None
    WebTransportServer._tunnel_url = None
    WebTransportServer._tunnel_ready = None
    WebTransport._forks.clear()
    WebTransport.start({"port": port, "password_hash": ""})

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
    # uvicorn-task живёт под прикрытием loop.call_soon_threadsafe — отдельной
    # ссылки на него теперь нет, отменять некого. На pytest-уровне loop
    # закрывается после теста и сервер уходит вместе с ним.
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
        r = await http_client.get(f"{dash['base']}/~/workspace/web/static_html.html")
        assert r.status_code == 200
        assert "hello-static" in r.text
        assert r.headers["content-type"].startswith("text/html")

    async def test_serves_json(self, dash, http_client):
        with open(os.path.join(dash["web_dir"], "static_data.json"), "w", encoding="utf-8") as f:
            f.write('{"x": 1}')
        r = await http_client.get(f"{dash['base']}/~/workspace/web/static_data.json")
        assert r.status_code == 200
        assert r.json() == {"x": 1}

    async def test_404_for_missing(self, dash, http_client):
        r = await http_client.get(f"{dash['base']}/~/workspace/web/does_not_exist.html")
        assert r.status_code == 404

    async def test_blocks_path_traversal(self, dash, http_client):
        # Попытка вылезти из web/ — должна 404'ить, не отдать чужой файл.
        r = await http_client.get(f"{dash['base']}/~/workspace/web/../../../etc/passwd")
        assert r.status_code == 404


# ─── /web/*.py CGI ────────────────────────────────────────────────────────────

class TestCGI:

    async def test_basic_print(self, dash, http_client):
        with open(os.path.join(dash["web_dir"], "cgi_basic.py"), "w", encoding="utf-8") as f:
            f.write("print('hi-from-cgi')\n")
        r = await http_client.get(f"{dash['base']}/~/workspace/web/cgi_basic.py")
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
        r = await http_client.get(f"{dash['base']}/~/workspace/web/cgi_query.py?name=Slon")
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
        r = await http_client.post(f"{dash['base']}/~/workspace/web/cgi_post.py",
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
        r = await http_client.get(f"{dash['base']}/~/workspace/web/cgi_header.py")
        assert r.status_code == 200
        assert r.headers.get("x-custom") == "totally-custom"

    async def test_edit_reflected_immediately(self, dash, http_client):
        path = os.path.join(dash["web_dir"], "cgi_live.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write("print('v1')\n")
        r1 = await http_client.get(f"{dash['base']}/~/workspace/web/cgi_live.py")
        assert "v1" in r1.text

        with open(path, "w", encoding="utf-8") as f:
            f.write("print('v2')\n")
        r2 = await http_client.get(f"{dash['base']}/~/workspace/web/cgi_live.py")
        assert "v2" in r2.text
        assert "v1" not in r2.text

    async def test_imported_module_edit_reflected(self, dash, http_client):
        """Регрессия: worker долгоживущий, sys.modules кешировал импорты —
        правки в helper.py не подтягивались. Сбрасываем workspace-модули перед
        каждым exec'ом → следующий import читает с диска."""
        helper = os.path.join(dash["web_dir"], "cgi_helper_mod.py")
        cgi = os.path.join(dash["web_dir"], "cgi_uses_helper.py")
        with open(helper, "w", encoding="utf-8") as f:
            f.write("VALUE = 'first'\n")
        with open(cgi, "w", encoding="utf-8") as f:
            f.write(
                "import sys; sys.path.insert(0, '/workspace/web')\n"
                "import cgi_helper_mod\n"
                "print(cgi_helper_mod.VALUE)\n"
            )
        r1 = await http_client.get(f"{dash['base']}/~/workspace/web/cgi_uses_helper.py")
        assert "first" in r1.text

        with open(helper, "w", encoding="utf-8") as f:
            f.write("VALUE = 'second'\n")
        r2 = await http_client.get(f"{dash['base']}/~/workspace/web/cgi_uses_helper.py")
        assert "second" in r2.text, r2.text
        assert "first" not in r2.text

    async def test_script_exception_returns_500(self, dash, http_client):
        with open(os.path.join(dash["web_dir"], "cgi_boom.py"), "w", encoding="utf-8") as f:
            f.write("raise ValueError('nope')\n")
        r = await http_client.get(f"{dash['base']}/~/workspace/web/cgi_boom.py")
        assert r.status_code == 500
        assert "ValueError" in r.text
        assert "nope" in r.text

    async def test_directory_index_html(self, dash, http_client):
        os.makedirs(os.path.join(dash["web_dir"], "subdir_html"), exist_ok=True)
        with open(os.path.join(dash["web_dir"], "subdir_html", "index.html"), "w", encoding="utf-8") as f:
            f.write("<h1>html-index</h1>")
        r = await http_client.get(f"{dash['base']}/~/workspace/web/subdir_html")
        assert r.status_code == 200
        assert "html-index" in r.text

    async def test_directory_index_py(self, dash, http_client):
        os.makedirs(os.path.join(dash["web_dir"], "subdir_py"), exist_ok=True)
        with open(os.path.join(dash["web_dir"], "subdir_py", "index.py"), "w", encoding="utf-8") as f:
            f.write("print('py-index')\n")
        r = await http_client.get(f"{dash['base']}/~/workspace/web/subdir_py")
        assert r.status_code == 200
        assert "py-index" in r.text


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


# ─── Worker recovery — host-side respawn after worker died ────────────────────

class TestWorkerRecovery:

    async def test_respawns_worker_killed_inside_container(self, dash, http_client):
        """Воркер умер (например, host долго был оффлайн → 10 фейлов реконнекта,
        worker сдался). Следующий CGI-запрос должен поднять нового воркера через
        _start_worker, не падать с 502."""
        sb = dash["sandbox"]
        # Сначала убедимся что воркер вообще поднялся хотя бы раз — простая
        # CGI-команда форсит handle_cgi → _ensure_tunnel → _start_worker.
        with open(os.path.join(dash["web_dir"], "recover_warmup.py"), "w", encoding="utf-8") as f:
            f.write("print('warmup')\n")
        r = await http_client.get(f"{dash['base']}/~/workspace/web/recover_warmup.py")
        assert r.status_code == 200

        # Убиваем воркер прямо в контейнере, имитируя ситуацию "процесс сдох".
        await sb.exec("pkill -9 -f /slonagent/sandbox_proxy.py", timeout=5)
        # Дать host'у заметить разрыв туннеля (handle_tunnel finally → tunnel=None).
        await asyncio.sleep(1.0)

        # Следующий CGI-запрос должен авто-перестартовать воркера.
        with open(os.path.join(dash["web_dir"], "recover_after.py"), "w", encoding="utf-8") as f:
            f.write("print('after-recovery')\n")
        r = await http_client.get(f"{dash['base']}/~/workspace/web/recover_after.py")
        assert r.status_code == 200
        assert "after-recovery" in r.text


# ─── _worker_url: subagent inheritance regression ─────────────────────────────

class TestWorkerURL:

    async def test_uses_fork_ref_agent_id_not_subagent_id(self, dash):
        """Subagent шарит fork с родителем. transport.agent у субагента может
        быть переустановлен (id вроде 'main:claude_code'), но fork — один,
        по ref_agent.id (id главного агента форка). worker URL обязан брать
        fork.ref_agent.id, иначе WebSocket-роут не совпадёт → 403."""
        transport = dash["transport"]
        fork = transport.fork
        original_ref_id = fork.ref_agent.id
        url = await fork._proxy._worker_url()
        assert f"/{original_ref_id}/dashboard/sandbox-tunnel" in url
        assert "main:claude_code" not in url
