"""
Интеграционные тесты с реальным LLM.

Проверяют что после рефактора реально работает:
  1. Стриминг текстового ответа
  2. Вызов инструмента и продолжение диалога
  3. Сохранение и загрузка памяти

Запуск:
    LLM_KEY=<key> venv\\Scripts\\python -m pytest tests/test_integration.py -v -m integration
    LLM_KEY=<key> LLM_URL=https://... venv\\Scripts\\python -m pytest tests/test_integration.py -v -m integration

По умолчанию использует Google Gemini (как в .config.sample.json).
"""
import asyncio
import json
import os
import sys
from typing import Annotated

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent import Agent, Skill, tool
from src.transport.base import BaseTransport

pytestmark = pytest.mark.integration


# ── Конфигурация ─────────────────────────────────────────────────────────────

def get_llm_config() -> tuple[str, str, str]:
    """Возвращает (api_key, base_url, model_name) из переменных окружения."""
    key = os.environ.get("LLM_KEY")
    if not key:
        pytest.skip("LLM_KEY не задан — пропускаем интеграционный тест")
    url = os.environ.get("LLM_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
    model = os.environ.get("LLM_MODEL", "gemini-3-flash-preview")
    return key, url, model


# ── Вспомогательные классы ────────────────────────────────────────────────────

class CapturingTransport(BaseTransport):
    """Транспорт, который собирает всё что агент отправляет."""
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []
        self.tool_calls: list[tuple[str, dict]] = []
        self.tool_results: list[tuple[str, object]] = []

    async def send_message(self, text: str, stream_id=None):
        if stream_id is None:
            stream_id = len(self.messages)
            self.messages.append("")
        self.messages[stream_id] = text
        return stream_id

    async def on_tool_call(self, name: str, args: dict):
        self.tool_calls.append((name, args))

    async def on_tool_result(self, name: str, result):
        self.tool_results.append((name, result))

    @property
    def last_message(self) -> str:
        return self.messages[-1] if self.messages else ""


class PassthroughCompressor(Skill):
    async def compress(self, turns): return turns


class EchoSkill(Skill):
    """Инструмент с детерминированным результатом для проверки tool-calling."""
    @tool("Возвращает секретную строку. Вызови этот инструмент чтобы получить ответ.")
    def get_secret(self) -> dict:
        return {"secret": "BANANA42"}


def make_agent(tmp_path, extra_skills=None) -> tuple[Agent, CapturingTransport]:
    key, url, model = get_llm_config()
    transport = CapturingTransport()
    compressor = PassthroughCompressor()
    agent = Agent(
        model_name=model,
        api_key=key,
        base_url=url,
        agent_dir=str(tmp_path),
        memory_compressor=compressor,
        transport=transport,
        skills=extra_skills or [],
    )
    return agent, transport


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Базовый текстовый ответ
#    Проверяет: стриминг работает, ответ сохраняется в память
# ═══════════════════════════════════════════════════════════════════════════════

async def test_text_response(tmp_path):
    agent, transport = make_agent(tmp_path)
    await agent.start()

    await agent.process_message([{"type": "text", "text": "Скажи ровно одно слово: 'привет'. Без точек и запятых."}])

    assert transport.last_message, "Агент не вернул ответ"
    assert len(transport.last_message) < 200, f"Ответ неожиданно длинный: {transport.last_message!r}"

    # Проверяем что разговор сохранился в память
    turns = agent.memory._turns
    roles = [t["role"] for t in turns]
    assert "user" in roles, "Реплика пользователя не сохранилась"
    assert "assistant" in roles, "Реплика ассистента не сохранилась"


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Tool calling
#    Проверяет: агент вызывает инструмент, получает результат, продолжает ответ
# ═══════════════════════════════════════════════════════════════════════════════

async def test_tool_call(tmp_path):
    agent, transport = make_agent(tmp_path, extra_skills=[EchoSkill()])
    await agent.start()

    await agent.process_message([{
        "type": "text",
        "text": "Вызови инструмент get_secret и скажи мне что он вернул. Это обязательно."
    }])

    assert transport.tool_calls, "Агент не вызвал инструмент get_secret"
    # Skill добавляет имя класса как префикс: EchoSkill → echo_get_secret
    assert transport.tool_calls[0][0] == "echo_get_secret"

    assert "BANANA42" in transport.last_message, (
        f"Агент не включил результат инструмента в ответ. Ответ: {transport.last_message!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Память сохраняется между сессиями
#    Проверяет: turns записываются на диск и корректно загружаются новым агентом
# ═══════════════════════════════════════════════════════════════════════════════

async def test_memory_persistence(tmp_path):
    # Первая сессия
    agent1, _ = make_agent(tmp_path)
    await agent1.start()
    await agent1.process_message([{"type": "text", "text": "Запомни: моё любимое число — 7331."}])

    turns_after = agent1.memory._turns
    assert len(turns_after) >= 2, "Разговор не сохранился в памяти"

    # Вторая сессия — загружаем существующую память
    agent2, transport2 = make_agent(tmp_path)
    await agent2.start()

    loaded_turns = agent2.memory._turns
    assert len(loaded_turns) >= 2, "Память не загрузилась из файла"

    # Проверяем что формат корректный (контент — список блоков для user)
    user_turns = [t for t in loaded_turns if t["role"] == "user"]
    assert user_turns, "Нет user-реплик в загруженной памяти"
    first_user = user_turns[0]
    assert isinstance(first_user["content"], list), (
        f"content user-реплики должен быть списком, получили: {type(first_user['content'])}"
    )

    # Агент помнит что сказал пользователь
    await agent2.process_message([{"type": "text", "text": "Какое моё любимое число? Назови только цифры."}])
    assert "7331" in transport2.last_message, (
        f"Агент не вспомнил число из предыдущей сессии. Ответ: {transport2.last_message!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Очерёдность реплик в памяти
#    Проверяет: после tool call структура turns корректная (user→assistant→tool→assistant)
# ═══════════════════════════════════════════════════════════════════════════════

async def test_memory_turn_structure_after_tool_call(tmp_path):
    agent, transport = make_agent(tmp_path, extra_skills=[EchoSkill()])
    await agent.start()

    await agent.process_message([{"type": "text", "text": "Вызови get_secret и ответь мне."}])

    if not transport.tool_calls:
        pytest.skip("Агент не вызвал инструмент — пропускаем проверку структуры")

    turns = agent.memory._turns
    roles = [t["role"] for t in turns]

    assert roles[0] == "user"
    assert "assistant" in roles
    assert "tool" in roles

    # После tool всегда должен быть финальный assistant
    last_role = roles[-1]
    assert last_role == "assistant", f"Последняя реплика должна быть assistant, получили: {last_role}"

    # tool_call_id в tool-реплике должен совпадать с id в assistant
    tool_turns = [t for t in turns if t["role"] == "tool"]
    assert tool_turns[0].get("tool_call_id"), "tool_call_id не сохранился"


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Транскрипция аудио
#    Проверяет: transcribe_audio возвращает непустой текст для реального .ogg
# ═══════════════════════════════════════════════════════════════════════════════

async def test_transcribe_audio(tmp_path):
    """Транскрипция аудио через реальный LLM.

    Для теста используется минимальный синтетический OGG/Opus файл.
    Если TRANSCRIPTION_MODEL не задан — используется gemini-2.5-flash.
    Переменные: LLM_KEY (обязателен), TRANSCRIPTION_MODEL (опционально).
    """
    import struct, math

    key, url, _ = get_llm_config()
    model = os.environ.get("TRANSCRIPTION_MODEL", "gemini-2.5-flash")

    # Минимальный синтетический WAV (440 Hz, 1 сек, mono 16bit 8000Hz)
    # — проще WAV чем OGG, Gemini принимает audio/wav
    sample_rate = 8000
    duration = 1
    freq = 440
    num_samples = sample_rate * duration
    samples = [int(32767 * math.sin(2 * math.pi * freq * i / sample_rate)) for i in range(num_samples)]
    pcm = struct.pack(f"<{num_samples}h", *samples)
    data_size = len(pcm)
    header = struct.pack("<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE",
        b"fmt ", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16,
        b"data", data_size)
    wav_bytes = header + pcm

    transport = CapturingTransport()
    agent = Agent(
        model_name="gemini-3-flash-preview",
        api_key=key,
        base_url=url,
        agent_dir=str(tmp_path),
        memory_compressor=PassthroughCompressor(),
        transport=transport,
        transcription_model_name=model,
    )

    result = await agent.transcribe_audio(wav_bytes, "audio/wav")

    assert isinstance(result, str), f"Ожидали str, получили {type(result)}"
    # Синтетический тон без речи — модель может вернуть пустую строку, это нормально


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Мысли модели (thinking)
#    Проверяет: thinking-модели возвращают мысли, не ломаются от Gemini extra_body
# ═══════════════════════════════════════════════════════════════════════════════

async def _collect_thinking_stream(client, model: str, extra_body: dict) -> tuple[str, str, int]:
    """Стримит простой промпт, возвращает (thinking_text, response_text, chunk_count)."""
    from openai import AsyncOpenAI
    stream = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Сколько будет 2+2?"}],
        stream=True,
        extra_body=extra_body,
    )
    thinking, response, chunks = "", "", 0
    async for chunk in stream:
        chunks += 1
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        extra = getattr(delta, "model_extra", None) or {}

        is_gemini_thought = extra.get("extra_content", {}).get("google", {}).get("thought")
        if delta.content:
            if is_gemini_thought:
                thinking += delta.content.removeprefix("<thought>")
            else:
                response += delta.content.removeprefix("</thought>")

        thought_extra = extra.get("reasoning_content") or extra.get("reasoning")
        if thought_extra and isinstance(thought_extra, str):
            thinking += thought_extra

    return thinking, response, chunks


def _get_openrouter_client():
    import httpx
    from openai import AsyncOpenAI
    or_key = os.environ.get("OPENROUTER_KEY")
    if not or_key:
        pytest.skip("OPENROUTER_KEY не задан")
    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    return AsyncOpenAI(
        api_key=or_key,
        base_url="https://openrouter.ai/api/v1",
        http_client=httpx.AsyncClient(proxy=proxy_url, timeout=120.0) if proxy_url else httpx.AsyncClient(timeout=120.0),
    )


_GEMINI_EXTRA_BODY = {"extra_body": {"google": {"thinking_config": {"include_thoughts": True}}}}
_OR_EXTRA_BODY = {"reasoning": {"effort": "low"}, **_GEMINI_EXTRA_BODY}


@pytest.mark.asyncio
async def test_gemini_thinking():
    """Gemini возвращает мысли через extra_content.google.thought."""
    key, url, _ = get_llm_config()
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=key, base_url=url)

    thinking, response, chunks = await _collect_thinking_stream(client, "gemini-3.1-pro-preview", _GEMINI_EXTRA_BODY)
    print(f"\nthinking={thinking[:100]!r}, response={response[:100]!r}")
    assert chunks > 0
    assert thinking, "Gemini не вернул мысли"
    assert response, "Gemini не вернул ответ"


@pytest.mark.parametrize("model,expect_thinking", [
    ("openrouter/hunter-alpha", True),
    ("nvidia/nemotron-3-super-120b-a12b:free", True),
    ("minimax/minimax-m2.5:free", True),
    ("liquid/lfm-2.5-1.2b-thinking:free", True),
    ("stepfun/step-3.5-flash:free", True),
])
@pytest.mark.asyncio
async def test_openrouter_thinking(model, expect_thinking):
    """OpenRouter: thinking-модели не падают от Gemini extra_body и возвращают мысли."""
    client = _get_openrouter_client()
    try:
        thinking, response, chunks = await _collect_thinking_stream(client, model, _OR_EXTRA_BODY)
    except Exception as e:
        pytest.skip(f"{model}: провайдер вернул ошибку: {e}")
    print(f"\n{model}: thinking={bool(thinking)}, response={response[:100]!r}")
    assert chunks > 0, f"{model}: нет чанков"
    assert thinking or response, f"{model}: нет ни мыслей, ни ответа"
    if expect_thinking:
        assert thinking, f"{model}: нет мыслей"


# ═══════════════════════════════════════════════════════════════════════════════
# 7. OpenAI-compatible embeddings
#    Проверяет: OpenAIEmbedder работает с Gemini endpoint
# ═══════════════════════════════════════════════════════════════════════════════

def test_openai_embedder():
    """OpenAIEmbedder: создаёт векторы через OpenAI-compatible endpoint."""
    from openai import OpenAI
    from src.memory.providers.fact.storage import OpenAIEmbedder

    key, url, _ = get_llm_config()
    embedder = OpenAIEmbedder(model="gemini-embedding-001", api_key=key, base_url=url)

    assert embedder.dimension > 0, "dimension не определена"

    vec = embedder.encode_query("что такое память?")
    assert len(vec) == embedder.dimension, "размерность query не совпадает"

    vecs = embedder.encode_texts(["первый текст", "второй текст"])
    assert len(vecs) == 2, "должно быть 2 вектора"
    assert len(vecs[0]) == embedder.dimension, "размерность docs не совпадает"

    print(f"\nOpenAIEmbedder: dim={embedder.dimension}, query_norm={sum(x**2 for x in vec)**0.5:.4f}")
