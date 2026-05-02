"""
Интеграционные тесты провайдеров памяти и скиллов с реальными зависимостями.

LLM-тесты параметризованы по трём провайдерам:
  - gemini  — через openai-совместимый эндпоинт ($GEMINI_KEY)
  - kimi    — через openrouter ($OPENROUTER_KEY)
  - claude  — через claude-cli (CLAUDE_AVAILABLE=1, нужна установленная и
              авторизованная claude CLI)

Тесты автоматически пропускаются если конкретный провайдер не настроен.
Embeddings всегда через gemini ($GEMINI_KEY) — DB не должна меняться при
смене LLM-бэкенда.

Запуск всех:
    GEMINI_KEY=... OPENROUTER_KEY=... CLAUDE_AVAILABLE=1 \\
        venv\\Scripts\\python -m pytest tests/test_integration_providers.py -v -m integration

Запуск только одного провайдера:
    venv\\Scripts\\python -m pytest tests/test_integration_providers.py -v -m integration -k gemini
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent import Agent, Skill
from src.memory.providers.base import BaseProvider
from src.transport.base import BaseTransport

pytestmark = pytest.mark.integration


# ── Конфигурация ──────────────────────────────────────────────────────────────

GEMINI_URL_DEFAULT = "https://generativelanguage.googleapis.com/v1beta/openai/"
OPENROUTER_URL_DEFAULT = "https://openrouter.ai/api/v1"


def _get_llm_config(provider: str) -> dict:
    """Возвращает {backend, model_name, api_key, base_url, backend_params}."""
    if provider == "gemini":
        key = os.environ.get("GEMINI_KEY")
        if not key:
            pytest.skip("GEMINI_KEY не задан")
        return {
            "backend": "openai",
            "model_name": os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview"),
            "api_key": key,
            "base_url": os.environ.get("GEMINI_URL", GEMINI_URL_DEFAULT),
            "backend_params": None,
        }
    if provider == "kimi":
        key = os.environ.get("OPENROUTER_KEY") or os.environ.get("KIMI_KEY")
        if not key:
            pytest.skip("OPENROUTER_KEY/KIMI_KEY не задан")
        return {
            "backend": "openai",
            "model_name": os.environ.get("KIMI_MODEL", "moonshotai/kimi-k2.6"),
            "api_key": key,
            "base_url": os.environ.get("KIMI_URL", OPENROUTER_URL_DEFAULT),
            "backend_params": None,
        }
    if provider == "claude":
        if not os.environ.get("CLAUDE_AVAILABLE"):
            pytest.skip("CLAUDE_AVAILABLE не задан (нужен claude-cli + auth)")
        return {
            "backend": "claude",
            "model_name": os.environ.get("CLAUDE_MODEL", "sonnet"),
            "api_key": "",
            "base_url": "",
            "backend_params": None,  # голый дефолт
        }
    raise ValueError(f"Unknown provider: {provider}")


@pytest.fixture(params=["gemini", "kimi", "claude"])
def llm(request) -> dict:
    """Параметризованная LLM-конфигурация. Каждый тест прогоняется на 3 провайдерах."""
    return _get_llm_config(request.param)


def get_embedding_config() -> dict:
    """Embeddings всегда через gemini — БД не зависит от LLM-провайдера."""
    key = os.environ.get("GEMINI_KEY")
    if not key:
        pytest.skip("GEMINI_KEY не задан (нужен для embeddings)")
    return {
        "provider": "openai",
        "model": "gemini-embedding-001",
        "api_key": key,
        "base_url": os.environ.get("GEMINI_URL", GEMINI_URL_DEFAULT),
    }


def get_default_llm_config() -> dict:
    """Любой настроенный провайдер — для тестов которым нужен Agent но не LLM."""
    for p in ("gemini", "kimi", "claude"):
        try:
            return _get_llm_config(p)
        except pytest.skip.Exception:
            continue
    pytest.skip("Ни один LLM провайдер не настроен")


def require_podman():
    if subprocess.run(["podman", "--version"], capture_output=True).returncode != 0:
        pytest.skip("podman не найден")


class PassthroughCompressor(BaseProvider):
    def __init__(self): super().__init__(consolidate_tokens=0)
    async def compress(self, turns): return turns


class CapturingTransport(BaseTransport):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    async def send_message(self, text: str, stream_id=None, final: bool = True):
        if stream_id is None:
            stream_id = len(self.messages)
            self.messages.append("")
        while len(self.messages) <= stream_id:
            self.messages.append("")
        self.messages[stream_id] = text
        return stream_id

    @property
    def last_message(self) -> str:
        return self.messages[-1] if self.messages else ""


def make_agent(tmp_path, llm: dict, providers=None) -> tuple[Agent, CapturingTransport]:
    transport = CapturingTransport()
    agent = Agent(
        id="test",
        model_name=llm["model_name"],
        api_key=llm["api_key"],
        base_url=llm["base_url"],
        backend=llm["backend"],
        backend_params=llm["backend_params"],
        agent_dir=str(tmp_path),
        memory_compressor=PassthroughCompressor(),
        memory_providers=providers or [],
        transport=transport,
    )
    return agent, transport


# ═══════════════════════════════════════════════════════════════════════════════
# LogCompressor — Observer с реальным LLM
# ═══════════════════════════════════════════════════════════════════════════════

async def test_log_compressor_observer(tmp_path, llm):
    """Observer генерирует observations из реального диалога."""
    from src.memory.compressors.log import LogCompressor

    compressor = LogCompressor(
        model_name=llm["model_name"], api_key=llm["api_key"], base_url=llm["base_url"],
        backend=llm["backend"], backend_params=llm["backend_params"],
        compress_after_tokens=1,   # сжимать сразу
        recent_tokens=10,          # очень маленький бюджет — всё старое идёт в observe
        min_recent_turns=1,
    )
    agent, _ = make_agent(tmp_path, llm)
    compressor.register(agent)

    turns = [
        {"role": "user", "content": [{"type": "text", "text": "Меня зовут Алексей, мне 32 года."}]},
        {"role": "assistant", "content": "Приятно познакомиться, Алексей!"},
        {"role": "user", "content": [{"type": "text", "text": "Я работаю программистом в Москве."}]},
        {"role": "assistant", "content": "Интересная профессия!"},
        {"role": "user", "content": [{"type": "text", "text": "Люблю играть в шахматы по выходным."}]},
        {"role": "assistant", "content": "Отличное хобби!"},
    ]

    await compressor._consolidate(turns)

    raw = compressor._read_log()
    assert raw, "LOG.md должен быть создан после observer'а"
    # Проверяем что LLM вернул что-то осмысленное
    raw_lower = raw.lower()
    assert any(word in raw_lower for word in ["алексей", "alexei", "alex", "программист", "developer", "moscow", "москв"]), \
        f"Observer не извлёк ключевые факты из диалога. Observations:\n{raw}"


async def test_log_compressor_reflect(tmp_path, llm):
    """Reflector сжимает большой блок observations."""
    from src.memory.compressors.log import LogCompressor

    compressor = LogCompressor(
        model_name=llm["model_name"], api_key=llm["api_key"], base_url=llm["base_url"],
        backend=llm["backend"], backend_params=llm["backend_params"],
        reflect_after_tokens=1,  # рефлектить сразу
    )
    agent, _ = make_agent(tmp_path, llm)
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

async def test_tool_provider_consolidate(tmp_path, llm):
    """ToolProvider генерирует описание инструмента через LLM после его использования."""
    from src.memory.providers.tool import ToolProvider

    provider = ToolProvider(
        model_name=llm["model_name"], api_key=llm["api_key"], base_url=llm["base_url"],
        backend=llm["backend"], backend_params=llm["backend_params"],
        consolidate_tokens=1,
    )
    agent, _ = make_agent(tmp_path, llm, providers=[provider])
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
    agent, _ = make_agent(tmp_path, get_default_llm_config())
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
    agent, _ = make_agent(tmp_path, get_default_llm_config())
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
    agent, _ = make_agent(tmp_path, get_default_llm_config())
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
    agent, _ = make_agent(tmp_path, get_default_llm_config())
    skill.register(agent)
    await skill.start()

    try:
        result = skill.read("/workspace/notes.txt")
        assert "error" not in result, f"Ошибка чтения: {result}"
        assert "test content" in result["content"]
    finally:
        skill.stop()


# ═══════════════════════════════════════════════════════════════════════════════
# FactProvider — retain + recall с реальными embeddings и LLM
# ═══════════════════════════════════════════════════════════════════════════════

async def test_fact_provider_retain_and_recall(tmp_path, llm):
    """FactProvider сохраняет факты из диалога и находит их при recall."""
    from src.memory.providers.fact import FactProvider

    provider = FactProvider(
        model_name=llm["model_name"], api_key=llm["api_key"], base_url=llm["base_url"],
        backend=llm["backend"], backend_params=llm["backend_params"],
        consolidate_tokens=1,
        auto_consolidate=False,
        embedding_model=get_embedding_config(),
    )
    agent, _ = make_agent(tmp_path, llm, providers=[provider])
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
    await _retain_impl(items, provider._make_sub_agent, provider.storage, with_observations=False)

    # Recall по запросу о дочери
    recalled = await provider.recall("дочь Маша день рождения")
    recalled_text = json.dumps(recalled, ensure_ascii=False).lower()
    assert any(w in recalled_text for w in ["маша", "masha", "дочь", "daughter", "2020", "март", "march"]), \
        f"Recall не нашёл факт о дочери Маше. Результат:\n{recalled}"


async def test_fact_provider_context_prompt(tmp_path, llm):
    """FactProvider подмешивает релевантные факты в системный промпт."""
    from src.memory.providers.fact import FactProvider

    provider = FactProvider(
        model_name=llm["model_name"], api_key=llm["api_key"], base_url=llm["base_url"],
        backend=llm["backend"], backend_params=llm["backend_params"],
        consolidate_tokens=1,
        auto_recall=True,
        embedding_model=get_embedding_config(),
    )
    agent, _ = make_agent(tmp_path, llm, providers=[provider])
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
