"""Sub-agent end-to-end тесты с echo backend, без podman.

Покрывают два сценария:
- ephemeral: Agent с agent_dir=None — нет персистентности, нет skills которые
  требуют dir (sandbox/config), нельзя дальше spawn_subagent.
- regular: parent.spawn_subagent → дир под parent'ом, persistent, использует
  Agent.from_config + propagate_stop как в проде.

Запуск:
    .venv\\Scripts\\python -m pytest tests/test_subagent.py -v
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent import Agent
from src.memory.providers.base import BaseProvider
from src.transport.base import BaseTransport


class PassthroughCompressor(BaseProvider):
    def __init__(self):
        super().__init__(consolidate_tokens=0)

    async def compress(self, turns):
        return turns


class CaptureTransport(BaseTransport):
    """Запоминает все final send_message и сигналит done на первом."""

    def __init__(self):
        super().__init__()
        self.messages: list[str] = []
        self.done = asyncio.Event()

    async def send_message(self, text, stream_id=None, final=True):
        if final:
            self.messages.append(text)
            self.done.set()


async def test_ephemeral_subagent_runs_echo():
    """Ephemeral: agent_dir=None, ничего на диск не падает. Echo backend
    отрабатывает один turn через process_message → loop → CaptureTransport."""
    transport = CaptureTransport()
    sub = Agent(
        id="ephemeral",
        model_name="dummy",
        api_key="",
        base_url="",
        backend="echo",
        agent_dir=None,
        memory_compressor=PassthroughCompressor(),
        transport=transport,
        skills=[],
    )
    await sub.start()  # default run_loop=True — loop крутится сам
    try:
        await sub.process_message([{"type": "text", "text": "ping"}])
        await asyncio.wait_for(transport.done.wait(), timeout=10)
        assert transport.messages, "no message captured"
        assert transport.messages[0].startswith("эхо: ")
        assert "ping" in transport.messages[0]
    finally:
        sub.stop()
        await sub.close()


async def test_regular_subagent_via_spawn_runs_echo(tmp_path):
    """Regular pattern: parent.spawn_subagent → агент в parent_dir/memory/subagents/<name>.
    Драйвим loop вручную (spawn_subagent ставит run_loop=False), capture в parent
    transport (sub наследует transport родителя по умолчанию)."""
    transport = CaptureTransport()
    parent = Agent(
        id="parent",
        model_name="dummy",
        api_key="",
        base_url="",
        backend="echo",
        agent_dir=str(tmp_path),
        memory_compressor=PassthroughCompressor(),
        transport=transport,
        skills=[],
    )
    # spawn_subagent зовёт Agent.from_config(self._config, ...) — но _config
    # ставится только когда сам parent создан через from_config. Прямой
    # __init__ оставляет _config пустым → подкладываем минимум.
    parent._config = {"model_name": "dummy", "api_key": "",
                      "base_url": "", "backend": "echo"}
    await parent.start(run_loop=False)
    try:
        sub = await parent.spawn_subagent(
            "reg", backend="echo", skills=[], memory_providers=[],
        )
        loop_task = asyncio.create_task(sub.loop())
        try:
            await sub.process_message([{"type": "text", "text": "hello"}])
            await asyncio.wait_for(transport.done.wait(), timeout=10)
            assert transport.messages, "no message captured"
            assert transport.messages[0].startswith("эхо: ")
            assert "hello" in transport.messages[0]
            # Sub-agent создал свою директорию под parent.
            sub_dir = tmp_path / "memory" / "subagents" / "reg"
            assert sub_dir.is_dir()
        finally:
            loop_task.cancel()
            try:
                await loop_task
            except BaseException:
                pass
    finally:
        parent.stop()
        await parent.close()
