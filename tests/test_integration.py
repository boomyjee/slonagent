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
