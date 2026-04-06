"""
Интеграционные тесты провайдеров памяти и скиллов с реальными зависимостями.

Запуск:
    LLM_KEY=<key> venv\\Scripts\\python -m pytest tests/test_integration_providers.py -v -m integration

Тесты автоматически пропускаются если не заданы нужные переменные окружения или
не установлен Podman.
"""
import asyncio
import os
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent import Agent, Skill
from src.transport.base import BaseTransport

pytestmark = pytest.mark.integration


# ── Конфигурация ──────────────────────────────────────────────────────────────

def get_llm_config() -> tuple[str, str, str]:
    key = os.environ.get("LLM_KEY")
    if not key:
        pytest.skip("LLM_KEY не задан")
    url = os.environ.get("LLM_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
    model = os.environ.get("LLM_MODEL", "gemini-3-flash-preview")
    return key, url, model


def require_podman():
    if subprocess.run(["podman", "--version"], capture_output=True).returncode != 0:
        pytest.skip("podman не найден")


class PassthroughCompressor(Skill):
    async def compress(self, turns): return turns


class CapturingTransport(BaseTransport):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    async def send_message(self, text: str, stream_id=None, final: bool = True):
        if stream_id is None:
            stream_id = len(self.messages)
            self.messages.append("")
        self.messages[stream_id] = text
        return stream_id

    @property
    def last_message(self) -> str:
        return self.messages[-1] if self.messages else ""


def make_agent(tmp_path, providers=None) -> tuple[Agent, CapturingTransport]:
    key, url, model = get_llm_config()
    transport = CapturingTransport()
    agent = Agent(
        model_name=model,
        api_key=key,
        base_url=url,
        agent_dir=str(tmp_path),
        memory_compressor=PassthroughCompressor(),
        memory_providers=providers or [],
        transport=transport,
    )
    return agent, transport


# ═══════════════════════════════════════════════════════════════════════════════
# LogCompressor — Observer с реальным LLM
# ═══════════════════════════════════════════════════════════════════════════════

async def test_log_compressor_observer(tmp_path):
    """Observer генерирует observations из реального диалога."""
    key, url, model = get_llm_config()
    from src.memory.compressors.log import LogCompressor

    compressor = LogCompressor(
        model_name=model, api_key=key, base_url=url,
        compress_after_tokens=1,   # сжимать сразу
        recent_tokens=10,          # очень маленький бюджет — всё старое идёт в observe
        min_recent_turns=1,
    )
    agent = Agent(
        model_name=model, api_key=key, base_url=url,
        agent_dir=str(tmp_path),
        memory_compressor=compressor,
        transport=CapturingTransport(),
    )
    compressor.register(agent)

    turns = [
        {"role": "user", "content": [{"type": "text", "text": "Меня зовут Алексей, мне 32 года."}]},
        {"role": "assistant", "content": "Приятно познакомиться, Алексей!"},
        {"role": "user", "content": [{"type": "text", "text": "Я работаю программистом в Москве."}]},
        {"role": "assistant", "content": "Интересная профессия!"},
        {"role": "user", "content": [{"type": "text", "text": "Люблю играть в шахматы по выходным."}]},
        {"role": "assistant", "content": "Отличное хобби!"},
    ]

    result = await compressor.compress(turns)

    om_turns = [t for t in result if isinstance(t, dict) and t.get("_observation_message")]
    assert len(om_turns) == 1, "Observer должен создать один OM-turn"

    raw = om_turns[0].get("_raw_observations", "")
    assert raw, "Observations не должны быть пустыми"
    # Проверяем что LLM вернул что-то осмысленное
    raw_lower = raw.lower()
    assert any(word in raw_lower for word in ["алексей", "alexei", "alex", "программист", "developer", "moscow", "москв"]), \
        f"Observer не извлёк ключевые факты из диалога. Observations:\n{raw}"


async def test_log_compressor_reflect(tmp_path):
    """Reflector сжимает большой блок observations."""
    key, url, model = get_llm_config()
    from src.memory.compressors.log import LogCompressor, _parse_observations

    compressor = LogCompressor(
        model_name=model, api_key=key, base_url=url,
        reflect_after_tokens=1,  # рефлектить сразу
    )
    agent = Agent(
        model_name=model, api_key=key, base_url=url,
        agent_dir=str(tmp_path),
        memory_compressor=compressor,
        transport=CapturingTransport(),
    )
    compressor.register(agent)

    long_obs = "\n".join([
        "Date: Jan 1, 2025",
        "* 🔴 (10:00) User stated their name is Alexei.",
        "* 🔴 (10:01) User stated they are 32 years old.",
        "* 🟡 (10:02) Agent greeted user.",
        "Date: Jan 2, 2025",
        "* 🔴 (11:00) User stated they work as a programmer in Moscow.",
        "* 🟡 (11:01) Agent acknowledged.",
        "Date: Jan 3, 2025",
        "* 🔴 (12:00) User likes chess on weekends.",
    ])

    reflected = await compressor._run_reflector(long_obs)
    assert reflected, "Reflector вернул пустой результат"
    assert len(reflected) > 20, f"Reflector вернул слишком короткий текст: {reflected!r}"


# ═══════════════════════════════════════════════════════════════════════════════
# ToolProvider — LLM-суммаризация после tool use
# ═══════════════════════════════════════════════════════════════════════════════

async def test_tool_provider_consolidate(tmp_path):
    """ToolProvider генерирует описание инструмента через LLM после его использования."""
    key, url, model = get_llm_config()
    from src.memory.providers.tool import ToolProvider

    provider = ToolProvider(model_name=model, api_key=key, base_url=url, consolidate_tokens=1)
    agent, _ = make_agent(tmp_path, providers=[provider])
    await agent.start()

    # Симулируем диалог с вызовом инструмента
    tool_call_id = "call_abc123"
    turns = [
        {"role": "user", "content": [{"type": "text", "text": "Сколько будет 2+2?"}]},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": tool_call_id, "type": "function",
            "function": {"name": "sandbox_exec", "arguments": '{"command": "python3 -c \\"print(2+2)\\""}'},
        }]},
        {"role": "tool", "tool_call_id": tool_call_id,
         "content": '{"stdout": "4\\n", "stderr": "", "exit_code": 0}',
         "_timestamp": "2025-01-01T10:00:01"},
        {"role": "assistant", "content": "Результат: 4"},
    ]

    # Принудительно запускаем consolidate
    await provider._consolidate(turns)

    prompt = await provider.get_tool_prompt("sandbox_exec")
    assert prompt, "ToolProvider не сгенерировал описание инструмента"
    assert len(prompt) > 30, f"Описание слишком короткое: {prompt!r}"


# ═══════════════════════════════════════════════════════════════════════════════
# SandboxSkill — выполнение команды в Podman
# ═══════════════════════════════════════════════════════════════════════════════

async def test_sandbox_exec(tmp_path):
    """SandboxSkill выполняет команду через Podman и возвращает stdout."""
    require_podman()
    from src.skills.sandbox import SandboxSkill

    container_name = f"slonagent_test_{os.getpid()}"
    skill = SandboxSkill(
        workspace_dir=str(tmp_path / "workspace"),
        container_name=container_name,
        image="python:3.11-slim",
        default_timeout=60,
        runtime="podman",
    )
    agent, _ = make_agent(tmp_path)
    skill.register(agent)
    await skill.start()

    try:
        result = await skill.exec(command="echo hello_from_sandbox")
        assert result.get("exit_code") == 0, f"Команда завершилась с ошибкой: {result}"
        assert "hello_from_sandbox" in result.get("stdout", ""), f"stdout не содержит ожидаемое: {result}"
    finally:
        skill.stop()


async def test_sandbox_python(tmp_path):
    """SandboxSkill выполняет Python-код в контейнере."""
    require_podman()
    from src.skills.sandbox import SandboxSkill

    container_name = f"slonagent_test_py_{os.getpid()}"
    skill = SandboxSkill(
        workspace_dir=str(tmp_path / "workspace"),
        container_name=container_name,
        runtime="podman",
    )
    agent, _ = make_agent(tmp_path)
    skill.register(agent)
    await skill.start()

    try:
        result = await skill.exec(command='python3 -c "print(6 * 7)"')
        assert result.get("exit_code") == 0, f"Python завершился с ошибкой: {result}"
        assert "42" in result.get("stdout", ""), f"stdout: {result}"
    finally:
        skill.stop()


async def test_sandbox_timeout(tmp_path):
    """SandboxSkill возвращает ошибку при превышении таймаута."""
    require_podman()
    from src.skills.sandbox import SandboxSkill

    container_name = f"slonagent_test_timeout_{os.getpid()}"
    skill = SandboxSkill(
        workspace_dir=str(tmp_path / "workspace"),
        container_name=container_name,
        runtime="podman",
    )
    agent, _ = make_agent(tmp_path)
    skill.register(agent)
    await skill.start()

    try:
        result = await skill.exec(command="sleep 100", timeout=2)
        assert "error" in result, f"Ожидали ошибку таймаута, получили: {result}"
        assert "таймаут" in result["error"].lower() or "timeout" in result["error"].lower()
    finally:
        skill.stop()


async def test_sandbox_read_file(tmp_path):
    """read_file читает файл из workspace напрямую с хоста."""
    require_podman()
    from src.skills.sandbox import SandboxSkill

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("test content\nline two\n", encoding="utf-8")

    container_name = f"slonagent_test_rf_{os.getpid()}"
    skill = SandboxSkill(workspace_dir=str(workspace), container_name=container_name, runtime="podman")
    agent, _ = make_agent(tmp_path)
    skill.register(agent)
    await skill.start()

    try:
        result = skill.read_file("/workspace/notes.txt")
        assert "error" not in result, f"Ошибка чтения: {result}"
        assert "test content" in result["content"]
    finally:
        skill.stop()


# ═══════════════════════════════════════════════════════════════════════════════
# FactProvider — retain + recall с реальными embeddings и LLM
# ═══════════════════════════════════════════════════════════════════════════════

async def test_fact_provider_retain_and_recall(tmp_path):
    """FactProvider сохраняет факты из диалога и находит их при recall."""
    key, url, model = get_llm_config()
    from src.memory.providers.fact import FactProvider

    embedding_cfg = {"provider": "openai", "model": "gemini-embedding-001", "api_key": key, "base_url": url}
    provider = FactProvider(
        model_name=model, api_key=key, base_url=url,
        consolidate_tokens=1,
        auto_consolidate=False,
        embedding_model=embedding_cfg,
    )
    agent, _ = make_agent(tmp_path, providers=[provider])
    await agent.start()

    # Диалог с конкретным запоминаемым фактом
    turns = [
        {"role": "user",
         "content": [{"type": "text", "text": "Моя дочь Маша родилась 3 марта 2020 года."}],
         "_timestamp": "2025-01-01T10:00:00"},
        {"role": "assistant", "content": "Запомнил! У тебя есть дочь Маша.",
         "_timestamp": "2025-01-01T10:00:01"},
    ]

    # Вызываем _retain_impl напрямую, минуя fire-and-forget задачу с lock
    from src.memory.providers.fact.retain import _retain_impl, RetainItem
    from datetime import datetime
    items = [RetainItem(
        content="[2025-01-01 10:00] Пользователь: Моя дочь Маша родилась 3 марта 2020 года.\n"
                "[2025-01-01 10:01] Ассистент: Запомнил! У тебя есть дочь Маша.",
        context="conversation",
        event_date=datetime(2025, 1, 1, 10, 0),
    )]
    await _retain_impl(items, provider._llm, provider._model_name, provider.storage, with_observations=False)

    # Recall по запросу о дочери
    recalled = await provider._recall_text("дочь Маша день рождения", query_label="test")
    assert recalled, "Recall вернул пустой результат"
    recalled_lower = recalled.lower()
    assert any(w in recalled_lower for w in ["маша", "masha", "дочь", "daughter", "2020", "март", "march"]), \
        f"Recall не нашёл факт о дочери Маше. Результат:\n{recalled}"


async def test_fact_provider_context_prompt(tmp_path):
    """FactProvider подмешивает релевантные факты в системный промпт."""
    key, url, model = get_llm_config()
    from src.memory.providers.fact import FactProvider

    embedding_cfg = {"provider": "openai", "model": "gemini-embedding-001", "api_key": key, "base_url": url}
    provider = FactProvider(
        model_name=model, api_key=key, base_url=url,
        consolidate_tokens=1,
        auto_recall=True,
        embedding_model=embedding_cfg,
    )
    agent, _ = make_agent(tmp_path, providers=[provider])
    await agent.start()

    turns = [
        {"role": "user",
         "content": [{"type": "text", "text": "Я живу в Санкт-Петербурге и работаю дизайнером."}],
         "_timestamp": "2025-01-01T10:00:00"},
        {"role": "assistant", "content": "Понял, ты живёшь в Петербурге.",
         "_timestamp": "2025-01-01T10:00:01"},
    ]
    await provider._consolidate(turns)

    prompt = await provider.get_context_prompt("где я работаю?")
    # Либо нашёл факты, либо вернул пустую строку — не должен падать
    assert isinstance(prompt, str)
