"""End-to-end test для /agent-rpc — sandbox-скрипты дотягиваются до host-агента
по своей инициативе через WS-канал.

Поднимает реальный DashboardFork (uvicorn + sandbox container), кладёт скрипт
в /workspace, запускает его обычным `python /workspace/script.py` (НЕ через
runner.py — мы тестируем именно ленивый путь). Скрипт делает
`from agent import get_agent`, открывает WS на `/agent-rpc`, дёргает методы
host-агента, проверяем побочный эффект на хосте.

Запуск:
    .venv\\Scripts\\python -m pytest tests/test_sandbox_agent_rpc.py -v -m integration
"""
import asyncio
import os
import socket
import subprocess
import sys

import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent import Agent
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


class CaptureDashboardTransport(DashboardTransport):
    """DashboardTransport который копит send_message-тексты — для проверки
    что вызов из контейнера долетел до host-стороны транспорта."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.captured: list[str] = []

    async def send_message(self, text, stream_id=None, final=True):
        self.captured.append(text)


# Отдельный от test_dashboard_integration workdir — чтобы не делить контейнер
# (тот мог быть создан до изменения с SLONAGENT_RPC_URL и не иметь env-var).
_WORKDIR = os.path.join(
    os.environ.get("TEMP", "/tmp"), "slonagent_agentrpc_test_workdir",
)


@pytest.fixture
async def dash():
    os.makedirs(_WORKDIR, exist_ok=True)
    port = _free_port()

    # Reset process-wide WebTransportServer state in case prior tests touched it.
    WebTransportServer._app = None
    WebTransportServer._tunnel_url = None
    WebTransportServer._tunnel_ready = None
    WebTransport._forks.clear()
    WebTransport.start({"port": port, "password_hash": ""})

    sb = SandboxSkill()
    cs = ConfigSkill()
    transport = CaptureDashboardTransport(verbose=False)
    agent = Agent(
        id="rpctest",
        model_name="dummy",
        api_key="",
        base_url="",
        agent_dir=_WORKDIR,
        memory_compressor=PassthroughCompressor(),
        skills=[sb, cs],
        transport=transport,
    )
    await agent.start(run_loop=False)

    # /agent-rpc хендлер резолвит agent через WebTransport.make_agent;
    # в production его регистрирует main.py, в тесте подсовываем заглушку
    # которая отдаёт нашего test-агента.
    async def _make_agent(agent_id, _thread_id=""):
        return agent if agent_id == agent.id else None
    WebTransport.make_agent = staticmethod(_make_agent)

    base = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(timeout=5.0) as cl:
        for _ in range(60):
            try:
                r = await cl.get(f"{base}/rpctest/dashboard/web/__probe__")
                if r.status_code in (200, 404):
                    break
            except Exception:
                pass
            await asyncio.sleep(0.25)
        else:
            pytest.fail("uvicorn didn't come up")

    # На каждый тест — свежий контейнер: уже идущий контейнер от прошлого
    # теста хранит SLONAGENT_RPC_URL в /run/slonagent.env с прежним портом
    # uvicorn (разный _free_port каждый раз). _sync_rpc_url перезапишет, но
    # проще сносить — изоляция тестов важнее экономии 5 секунд.
    sb.stop()
    sb._mounts_hash = None

    yield {"agent": agent, "sandbox": sb, "transport": transport, "port": port}

    transport.cleanup()


# ─── Tests ────────────────────────────────────────────────────────────────────

class TestAgentRpc:

    async def test_send_message_round_trip(self, dash):
        """Скрипт в контейнере: get_agent() → agent.transport.send_message →
        WS обратно на host → CaptureDashboardTransport ловит текст."""
        sb = dash["sandbox"]
        transport = dash["transport"]

        script_path = os.path.join(sb.workspace_dir, "rpc_send.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(
                "from agent import get_agent\n"
                "agent = get_agent()\n"
                "agent.transport.send_message('hello-from-container').wait()\n"
            )
        r = await sb.exec("python /workspace/rpc_send.py", timeout=180)
        assert r["exit_code"] == 0, r
        assert "hello-from-container" in transport.captured, transport.captured

    async def test_memory_add_turn_persists_on_host(self, dash):
        """Скрипт зовёт agent.memory.add_turn — host-side memory._turns содержит запись."""
        sb = dash["sandbox"]
        agent = dash["agent"]

        before = len(agent.memory._turns)

        script_path = os.path.join(sb.workspace_dir, "rpc_memory.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(
                "from agent import get_agent\n"
                "agent = get_agent()\n"
                "agent.memory.add_turn({'role': 'user', 'content': 'from-script'}).wait()\n"
            )
        r = await sb.exec("python /workspace/rpc_memory.py", timeout=180)
        assert r["exit_code"] == 0, r

        added = agent.memory._turns[before:]
        contents = [t.get("content") for t in added]
        assert "from-script" in contents, contents

    async def test_no_rpc_url_when_no_fork(self, dash):
        """Sanity: если URL'а нет нигде (ни в файле, ни в env), get_agent()
        даёт понятную ошибку, а не молча виснет."""
        sb = dash["sandbox"]
        script_path = os.path.join(sb.workspace_dir, "rpc_noenv.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(
                "import os\n"
                "os.environ.pop('SLONAGENT_RPC_URL', None)\n"
                "try:\n"
                "    os.unlink('/run/slonagent.env')\n"
                "except FileNotFoundError:\n"
                "    pass\n"
                "from agent import get_agent\n"
                "try:\n"
                "    get_agent()\n"
                "    print('UNEXPECTED_OK')\n"
                "except RuntimeError as e:\n"
                "    print('ERR:', e)\n"
            )
        r = await sb.exec("python /workspace/rpc_noenv.py", timeout=60)
        assert r["exit_code"] == 0, r
        assert "SLONAGENT RPC URL" in r["stdout"]
        assert "UNEXPECTED_OK" not in r["stdout"]
