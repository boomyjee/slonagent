"""
Тесты рефакторинга OpenAI-совместимого API.

Запуск:
    .venv\\Scripts\\python -m pytest tests/test_openai_refactor.py -v
"""
import asyncio
import json
import os
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)


from agent import Skill


class PassthroughCompressor(Skill):
    """Stub-компрессор для тестов — возвращает туры без изменений."""
    async def compress(self, turns): return turns



# ── helpers ───────────────────────────────────────────────────────────────────

def make_openai_response(content: str = None, tool_calls: list = None):
    """Создаёт mock-объект OpenAI ChatCompletion."""
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls or []

    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = "stop"

    resp = MagicMock()
    resp.choices = [choice]
    return resp


def make_stream_chunk(text: str = None, tool_name: str = None, tool_id: str = None, tool_args: str = None):
    """Создаёт один chunk для стриминга."""
    delta = MagicMock()
    delta.content = text
    delta.tool_calls = []
    delta.model_fields_set = set()  # предотвращает предупреждения о неизвестных полях

    if tool_name is not None:
        tc = MagicMock()
        tc.index = 0
        tc.id = tool_id
        tc.function = MagicMock()
        tc.function.name = tool_name
        tc.function.arguments = tool_args or ""
        delta.tool_calls = [tc]
        delta.model_fields_set = {"tool_calls"}

    if text is not None:
        delta.model_fields_set = (delta.model_fields_set or set()) | {"content"}

    choice = MagicMock()
    choice.delta = delta

    chunk = MagicMock()
    chunk.choices = [choice]
    return chunk


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Чистые функции — agent.py
# ═══════════════════════════════════════════════════════════════════════════════



from agent import Agent


class TestStripContentsPrivate:

    def test_strips_turn_level_private(self):
        # без _timestamp — content не меняется
        turns = [{"role": "user", "content": "hi", "_secret": "x"}]
        result = Agent.strip_contents_private(turns)
        assert "_secret" not in result[0]
        assert result[0]["content"] == "hi"

    def test_strips_content_block_private(self):
        turns = [{"role": "user", "content": [{"type": "text", "text": "doc", "_document_id": "x"}]}]
        result = Agent.strip_contents_private(turns)
        block = result[0]["content"][0]
        assert "_document_id" not in block
        assert block["text"] == "doc"

    def test_injects_timestamp_into_list_content(self):
        turns = [{"role": "user", "content": [{"type": "text", "text": "hi"}], "_timestamp": "2024-06-01T12:00:00"}]
        result = Agent.strip_contents_private(turns)
        assert result[0]["content"][0]["type"] == "text"
        assert "2024-06-01" in result[0]["content"][0]["text"]

    def test_no_timestamp_no_inject(self):
        turns = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
        result = Agent.strip_contents_private(turns)
        assert result[0]["content"] == [{"type": "text", "text": "hello"}]

    def test_assistant_turn_passthrough(self):
        turns = [{"role": "assistant", "content": "ok", "_timestamp": "x"}]
        result = Agent.strip_contents_private(turns)
        assert result[0]["content"] == "ok"
        assert "_timestamp" not in result[0]


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Чистые функции — memory.py
# ═══════════════════════════════════════════════════════════════════════════════

from src.memory.memory import _migrate_gemini_turns


class TestMigrateGeminiTurns:

    def test_plain_user_text(self):
        turns = [{"role": "user", "parts": [{"text": "hello"}]}]
        result = _migrate_gemini_turns(turns)
        assert result[0]["role"] == "user"
        assert result[0]["content"] == [{"type": "text", "text": "hello"}]
        assert "parts" not in result[0]

    def test_model_text(self):
        turns = [{"role": "model", "parts": [{"text": "world"}]}]
        result = _migrate_gemini_turns(turns)
        assert result[0]["role"] == "assistant"
        assert result[0]["content"] == "world"

    def test_function_call_and_response(self):
        turns = [
            {"role": "model", "parts": [{"function_call": {"name": "my_tool", "args": {"x": 1}}}]},
            {"role": "user",  "parts": [{"function_response": {"name": "my_tool", "response": {"result": "ok"}}}]},
        ]
        result = _migrate_gemini_turns(turns)

        # assistant turn with tool_calls
        assert result[0]["role"] == "assistant"
        assert result[0]["content"] is None
        tc = result[0]["tool_calls"][0]
        assert tc["function"]["name"] == "my_tool"
        assert json.loads(tc["function"]["arguments"]) == {"x": 1}

        # tool response turn
        assert result[1]["role"] == "tool"
        assert result[1]["tool_call_id"] == tc["id"]
        assert json.loads(result[1]["content"])["result"] == "ok"

    def test_already_openai_format_passthrough(self):
        turns = [{"role": "user", "content": "hi"}]
        result = _migrate_gemini_turns(turns)
        assert result == turns

    def test_preserves_private_fields(self):
        turns = [{"role": "user", "parts": [{"text": "hello"}], "_timestamp": "2024-01-01"}]
        result = _migrate_gemini_turns(turns)
        assert result[0]["_timestamp"] == "2024-01-01"

    def test_thought_signature_migrated_to_tool_calls(self):
        """thought_signature из Gemini-формата переносится в extra_content каждого tool_call."""
        turns = [{
            "role": "model",
            "parts": [{"function_call": {"name": "my_tool", "args": {}}}],
            "thought_signature": "SIGNATURE_XYZ",
        }]
        result = _migrate_gemini_turns(turns)
        tc = result[0]["tool_calls"][0]
        assert tc.get("extra_content", {}).get("google", {}).get("thought_signature") == "SIGNATURE_XYZ"

    def test_no_thought_signature_no_extra_content(self):
        """Если thought_signature нет — extra_content в tool_call не добавляется."""
        turns = [{"role": "model", "parts": [{"function_call": {"name": "my_tool", "args": {}}}]}]
        result = _migrate_gemini_turns(turns)
        tc = result[0]["tool_calls"][0]
        assert "extra_content" not in tc


from src.memory.memory import Memory


class TestCountTokens:

    def test_string_content(self):
        turns = [{"role": "user", "content": "hello world"}]
        n = Memory.count_tokens(turns)
        assert n > 0

    def test_list_content(self):
        turns = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
        n = Memory.count_tokens(turns)
        assert n > 0

    def test_image_block(self):
        turns = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:..."}}]}]
        n = Memory.count_tokens(turns)
        assert n == 258

    def test_tool_calls(self):
        turns = [{"role": "assistant", "content": None, "tool_calls": [
            {"function": {"name": "foo", "arguments": '{"x": 1}'}}
        ]}]
        n = Memory.count_tokens(turns)
        assert n > 0

    def test_empty(self):
        assert Memory.count_tokens([]) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Agent loop — с mock-клиентом
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgentLoop:

    def _make_agent(self, tmp_path):
        from agent import Agent
        agent = Agent(
            model_name="test-model",
            api_key="test-key",
            base_url="http://test",
            agent_dir=str(tmp_path),
            memory_compressor=PassthroughCompressor(),
        )
        return agent

    def _mock_transport(self):
        """Создаёт полный async-мок transport."""
        t = MagicMock()
        t.send_message = AsyncMock(return_value=None)
        t.send_system_prompt = AsyncMock(return_value=None)
        t.on_tool_call = AsyncMock(return_value=None)
        t.on_tool_result = AsyncMock(return_value=None)
        return t

    def _mock_stream(self, chunks: list):
        """Создаёт async итератор из списка chunks."""
        async def aiter():
            for chunk in chunks:
                yield chunk
        mock = MagicMock()
        mock.__aiter__ = lambda self: aiter()
        mock.__aenter__ = AsyncMock(return_value=mock)
        mock.__aexit__ = AsyncMock(return_value=False)
        return mock

    @pytest.mark.asyncio(loop_scope="function")
    async def test_simple_text_response(self, tmp_path):
        agent = self._make_agent(tmp_path)

        chunks = [
            make_stream_chunk(text="Hello"),
            make_stream_chunk(text=" world"),
        ]
        agent.client.chat.completions.create = AsyncMock(return_value=self._mock_stream(chunks))

        responses = []
        transport = self._mock_transport()
        transport.send_message = AsyncMock(side_effect=lambda text, stream_id=None: responses.append(text))
        agent.transport = transport

        await agent.start()
        await agent.process_message(content_parts=[{"text": "hi"}])

        # Агент должен был ответить
        assert responses, "Агент не отправил ответ"
        full_text = " ".join(str(r) for r in responses)
        assert "Hello" in full_text or "world" in full_text

        # В памяти должны быть user + assistant тёрны
        turns = agent.memory._turns
        roles = [t.get("role") for t in turns if isinstance(t, dict)]
        assert "user" in roles
        assert "assistant" in roles

    @pytest.mark.asyncio(loop_scope="function")
    async def test_tool_call_and_response(self, tmp_path):
        from typing import Annotated
        from agent import Agent, tool

        agent = self._make_agent(tmp_path)

        # Регистрируем тестовый инструмент
        tool_was_called = []

        class TestSkill:
            agent = None
            _tools = []
            _tool_map = {}
            _bypass_handlers = {}
            _bypass_descriptions = {}
            _bypass_standalone = set()

            def register(self, a): self.agent = a
            async def start(self): pass
            async def get_context_prompt(self, user_text=""): return ""
            async def get_tool_prompt(self, name): return ""
            def get_tools(self): return [{
                "type": "function",
                "function": {
                    "name": "testskill_ping",
                    "description": "test",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                }
            }]
            def get_bypass_commands(self, standalone_only=False): return {}
            def is_bypass_command(self, text): return False
            async def dispatch_bypass(self, text): return ""
            async def dispatch_tool_call(self, tc):
                tool_was_called.append(tc["function"]["name"])
                return {"result": "pong"}, []

        skill = TestSkill()
        agent.skills.append(skill)
        skill.register(agent)

        # Первый вызов LLM — возвращает tool call
        tc_chunk = make_stream_chunk(tool_name="testskill_ping", tool_id="call_1", tool_args="{}")
        tc_chunk.choices[0].delta.content = None
        # Последний chunk без данных — конец стрима
        end_chunk = make_stream_chunk()

        # Второй вызов — возвращает текст
        text_chunk = make_stream_chunk(text="Done!")
        end_chunk2 = make_stream_chunk()

        call_count = 0
        def make_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return self._mock_stream([tc_chunk, end_chunk])
            return self._mock_stream([text_chunk, end_chunk2])

        agent.client.chat.completions.create = AsyncMock(side_effect=make_stream)

        responses = []
        transport = self._mock_transport()
        transport.send_message = AsyncMock(side_effect=lambda text, stream_id=None: responses.append(text))
        agent.transport = transport

        await agent.start()
        await agent.process_message(content_parts=[{"text": "ping"}])

        assert "testskill_ping" in tool_was_called, "Инструмент не был вызван"

        # В памяти: user, assistant(tool_calls), tool, assistant(text)
        turns = agent.memory._turns
        roles = [t.get("role") for t in turns if isinstance(t, dict)]
        assert roles.count("tool") >= 1, f"Нет tool тёрна в памяти: {roles}"
        assert "assistant" in roles


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Memory.add_turn / load_turns_json
# ═══════════════════════════════════════════════════════════════════════════════

class TestMemoryStorage:

    @pytest.mark.asyncio(loop_scope="function")
    async def test_add_and_persist(self, tmp_path):
        from src.memory.memory import Memory
        m = Memory(compressor=PassthroughCompressor(), memory_dir=str(tmp_path))

        await m.add_turn({"role": "user", "content": "hello", "_timestamp": "2024-01-01T00:00:00"})
        await m.add_turn({"role": "assistant", "content": "hi there", "_timestamp": "2024-01-01T00:00:01"})

        # Файл должен быть записан (assistant trigger)
        state_file = os.path.join(str(tmp_path), "CONTEXT.json")
        assert os.path.exists(state_file)

        m2 = Memory(compressor=PassthroughCompressor(), memory_dir=str(tmp_path))
        turns = m2._turns
        assert any(t.get("content") == "hello" for t in turns)
        assert any(t.get("content") == "hi there" for t in turns)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_migrates_gemini_format_on_load(self, tmp_path):
        from src.memory.memory import Memory, save_turns_json

        state_file = os.path.join(str(tmp_path), "CONTEXT.json")
        gemini_turns = [
            {"role": "user",  "parts": [{"text": "old message"}]},
            {"role": "model", "parts": [{"text": "old reply"}]},
        ]
        save_turns_json(state_file, gemini_turns)

        m = Memory(compressor=PassthroughCompressor(), memory_dir=str(tmp_path))
        turns = m._turns
        roles = [t.get("role") for t in turns]
        assert "model" not in roles, "Gemini role 'model' не был мигрирован"
        assert "assistant" in roles
        assert any(t.get("content") == [{"type": "text", "text": "old message"}] for t in turns)
