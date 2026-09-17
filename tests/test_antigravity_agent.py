"""Тесты AntigravityBackend:
- вырезание нативных инструкций Antigravity (--disable-slash-commands, _FORBIDDEN_NATIVE_TOOLS, override envelope)
- оборачивание скиллов Слона и вычисление fingerprint
- стриминг дельт текста, мыслей (thinking) и событий вызова тулов
- подавление запрещённых нативных тулов
- синхронизация контекста (context synchronization) и обнаружение расхождения (divergence detection)
- сохранение и возобновление conversation_id сессии
- обработка прерывания (cancellation)
- интеграционный тест с реальным agy.exe по подписке Google Antigravity

Запуск:
    .venv\\Scripts\\python -m pytest tests/test_antigravity_agent.py -v
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent import Skill, tool
from src.agent.backends.antigravity import (
    AntigravityBackend,
    AntigravityClient,
    _FORBIDDEN_NATIVE_TOOLS,
    _build_base_instructions,
)


class PassthroughCompressor(Skill):
    async def compress(self, turns):
        return turns


def make_agent(skills=None, model_name: str = "gemini-3.8-flash-high", sdk_options=None, agent_dir=None):
    from src.agent.agent import Agent
    backend_params = {}
    if sdk_options is not None:
        backend_params["sdk_options"] = sdk_options
    agent = Agent(
        id="test",
        model_name=model_name,
        backend="antigravity",
        backend_params=backend_params or None,
        agent_dir=agent_dir or tempfile.mkdtemp(),
        memory_compressor=PassthroughCompressor(),
        skills=skills or [],
    )
    agent.transport = MagicMock()
    agent.transport.send_message = AsyncMock()
    agent.transport.send_thinking = AsyncMock()
    agent.transport.on_tool_call = AsyncMock()
    agent.transport.on_tool_result = AsyncMock()
    agent.transport.send_processing = AsyncMock()
    agent.transport.send_memory_info = AsyncMock()
    agent.transport.send_system_prompt = AsyncMock()
    agent.transport.close = AsyncMock()
    return agent


# ═══════════════════════════════════════════════════════════════════════════════
# Unit: State file & Session Persistence
# ═══════════════════════════════════════════════════════════════════════════════

class TestStateFile:

    def test_load_empty_returns_dict(self):
        agent = make_agent()
        assert agent.backend_impl._load_state() == {}

    def test_save_then_load(self):
        agent = make_agent()
        agent.backend_impl._save_state({"conversation_id": "conv_12345"})
        assert agent.backend_impl._load_state() == {"conversation_id": "conv_12345"}

    def test_load_invalid_json_returns_empty(self):
        agent = make_agent()
        with open(agent.backend_impl._state_file, "w", encoding="utf-8") as f:
            f.write("invalid json content")
        assert agent.backend_impl._load_state() == {}

    def test_ephemeral_state_in_memory(self):
        from src.agent.agent import Agent
        agent = Agent(id="eph", model_name="gemini-3.8-flash-high", backend="antigravity")
        b = agent.backend_impl
        assert b._state_file is None
        b._save_state({"conversation_id": "conv_eph"})
        assert b._load_state() == {"conversation_id": "conv_eph"}


# ═══════════════════════════════════════════════════════════════════════════════
# Unit: Fingerprint & Tool Wrapping
# ═══════════════════════════════════════════════════════════════════════════════

class _SkillA(Skill):
    @tool("Первый тестовый тул")
    async def hello(self, name: str = "x"):
        return {"greeting": f"hello {name}"}


class _SkillB(Skill):
    @tool("Второй тестовый тул")
    async def world(self):
        return {"ok": True}


class TestToolWrappingAndFingerprint:

    def test_same_skills_same_fp(self):
        a1 = make_agent(skills=[_SkillA()])
        a2 = make_agent(skills=[_SkillA()])
        assert a1.backend_impl._skills_fingerprint() == a2.backend_impl._skills_fingerprint()

    def test_different_skills_different_fp(self):
        a1 = make_agent(skills=[_SkillA()])
        a2 = make_agent(skills=[_SkillA(), _SkillB()])
        assert a1.backend_impl._skills_fingerprint() != a2.backend_impl._skills_fingerprint()

    def test_build_tools_creates_tool_with_schema(self):
        agent = make_agent(skills=[_SkillA()])
        tools = agent.backend_impl._build_tools()
        assert len(tools) == 1
        t = tools[0]
        assert t.__name__ == "_skilla_hello"
        assert t.__doc__ == "Первый тестовый тул"
        assert "properties" in t.input_schema

    @pytest.mark.asyncio
    async def test_tool_execution_dispatches_to_slon_skill(self):
        skill = _SkillA()
        agent = make_agent(skills=[skill])
        tools = agent.backend_impl._build_tools()
        handler = tools[0]

        result = await handler(name="world")
        assert json.loads(result) == {"greeting": "hello world"}


# ═══════════════════════════════════════════════════════════════════════════════
# Unit: Instructions & Capability Stripping
# ═══════════════════════════════════════════════════════════════════════════════

class TestStrippingConfiguration:

    def test_forbidden_tools_list_covers_all_native_tools(self):
        assert len(_FORBIDDEN_NATIVE_TOOLS) >= 15
        assert "run_command" in _FORBIDDEN_NATIVE_TOOLS
        assert "list_dir" in _FORBIDDEN_NATIVE_TOOLS
        assert "view_file" in _FORBIDDEN_NATIVE_TOOLS
        assert "write_to_file" in _FORBIDDEN_NATIVE_TOOLS
        assert "replace_file_content" in _FORBIDDEN_NATIVE_TOOLS
        assert "invoke_subagent" in _FORBIDDEN_NATIVE_TOOLS

    def test_base_instructions_contain_override_and_forbidden_list(self):
        instr = _build_base_instructions()
        assert "CRITICAL SYSTEM OVERRIDE" in instr
        assert "FORBIDDEN NATIVE TOOLS" in instr
        assert "- run_command" in instr
        assert "- replace_file_content" in instr

    def test_prompt_payload_builder_includes_overrides(self):
        agent = make_agent(skills=[_SkillA()])
        backend = agent.backend_impl
        payload = backend._build_prompt_payload(
            append_text="System instructions text",
            user_text="User request text",
        )
        assert "CRITICAL SYSTEM OVERRIDE" in payload
        assert "FORBIDDEN NATIVE TOOLS" in payload
        assert "[System Instructions]\nSystem instructions text" in payload
        assert "Available SlonAgent Tools:" in payload
        assert "_skilla_hello" in payload
        assert "[User Request]\nUser request text" in payload

    def test_client_args_include_disable_flags(self):
        client = AntigravityClient(
            agy_path="agy.exe",
            model="gemini-3.8-flash-high",
            disable_slash_commands=True,
            dangerously_skip_permissions=True,
        )
        assert client._disable_slash_commands is True
        assert client._dangerously_skip_permissions is True


# ═══════════════════════════════════════════════════════════════════════════════
# Unit: Streaming Events & Multi-Turn Conversion
# ═══════════════════════════════════════════════════════════════════════════════

class TestStreaming:

    @pytest.mark.asyncio
    async def test_streaming_thoughts_text_and_tools(self):
        agent = make_agent(skills=[_SkillA()])
        agent.memory._turns.append({"role": "user", "content": "Тестовый запрос"})

        class MockAntigravityClient:
            def __init__(self, **kwargs):
                self.conversation_id = "test_conv_abc"
                self.is_alive = True

            async def chat(self, prompt):
                # 1. Стрим рассуждений (thinking)
                yield {
                    "event": "step_update",
                    "step_update": {
                        "state": "ACTIVE",
                        "step_type": "agent_response",
                        "thinking_delta": "Думаю...",
                    },
                }
                yield {
                    "event": "step_update",
                    "step_update": {
                        "state": "ACTIVE",
                        "step_type": "agent_response",
                        "thinking_delta": " продолжение мысли",
                    },
                }
                # 2. Вызов инструмента
                yield {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 1,
                        "state": "ACTIVE",
                        "step_type": "tool",
                        "tool_name": "_skilla_hello",
                        "tool_info": {"parameters": {"name": "test"}},
                    },
                }
                yield {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 1,
                        "state": "DONE",
                        "step_type": "tool",
                        "tool_name": "_skilla_hello",
                        "tool_info": {"parameters": {"name": "test"}, "output": '{"greeting": "hello test"}'},
                    },
                }
                # 3. Ответ модели
                yield {
                    "event": "step_update",
                    "step_update": {
                        "state": "ACTIVE",
                        "step_type": "agent_response",
                        "text_delta": "Готово! Всё сделано.",
                    },
                }
                # 4. Финал
                yield {
                    "event": "result",
                    "result": {
                        "conversation_id": "test_conv_abc",
                        "status": "SUCCESS",
                        "response": "Готово! Всё сделано.",
                        "usage": {"input_tokens": 100, "output_tokens": 50},
                    },
                }

            async def close(self):
                self.is_alive = False

        with patch("src.agent.backends.antigravity.AntigravityClient", MockAntigravityClient):
            turns = await agent.llm()

        # Проверка отправки thinking в транспорт
        assert agent.transport.send_thinking.call_count >= 2
        assert any(c.args and "Думаю..." in c.args[0] for c in agent.transport.send_thinking.call_args_list)

        # Проверка отправки текста в транспорт
        assert agent.transport.send_message.call_count >= 1
        assert any(c.args and "Готово! Всё сделано." in c.args[0] and c.kwargs.get("final") is True
                   for c in agent.transport.send_message.call_args_list)

        # Проверка tool call/result событий
        agent.transport.on_tool_call.assert_called_with("_skilla_hello", {"name": "test"})
        agent.transport.on_tool_result.assert_called_with("_skilla_hello", '{"greeting": "hello test"}')

        # Проверка результирующих turn'ов
        assert len(turns) == 3
        assert turns[0]["role"] == "assistant"
        assert "tool_calls" in turns[0]
        assert turns[1]["role"] == "tool"
        assert turns[1]["name"] == "_skilla_hello"
        assert turns[2]["role"] == "assistant"
        assert turns[2]["content"] == "Готово! Всё сделано."

        # Проверка сохранения conversation_id
        assert agent.backend_impl._load_state().get("conversation_id") == "test_conv_abc"

    @pytest.mark.asyncio
    async def test_forbidden_native_tool_is_suppressed(self):
        agent = make_agent()
        agent.memory._turns.append({"role": "user", "content": "Попробуй вызвать нативный тул"})

        class MockAntigravityClient:
            def __init__(self, **kwargs):
                self.conversation_id = "test_suppress"
                self.is_alive = True

            async def chat(self, prompt):
                # Нативный тул, запрещённый политикой
                yield {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 1,
                        "state": "ACTIVE",
                        "step_type": "tool",
                        "tool_name": "list_dir",
                        "tool_info": {"parameters": {"DirectoryPath": "."}},
                    },
                }
                yield {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 1,
                        "state": "DONE",
                        "step_type": "tool",
                        "tool_name": "list_dir",
                        "tool_info": {"output": "secret_file.txt"},
                    },
                }
                yield {
                    "event": "step_update",
                    "step_update": {
                        "state": "ACTIVE",
                        "step_type": "agent_response",
                        "text_delta": "Ответ без тула",
                    },
                }
                yield {
                    "event": "result",
                    "result": {
                        "conversation_id": "test_suppress",
                        "status": "SUCCESS",
                        "response": "Ответ без тула",
                    },
                }

            async def close(self):
                self.is_alive = False

        with patch("src.agent.backends.antigravity.AntigravityClient", MockAntigravityClient):
            turns = await agent.llm()

        # list_dir не должен попасть в transport
        agent.transport.on_tool_call.assert_not_called()
        agent.transport.on_tool_result.assert_not_called()

        # И не должен попасть в turns
        assert len(turns) == 1
        assert turns[0]["role"] == "assistant"
        assert turns[0]["content"] == "Ответ без тула"


# ═══════════════════════════════════════════════════════════════════════════════
# Unit: Cancellation Handling
# ═══════════════════════════════════════════════════════════════════════════════

class TestCancellation:

    @pytest.mark.asyncio
    async def test_cancelled_turn_adds_synthetic_results(self):
        agent = make_agent(skills=[_SkillA()])
        agent.memory._turns.append({"role": "user", "content": "Долгий запрос"})

        class HangingClient:
            def __init__(self, **kwargs):
                self.conversation_id = "test_cancel"
                self.is_alive = True

            async def chat(self, prompt):
                yield {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 1,
                        "state": "ACTIVE",
                        "step_type": "tool",
                        "tool_name": "_skilla_hello",
                        "tool_info": {"parameters": {}},
                    },
                }
                raise asyncio.CancelledError()

            async def close(self):
                self.is_alive = False

        with patch("src.agent.backends.antigravity.AntigravityClient", HangingClient):
            turns = await agent.llm()

        # Проверяем, что висящий tool_call закрыт синтетическим ответом
        tool_results = [t for t in turns if t.get("role") == "tool"]
        assert len(tool_results) == 1
        assert tool_results[0]["content"] == "[прервано пользователем]"

        assert turns[-1]["role"] == "assistant"
        assert turns[-1]["content"] == "[ответ прерван пользователем]"


# ═══════════════════════════════════════════════════════════════════════════════
# Unit: Context Synchronization & Divergence Detection
# ═══════════════════════════════════════════════════════════════════════════════

class TestContextSynchronization:

    def test_stable_memory_turns_excludes_trailing_user_turns(self):
        turns = [
            {"role": "user", "content": "Привет"},
            {"role": "assistant", "content": "Здравствуйте!"},
            {"role": "user", "content": "Запрос 1"},
            {"role": "user", "content": "Запрос 2"},
        ]
        stable = AntigravityBackend._stable_memory_turns(turns)
        assert len(stable) == 2
        assert stable[0]["content"] == "Привет"
        assert stable[1]["content"] == "Здравствуйте!"

    def test_memory_signatures_extraction(self):
        turns = [
            {"role": "user", "content": "Вопрос"},
            {"role": "assistant", "content": "Ответ", "tool_calls": [{
                "id": "c1",
                "function": {"name": "test_tool", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": "c1", "name": "test_tool", "content": "result"},
            {"role": "assistant", "content": "Финальный текст"},
        ]
        sigs = AntigravityBackend._memory_signatures(turns)
        assert ("message", "user", "Вопрос") in sigs
        assert ("tool_call", "test_tool") in sigs
        assert ("tool_output", "c1") in sigs
        assert ("message", "assistant", "Финальный текст") in sigs

    def test_transcript_signatures_parsing(self, tmp_path):
        transcript = tmp_path / "transcript.jsonl"
        lines = [
            json.dumps({"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
                        "content": "<USER_REQUEST>\nПривет из transcript\n</USER_REQUEST>"}),
            json.dumps({"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE",
                        "tool_calls": [{"name": "my_skill_tool"}]}),
            json.dumps({"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE",
                        "content": "Ответ модели из transcript"}),
        ]
        transcript.write_text("\n".join(lines), encoding="utf-8")

        sigs = AntigravityBackend._transcript_signatures(str(transcript))
        assert ("message", "user", "Привет из transcript") in sigs
        assert ("tool_call", "my_skill_tool") in sigs
        assert ("message", "assistant", "Ответ модели из transcript") in sigs

    @pytest.mark.asyncio
    async def test_divergence_detected_when_memory_is_pruned(self):
        agent = make_agent()
        backend = agent.backend_impl

        agent.memory._turns = [
            {"role": "user", "content": "Недавний вопрос"},
            {"role": "assistant", "content": "Недавний ответ"},
        ]

        backend._agy_signatures = MagicMock(return_value={
            ("message", "user", "Старый забытый вопрос"),
            ("message", "assistant", "Старый забытый ответ"),
            ("message", "user", "Недавний вопрос"),
            ("message", "assistant", "Недавний ответ"),
        })

        assert await backend._conversation_has_diverged() is True

    @pytest.mark.asyncio
    async def test_no_divergence_when_memory_matches_trajectory(self):
        agent = make_agent()
        backend = agent.backend_impl

        agent.memory._turns = [
            {"role": "user", "content": "Вопрос"},
            {"role": "assistant", "content": "Ответ"},
        ]

        backend._agy_signatures = MagicMock(return_value={
            ("message", "user", "Вопрос"),
            ("message", "assistant", "Ответ"),
        })

        assert await backend._conversation_has_diverged() is False

    @pytest.mark.asyncio
    async def test_divergence_triggers_fresh_session_and_notifies_transport(self):
        agent = make_agent()
        backend = agent.backend_impl

        backend._save_state({"conversation_id": "old_session_123", "created": True})
        agent.memory._turns = [
            {"role": "user", "content": "Новый вопрос"},
        ]

        backend._agy_signatures = MagicMock(return_value={
            ("message", "user", "Старый вопрос до компрессии"),
        })

        created_cids = []

        class MockAntigravityClient:
            def __init__(self, conversation_id=None, **kwargs):
                self.conversation_id = conversation_id or "new_session_456"
                self.is_alive = True
                created_cids.append(self.conversation_id)

            async def chat(self, prompt):
                yield {
                    "event": "step_update",
                    "step_update": {
                        "state": "ACTIVE",
                        "step_type": "agent_response",
                        "text_delta": "Свежий ответ",
                    },
                }
                yield {
                    "event": "result",
                    "result": {
                        "conversation_id": self.conversation_id,
                        "status": "SUCCESS",
                        "response": "Свежий ответ",
                    },
                }

            async def close(self):
                self.is_alive = False

        with patch("src.agent.backends.antigravity.AntigravityClient", MockAntigravityClient):
            turns = await agent.llm()

        assert len(created_cids) >= 1
        new_cid = created_cids[-1]
        assert new_cid != "old_session_123"

        agent.transport.send_memory_info.assert_called()
        call_msg = agent.transport.send_memory_info.call_args[0][0]
        assert "Синхронизировал antigravity-сессию" in call_msg
        assert "память разошлась" in call_msg

        assert backend._load_state().get("conversation_id") == new_cid


# ═══════════════════════════════════════════════════════════════════════════════
# Integration: Реальная сессия Antigravity по подписке (без ключей)
# ═══════════════════════════════════════════════════════════════════════════════

class _CalcSkill(Skill):
    """Используется в integration-тестах для проверки реального tool-call'а."""

    def __init__(self):
        super().__init__()
        self.calls: list[dict] = []

    @tool("Сложить два числа. ОБЯЗАТЕЛЬНО используй эту тулзу, не считай в уме.")
    async def add(self, a: int, b: int) -> dict:
        self.calls.append({"a": a, "b": b})
        return {"sum": a + b}


class TestIntegrationAntigravity:

    @pytest.mark.asyncio
    async def test_real_antigravity_ping_pong(self):
        # Если agy.exe не найден в системе — пропускаем
        from src.agent.backends.antigravity import _find_antigravity
        try:
            agy_bin = _find_antigravity()
            if agy_bin == "agy" and not shutil.which("agy"):
                pytest.skip("agy CLI не найден в системе")
        except Exception:
            pytest.skip("agy CLI не найден в системе")

        agent = make_agent(model_name=os.environ.get("ANTIGRAVITY_MODEL", "gemini-3.8-flash-high"))
        agent.memory._turns.append({"role": "user", "content": "Reply with exactly one word: pong"})
        try:
            turns = await agent.llm()
            assert len(turns) >= 1
            content = turns[-1]["content"].lower()
            assert "pong" in content
        finally:
            await agent.backend_impl.close()

    @pytest.mark.asyncio
    async def test_real_tool_call_through_mcp(self):
        """Реальный agy.exe должен вызвать наш MCP-tool через slon-сервер,
        и мы получим tool_call/tool_result в transport + соответствующие turn'ы."""
        from src.agent.backends.antigravity import _find_antigravity
        try:
            agy_bin = _find_antigravity()
            if agy_bin == "agy" and not shutil.which("agy"):
                pytest.skip("agy CLI не найден в системе")
        except Exception:
            pytest.skip("agy CLI не найден в системе")

        calc = _CalcSkill()
        agent = make_agent(skills=[calc], model_name=os.environ.get("ANTIGRAVITY_MODEL", "gemini-3.8-flash-high"))
        agent.memory._turns.append({
            "role": "user",
            "content": "Используй тулзу add чтобы сложить 17 и 25. Верни только результат.",
        })

        try:
            turns = await agent.llm()
        finally:
            await agent.backend_impl.close()

        # Скилл получил вызов с правильными аргументами
        assert calc.calls, f"тул не был вызван, turns={turns}"
        assert calc.calls[0] == {"a": 17, "b": 25}

        # Turn'ы содержат tool_calls и tool результат
        tool_use_turns = [t for t in turns if t.get("tool_calls")]
        tool_result_turns = [t for t in turns if t.get("role") == "tool"]
        assert tool_use_turns, f"нет turn с tool_calls, turns={turns}"
        assert tool_result_turns, f"нет turn с role:tool, turns={turns}"

        # Транспорт получил оба события
        agent.transport.on_tool_call.assert_called()
        agent.transport.on_tool_result.assert_called()
        tool_call = agent.transport.on_tool_call.call_args
        assert "add" in tool_call.args[0]
        assert tool_call.args[1] == {"a": 17, "b": 25}
