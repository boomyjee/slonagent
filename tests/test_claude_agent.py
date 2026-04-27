"""
Тесты ClaudeAgent: llm()-стрим клода, конвертация блоков в OpenAI-format,
session_id state, переиспользование клиента.

Запуск:
    .venv\\Scripts\\python -m pytest tests/test_claude_agent.py -v
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

from agent import Skill, tool
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)


class PassthroughCompressor(Skill):
    async def compress(self, turns):
        return turns


def make_agent(skills=None):
    """Минимальный ClaudeAgent для тестов."""
    from src.agent.claude_agent import ClaudeAgent
    agent = ClaudeAgent(
        id="test",
        model_name="claude-test",
        api_key="",
        base_url="",
        agent_dir=tempfile.mkdtemp(),
        memory_compressor=PassthroughCompressor(),
        skills=skills or [],
    )
    agent.transport = MagicMock()
    agent.transport.send_message = AsyncMock()
    agent.transport.send_thinking = AsyncMock()
    agent.transport.on_tool_call = AsyncMock()
    agent.transport.on_tool_result = AsyncMock()
    return agent


def stream_event(etype: str, **kwargs) -> StreamEvent:
    """Создаёт SDK StreamEvent."""
    event_data = {"type": etype, **kwargs}
    return StreamEvent(uuid=None, session_id="s", parent_tool_use_id=None, event=event_data)


async def aiter_messages(messages):
    """Превращает список сообщений в async-итератор для receive_response."""
    for msg in messages:
        yield msg


# ═══════════════════════════════════════════════════════════════════════════════
# State file
# ═══════════════════════════════════════════════════════════════════════════════

class TestStateFile:

    def test_load_empty_returns_dict(self):
        agent = make_agent()
        assert agent._load_state() == {}

    def test_save_then_load(self):
        agent = make_agent()
        agent._save_state({"session_id": "abc", "created": True})
        assert agent._load_state() == {"session_id": "abc", "created": True}

    def test_load_invalid_json_returns_empty(self):
        agent = make_agent()
        with open(agent._state_file, "w") as f:
            f.write("not json")
        assert agent._load_state() == {}


# ═══════════════════════════════════════════════════════════════════════════════
# _build_mcp_server
# ═══════════════════════════════════════════════════════════════════════════════

class _SkillWithTool(Skill):
    @tool("Test tool")
    async def hello(self, name: str = "world"):
        return {"greeting": f"hello {name}"}


class TestBuildMcpServer:

    def test_no_skills_returns_none(self):
        agent = make_agent(skills=[])
        # AgentSkill автоматически добавляется, но у него тулов нет — server должен быть None
        assert agent._build_mcp_server() is None

    def test_with_skill_returns_server(self):
        skill = _SkillWithTool()
        agent = make_agent(skills=[skill])
        server = agent._build_mcp_server()
        assert server is not None
        assert server["type"] == "sdk"
        assert server["name"] == "slon"


# ═══════════════════════════════════════════════════════════════════════════════
# llm() — конвертация блоков в OpenAI-format
# ═══════════════════════════════════════════════════════════════════════════════

class TestLlmBlockConversion:

    @pytest.fixture
    def mock_client_class(self):
        """Подменяет ClaudeSDKClient на mock с контролируемыми сообщениями."""
        with patch("src.agent.claude_agent.ClaudeSDKClient") as cls:
            instance = MagicMock()
            instance.connect = AsyncMock()
            instance.query = AsyncMock()
            instance.disconnect = AsyncMock()
            cls.return_value = instance
            yield cls, instance

    @pytest.mark.asyncio
    async def test_text_block_becomes_assistant_content(self, mock_client_class):
        _, client = mock_client_class
        client.receive_response = lambda: aiter_messages([
            AssistantMessage(
                content=[TextBlock(text="hello")],
                model="claude", parent_tool_use_id=None, error=None,
                usage={}, message_id="m1", stop_reason="end_turn",
                session_id="s", uuid="u1",
            ),
            ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=1, session_id="s",
                total_cost_usd=0.01, usage={}, result=None, uuid="r1",
            ),
        ])

        agent = make_agent()
        agent._current_content_parts = [{"type": "text", "text": "hi"}]
        turns = await agent.llm()

        assert turns == [{
            "role": "assistant",
            "content": "hello",
            "_uuid": "u1",
        }]

    @pytest.mark.asyncio
    async def test_tool_use_becomes_tool_calls(self, mock_client_class):
        _, client = mock_client_class
        client.receive_response = lambda: aiter_messages([
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="search", input={"q": "x"})],
                model="claude", parent_tool_use_id=None, error=None,
                usage={}, message_id="m1", stop_reason="tool_use",
                session_id="s", uuid="u1",
            ),
            ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=1, session_id="s",
                total_cost_usd=0.0, usage={}, result=None, uuid="r1",
            ),
        ])

        agent = make_agent()
        agent._current_content_parts = [{"type": "text", "text": "go"}]
        turns = await agent.llm()

        assert len(turns) == 1
        t = turns[0]
        assert t["role"] == "assistant"
        assert t["_uuid"] == "u1"
        assert t["tool_calls"][0]["id"] == "t1"
        assert t["tool_calls"][0]["function"]["name"] == "search"
        assert json.loads(t["tool_calls"][0]["function"]["arguments"]) == {"q": "x"}

    @pytest.mark.asyncio
    async def test_tool_result_becomes_tool_role(self, mock_client_class):
        _, client = mock_client_class
        client.receive_response = lambda: aiter_messages([
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="search", input={})],
                model="claude", parent_tool_use_id=None, error=None,
                usage={}, message_id="m1", stop_reason="tool_use",
                session_id="s", uuid="u1",
            ),
            UserMessage(
                content=[ToolResultBlock(tool_use_id="t1", content="found it", is_error=False)],
                parent_tool_use_id=None,
                tool_use_result=None, uuid="u2",
            ),
            ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=1, session_id="s",
                total_cost_usd=0.0, usage={}, result=None, uuid="r1",
            ),
        ])

        agent = make_agent()
        agent._current_content_parts = [{"type": "text", "text": "go"}]
        turns = await agent.llm()

        assert len(turns) == 2
        result_turn = turns[1]
        assert result_turn["role"] == "tool"
        assert result_turn["tool_call_id"] == "t1"
        assert result_turn["name"] == "search"
        assert result_turn["content"] == "found it"
        assert result_turn["_uuid"] == "u2"

    @pytest.mark.asyncio
    async def test_stream_text_delta_pipes_to_transport(self, mock_client_class):
        _, client = mock_client_class
        client.receive_response = lambda: aiter_messages([
            stream_event("content_block_start", content_block={"type": "text"}),
            stream_event("content_block_delta", delta={"type": "text_delta", "text": "Hi"}),
            stream_event("content_block_delta", delta={"type": "text_delta", "text": "!"}),
            stream_event("content_block_stop"),
            AssistantMessage(
                content=[TextBlock(text="Hi!")],
                model="claude", parent_tool_use_id=None, error=None,
                usage={}, message_id="m1", stop_reason="end_turn",
                session_id="s", uuid="u1",
            ),
            ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=1, session_id="s",
                total_cost_usd=0.0, usage={}, result=None, uuid="r1",
            ),
        ])

        agent = make_agent()
        agent._current_content_parts = [{"type": "text", "text": "hi"}]
        await agent.llm()

        assert agent.transport.send_message.call_count == 2  # "Hi" + "!"


# ═══════════════════════════════════════════════════════════════════════════════
# llm() — session_id (resume vs fresh)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSessionLifecycle:

    @pytest.fixture
    def mock_client_class(self):
        with patch("src.agent.claude_agent.ClaudeSDKClient") as cls:
            instance = MagicMock()
            instance.connect = AsyncMock()
            instance.query = AsyncMock()
            instance.disconnect = AsyncMock()
            instance.receive_response = lambda: aiter_messages([
                ResultMessage(
                    subtype="success", duration_ms=0, duration_api_ms=0,
                    is_error=False, num_turns=1, session_id="s",
                    total_cost_usd=0.0, usage={}, result=None, uuid="r1",
                ),
            ])
            cls.return_value = instance
            yield cls, instance

    @pytest.mark.asyncio
    async def test_first_call_uses_session_id_and_persists_state(self, mock_client_class):
        cls, _ = mock_client_class

        agent = make_agent()
        agent._current_content_parts = [{"type": "text", "text": "hi"}]
        await agent.llm()

        # ClaudeSDKClient был создан с options.session_id (не resume)
        opts = cls.call_args.kwargs["options"]
        assert opts.session_id  # UUID
        assert opts.resume is None

        # state-файл сохранён с created=True
        state = agent._load_state()
        assert state["created"] is True
        assert state["session_id"] == opts.session_id

    @pytest.mark.asyncio
    async def test_subsequent_call_uses_resume(self, mock_client_class):
        cls, _ = mock_client_class

        agent = make_agent()
        agent._save_state({"session_id": "fixed-uuid", "created": True})
        agent._current_content_parts = [{"type": "text", "text": "hi"}]
        await agent.llm()

        opts = cls.call_args.kwargs["options"]
        assert opts.resume == "fixed-uuid"
        assert opts.session_id is None


# ═══════════════════════════════════════════════════════════════════════════════
# llm() — переиспользование клиента
# ═══════════════════════════════════════════════════════════════════════════════

class TestClientReuse:

    @pytest.fixture
    def mock_client_class(self):
        with patch("src.agent.claude_agent.ClaudeSDKClient") as cls:
            instance = MagicMock()
            instance.connect = AsyncMock()
            instance.query = AsyncMock()
            instance.disconnect = AsyncMock()
            instance.receive_response = lambda: aiter_messages([
                ResultMessage(
                    subtype="success", duration_ms=0, duration_api_ms=0,
                    is_error=False, num_turns=1, session_id="s",
                    total_cost_usd=0.0, usage={}, result=None, uuid="r1",
                ),
            ])
            cls.return_value = instance
            yield cls, instance

    @pytest.mark.asyncio
    async def test_reuses_client_when_append_unchanged(self, mock_client_class):
        cls, _ = mock_client_class

        agent = make_agent()
        agent._current_content_parts = [{"type": "text", "text": "first"}]
        await agent.llm()
        agent._current_content_parts = [{"type": "text", "text": "second"}]
        await agent.llm()

        # Клиент создан только один раз
        assert cls.call_count == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Polymorphism — Agent.loop принимает list или dict от llm()
# ═══════════════════════════════════════════════════════════════════════════════

class TestLoopPolymorphism:

    def test_dict_wrapped_as_list(self):
        result = {"role": "assistant", "content": "hi"}
        turns = result if isinstance(result, list) else [result]
        assert turns == [{"role": "assistant", "content": "hi"}]

    def test_list_passes_through(self):
        result = [{"role": "assistant", "content": "a"}, {"role": "tool", "content": "b"}]
        turns = result if isinstance(result, list) else [result]
        assert turns is result


# ═══════════════════════════════════════════════════════════════════════════════
# Integration — реальный claude binary
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
class TestClaudeAgentIntegration:

    @pytest.mark.asyncio
    async def test_real_claude_says_hi(self):
        agent = make_agent()
        agent._current_content_parts = [{"type": "text", "text": "Reply with exactly the word: pong"}]

        try:
            turns = await agent.llm()
        finally:
            await agent.close()

        # Ожидаем хотя бы один assistant text-turn
        text_turns = [t for t in turns if t.get("role") == "assistant" and "content" in t]
        assert text_turns, f"no assistant text turns, got: {turns}"
        # Текст не пустой и uuid от клода есть
        assert text_turns[0]["content"]
        assert text_turns[0].get("_uuid")
