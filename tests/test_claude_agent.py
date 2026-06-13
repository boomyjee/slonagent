"""
Тесты ClaudeAgent: llm()-стрим клода, конвертация блоков в OpenAI-format,
session_id state, переиспользование клиента.

Запуск:
    .venv\\Scripts\\python -m pytest tests/test_claude_agent.py -v
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent import Skill, tool
from src.memory.providers.base import BaseProvider
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


CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")


def make_agent(skills=None, model_name: str = "claude-test"):
    """Минимальный Agent с Claude-бекендом для тестов. По умолчанию модель —
    фейковая (для unit-тестов с моками). Integration-тесты передают реальную."""
    from src.agent.agent import Agent
    agent = Agent(
        id="test",
        model_name=model_name,
        backend="claude",
        agent_dir=tempfile.mkdtemp(),
        memory_compressor=PassthroughCompressor(),
        skills=skills or [],
    )
    agent.transport = MagicMock()
    agent.transport.send_message = AsyncMock()
    agent.transport.send_thinking = AsyncMock()
    agent.transport.on_tool_call = AsyncMock()
    agent.transport.on_tool_result = AsyncMock()
    agent.transport.close = AsyncMock()
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
        assert agent.backend_impl._load_state() == {}

    def test_save_then_load(self):
        agent = make_agent()
        agent.backend_impl._save_state({"session_id": "abc", "created": True})
        assert agent.backend_impl._load_state() == {"session_id": "abc", "created": True}

    def test_load_invalid_json_returns_empty(self):
        agent = make_agent()
        with open(agent.backend_impl._state_file, "w") as f:
            f.write("not json")
        assert agent.backend_impl._load_state() == {}


# ═══════════════════════════════════════════════════════════════════════════════
# Ephemeral session cleanup
# ═══════════════════════════════════════════════════════════════════════════════

class TestEphemeralCleanup:
    """Эфемерный backend (без agent_dir → memory_dir пуст → state_file None) на
    GC удаляет свой session jsonl из ~/.claude/projects/<cwd>/, чтоб между
    запусками не накапливались orphan-сессии. Persistent main-агент (со
    state_file на диске) не трогается — его сессия живёт через рестарты."""

    def _make_ephemeral_with_fake_jsonl(self):
        from src.agent.agent import Agent
        import uuid as _uuid
        agent = Agent(id="eph", model_name="m", backend="claude")
        backend = agent.backend_impl
        sid = str(_uuid.uuid4())
        backend._memory_state = {"session_id": sid, "created": True}
        # Кладём фейковый jsonl там же где CLI положил бы свой.
        jsonl = backend._claude_session_jsonl(sid)
        # _claude_session_jsonl возвращает None если файла нет — для теста
        # вычислим путь сами и создадим.
        import re
        cwd = backend._cwd or os.getcwd()
        sanitized = re.sub(r'[^a-zA-Z0-9]', '-', cwd)
        proj_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects", sanitized)
        os.makedirs(proj_dir, exist_ok=True)
        jsonl = os.path.join(proj_dir, f"{sid}.jsonl")
        with open(jsonl, "w") as f:
            f.write('{"fake": true}\n')
        return agent, backend, jsonl

    def test_ephemeral_jsonl_removed_on_gc(self):
        import gc
        agent, backend, jsonl = self._make_ephemeral_with_fake_jsonl()
        assert os.path.exists(jsonl)
        del agent
        del backend
        gc.collect()
        assert not os.path.exists(jsonl), f"expected {jsonl} to be removed by __del__"

    def test_persistent_jsonl_not_removed_on_gc(self):
        # У main-агента state_file на диске → __del__ его сессию не трогает.
        import gc, tempfile, uuid as _uuid, re
        from src.agent.agent import Agent
        agent_dir = tempfile.mkdtemp()
        agent = Agent(id="main", model_name="m", backend="claude", agent_dir=agent_dir)
        backend = agent.backend_impl
        sid = str(_uuid.uuid4())
        backend._save_state({"session_id": sid, "created": True})
        sanitized = re.sub(r'[^a-zA-Z0-9]', '-', backend._cwd)
        proj_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects", sanitized)
        os.makedirs(proj_dir, exist_ok=True)
        jsonl = os.path.join(proj_dir, f"{sid}.jsonl")
        with open(jsonl, "w") as f:
            f.write('{"fake": true}\n')
        try:
            del agent
            del backend
            gc.collect()
            assert os.path.exists(jsonl), "persistent agent's session must NOT be cleaned by __del__"
        finally:
            with __import__("contextlib").suppress(OSError): os.remove(jsonl)


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
        assert agent.backend_impl._build_mcp_server() is None

    def test_with_skill_returns_server(self):
        skill = _SkillWithTool()
        agent = make_agent(skills=[skill])
        server = agent.backend_impl._build_mcp_server()
        assert server is not None
        assert server["type"] == "sdk"
        assert server["name"] == "slon"


_FAKE_B64 = "ZmFrZWltYWdl"  # base64 of "fakeimage"


class _ImgSkill(Skill):
    @tool("returns image")
    async def pic(self):
        return {"_parts": [{"type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{_FAKE_B64}"}}]}


class _MixSkill(Skill):
    @tool("returns text+image")
    async def both(self):
        return {"_parts": [
            {"type": "text", "text": "look at this:"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_FAKE_B64}"}},
        ]}


class _StrSkill(Skill):
    @tool("returns string")
    async def msg(self):
        return "plain text result"


class TestMcpHandlerContent:
    """Handler внутри _build_mcp_server конвертирует skill-результаты в
    MCP content blocks. Дёргаем настоящий MCP-сервер через его call_tool
    request_handler — чтоб не подстраивать код под тесты."""

    @staticmethod
    async def _call(skill, tool_name, arguments=None):
        from mcp.types import CallToolRequest, CallToolRequestParams
        agent = make_agent(skills=[skill])
        server = agent.backend_impl._build_mcp_server()
        handler = server["instance"].request_handlers[CallToolRequest]
        req = CallToolRequest(method="tools/call",
                              params=CallToolRequestParams(name=tool_name, arguments=arguments or {}))
        return (await handler(req)).root.content

    @pytest.mark.asyncio
    async def test_dict_result_becomes_text_block_with_json(self):
        content = await self._call(_SkillWithTool(), "_skillwithtool_hello", {"name": "test"})
        assert len(content) == 1
        assert content[0].type == "text"
        assert '"greeting": "hello test"' in content[0].text

    @pytest.mark.asyncio
    async def test_image_url_part_becomes_image_block(self):
        # dispatch_tool_calls эмитит tool_turn (с пустым '{}' когда у result'a были
        # только _parts) + user_turn с самими parts. MCP отдаёт оба честно.
        content = await self._call(_ImgSkill(), "_img_pic")
        assert len(content) == 2
        assert content[0].type == "text" and content[0].text == "{}"
        assert content[1].type == "image"
        assert content[1].mimeType == "image/png"
        assert content[1].data == _FAKE_B64

    @pytest.mark.asyncio
    async def test_mixed_text_and_image_parts(self):
        content = await self._call(_MixSkill(), "_mix_both")
        assert len(content) == 3
        assert content[0].type == "text" and content[0].text == "{}"
        assert content[1].type == "text" and content[1].text == "look at this:"
        assert content[2].type == "image" and content[2].data == _FAKE_B64

    @pytest.mark.asyncio
    async def test_string_result_becomes_text_block(self):
        content = await self._call(_StrSkill(), "_str_msg")
        assert len(content) == 1
        assert content[0].type == "text"
        # MCP handler идёт через agent.dispatch_tool_calls, который JSON-сериализует
        # tool-result. Не-dict результаты оборачиваются в {"result": ...} для
        # унификации с OpenAI-форматом tool turns.
        assert content[0].text == '{"result": "plain text result"}'


# ═══════════════════════════════════════════════════════════════════════════════
# llm() — конвертация блоков в OpenAI-format
# ═══════════════════════════════════════════════════════════════════════════════

class TestLlmBlockConversion:

    @pytest.fixture
    def mock_client_class(self):
        """Подменяет ClaudeSDKClient на mock с контролируемыми сообщениями."""
        with patch("src.agent.backends.claude.ClaudeSDKClient") as cls:
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
        agent.memory._turns.append({"role": "user", "content": "hi"})
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
        agent.memory._turns.append({"role": "user", "content": "go"})
        turns = await agent.llm()

        assert len(turns) == 1
        t = turns[0]
        assert t["role"] == "assistant"
        assert t["_uuid"] == "u1"
        assert t["tool_calls"][0]["id"] == "t1"
        assert t["tool_calls"][0]["function"]["name"] == "search"
        assert json.loads(t["tool_calls"][0]["function"]["arguments"]) == {"q": "x"}

        # transport.on_tool_call вызывается с именем и аргументами
        agent.transport.on_tool_call.assert_called_once_with("search", {"q": "x"})

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
        agent.memory._turns.append({"role": "user", "content": "go"})
        turns = await agent.llm()

        assert len(turns) == 2
        result_turn = turns[1]
        assert result_turn["role"] == "tool"
        assert result_turn["tool_call_id"] == "t1"
        assert result_turn["name"] == "search"
        assert result_turn["content"] == "found it"
        assert result_turn["_uuid"] == "u2"

        # transport.on_tool_result вызывается с именем тула (НЕ tool_use_id) — UI
        # матчит карточку on_tool_call → on_tool_result по имени, поэтому они должны
        # совпадать. on_tool_call выше передал "search", здесь тоже "search".
        agent.transport.on_tool_result.assert_called_once_with("search", "found it")

    @pytest.mark.asyncio
    async def test_stream_text_delta_accumulates(self, mock_client_class):
        """Каждый text_delta должен слать в транспорт АККУМУЛИРОВАННЫЙ текст,
        не дельту. Иначе UI получит куски вместо растущего сообщения."""
        _, client = mock_client_class
        client.receive_response = lambda: aiter_messages([
            stream_event("content_block_start", content_block={"type": "text"}),
            stream_event("content_block_delta", delta={"type": "text_delta", "text": "Hi"}),
            stream_event("content_block_delta", delta={"type": "text_delta", "text": ", "}),
            stream_event("content_block_delta", delta={"type": "text_delta", "text": "world!"}),
            stream_event("content_block_stop"),
            AssistantMessage(
                content=[TextBlock(text="Hi, world!")],
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
        agent.memory._turns.append({"role": "user", "content": "hi"})
        await agent.llm()

        # Стрим-вызовы (с stream_id) — отдельно от финального "Готово ($cost)" без stream_id
        stream_calls = [c for c in agent.transport.send_message.call_args_list if c.kwargs.get("stream_id")]
        sent_texts = [c.args[0] for c in stream_calls]
        assert sent_texts == ["Hi", "Hi, ", "Hi, world!", "Hi, world!"]
        sent_stream_ids = [c.kwargs["stream_id"] for c in stream_calls]
        assert len(set(sent_stream_ids)) == 1

    @pytest.mark.asyncio
    async def test_stream_thinking_delta_accumulates(self, mock_client_class):
        _, client = mock_client_class
        client.receive_response = lambda: aiter_messages([
            stream_event("content_block_start", content_block={"type": "thinking"}),
            stream_event("content_block_delta", delta={"type": "thinking_delta", "thinking": "Let "}),
            stream_event("content_block_delta", delta={"type": "thinking_delta", "thinking": "me think"}),
            stream_event("content_block_stop"),
            ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=1, session_id="s",
                total_cost_usd=0.0, usage={}, result=None, uuid="r1",
            ),
        ])

        agent = make_agent()
        agent.memory._turns.append({"role": "user", "content": "hi"})
        await agent.llm()

        sent_thinking = [c.args[0] for c in agent.transport.send_thinking.call_args_list]
        assert sent_thinking == ["Let ", "Let me think", "Let me think"]


# ═══════════════════════════════════════════════════════════════════════════════
# llm() — субагенты (встроенный Claude Code Agent-тул)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSubagentMessages:
    """Сообщения с parent_tool_use_id != None — это активность изнутри встроенного
    Agent-тула. В parent's turns не пишем (иначе родитель «помнит» как делал
    Glob/Read которые на самом деле делал субагент), но в transport шлём — иначе
    в чате тишина пока субагент работает."""

    @pytest.fixture
    def mock_client_class(self):
        with patch("src.agent.backends.claude.ClaudeSDKClient") as cls:
            instance = MagicMock()
            instance.connect = AsyncMock()
            instance.query = AsyncMock()
            instance.disconnect = AsyncMock()
            cls.return_value = instance
            yield cls, instance

    @pytest.mark.asyncio
    async def test_subagent_textblock_goes_to_transport_not_turns(self, mock_client_class):
        """TextBlock субагента — send_message в transport, в turns не пишется.
        У субагента StreamEvent'ов нет (probe подтвердил), поэтому текст приходит
        целым TextBlock'ом и из этой ветки должен уйти в чат."""
        _, client = mock_client_class
        client.receive_response = lambda: aiter_messages([
            AssistantMessage(
                content=[ToolUseBlock(id="agent_call", name="Agent", input={"subagent_type": "haiku"})],
                model="claude", parent_tool_use_id=None, error=None,
                usage={}, message_id="m1", stop_reason="tool_use",
                session_id="s", uuid="u_parent_call",
            ),
            # Текст изнутри субагента
            AssistantMessage(
                content=[TextBlock(text="осенний лист падает")],
                model="claude", parent_tool_use_id="agent_call", error=None,
                usage={}, message_id="m2", stop_reason="end_turn",
                session_id="s", uuid="u_sub_text",
            ),
            # Финальный tool_result Agent-тула для родителя
            UserMessage(
                content=[ToolResultBlock(tool_use_id="agent_call", content="осенний лист падает", is_error=False)],
                parent_tool_use_id=None, tool_use_result=None, uuid="u_parent_result",
            ),
            ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=1, session_id="s",
                total_cost_usd=0.0, usage={}, result=None, uuid="r1",
            ),
        ])

        agent = make_agent()
        agent.memory._turns.append({"role": "user", "content": "сделай хайку"})
        turns = await agent.llm()

        # В turns: только parent's ToolUse(Agent) и финальный ToolResult — без TextBlock субагента
        uuids = [t.get("_uuid") for t in turns]
        assert "u_sub_text" not in uuids, "TextBlock субагента не должен попасть в turns"
        assert "u_parent_call" in uuids and "u_parent_result" in uuids

        # В transport: текст субагента ушёл send_message'ом
        sent_texts = [c.args[0] for c in agent.transport.send_message.call_args_list]
        assert "осенний лист падает" in sent_texts

    @pytest.mark.asyncio
    async def test_subagent_tooluse_calls_transport_not_turns(self, mock_client_class):
        """ToolUseBlock субагента — on_tool_call зовётся (как и раньше), но в
        turns не пишется."""
        _, client = mock_client_class
        client.receive_response = lambda: aiter_messages([
            AssistantMessage(
                content=[ToolUseBlock(id="agent_call", name="Agent", input={})],
                model="claude", parent_tool_use_id=None, error=None,
                usage={}, message_id="m1", stop_reason="tool_use",
                session_id="s", uuid="u_parent_call",
            ),
            # Внутренний Glob субагента
            AssistantMessage(
                content=[ToolUseBlock(id="sub_glob", name="Glob", input={"pattern": "**/*.py"})],
                model="claude", parent_tool_use_id="agent_call", error=None,
                usage={}, message_id="m2", stop_reason="tool_use",
                session_id="s", uuid="u_sub_tool",
            ),
            # ToolResult субагентского Glob
            UserMessage(
                content=[ToolResultBlock(tool_use_id="sub_glob", content="found 62 files", is_error=False)],
                parent_tool_use_id="agent_call", tool_use_result=None, uuid="u_sub_result",
            ),
            UserMessage(
                content=[ToolResultBlock(tool_use_id="agent_call", content="62", is_error=False)],
                parent_tool_use_id=None, tool_use_result=None, uuid="u_parent_result",
            ),
            ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=1, session_id="s",
                total_cost_usd=0.0, usage={}, result=None, uuid="r1",
            ),
        ])

        agent = make_agent()
        agent.memory._turns.append({"role": "user", "content": "посчитай"})
        turns = await agent.llm()

        # В turns: НЕТ ни tool_call'а Glob, ни tool_result'а от него
        uuids = [t.get("_uuid") for t in turns]
        assert "u_sub_tool" not in uuids
        assert "u_sub_result" not in uuids
        # Но parent's вызовы есть
        assert "u_parent_call" in uuids and "u_parent_result" in uuids

        # transport.on_tool_call вызывался ОБА раза — для Agent (parent) и Glob (sub)
        tool_call_names = [c.args[0] for c in agent.transport.on_tool_call.call_args_list]
        assert "Agent" in tool_call_names
        assert "Glob" in tool_call_names

        # transport.on_tool_result тоже зовётся для обоих
        tool_result_names = [c.args[0] for c in agent.transport.on_tool_result.call_args_list]
        assert "Glob" in tool_result_names
        assert "Agent" in tool_result_names

    @pytest.mark.asyncio
    async def test_subagent_initial_prompt_usermessage_ignored(self, mock_client_class):
        """SDK эхает initial prompt субагенту как UserMessage(parent_tool_use_id=X)
        с TextBlock внутри. В turns не идёт (там обрабатываются только ToolResult'ы),
        в transport тоже не шлём."""
        _, client = mock_client_class
        client.receive_response = lambda: aiter_messages([
            AssistantMessage(
                content=[ToolUseBlock(id="agent_call", name="Agent", input={})],
                model="claude", parent_tool_use_id=None, error=None,
                usage={}, message_id="m1", stop_reason="tool_use",
                session_id="s", uuid="u_parent_call",
            ),
            UserMessage(
                content=[TextBlock(text="initial prompt to subagent")],
                parent_tool_use_id="agent_call", tool_use_result=None, uuid="u_sub_initial",
            ),
            UserMessage(
                content=[ToolResultBlock(tool_use_id="agent_call", content="done", is_error=False)],
                parent_tool_use_id=None, tool_use_result=None, uuid="u_parent_result",
            ),
            ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=1, session_id="s",
                total_cost_usd=0.0, usage={}, result=None, uuid="r1",
            ),
        ])

        agent = make_agent()
        agent.memory._turns.append({"role": "user", "content": "go"})
        turns = await agent.llm()

        uuids = [t.get("_uuid") for t in turns]
        assert "u_sub_initial" not in uuids


# ═══════════════════════════════════════════════════════════════════════════════
# llm() — session_id (resume vs fresh)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSessionLifecycle:

    @pytest.fixture
    def mock_client_class(self):
        with patch("src.agent.backends.claude.ClaudeSDKClient") as cls:
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
        agent.memory._turns.append({"role": "user", "content": "hi"})
        await agent.llm()

        # ClaudeSDKClient был создан с options.session_id (не resume)
        opts = cls.call_args.kwargs["options"]
        assert opts.session_id  # UUID
        assert opts.resume is None

        # state-файл сохранён с created=True
        state = agent.backend_impl._load_state()
        assert state["created"] is True
        assert state["session_id"] == opts.session_id

    @pytest.mark.asyncio
    async def test_subsequent_call_uses_resume(self, mock_client_class):
        cls, _ = mock_client_class

        agent = make_agent()
        agent.backend_impl._save_state({"session_id": "fixed-uuid", "created": True})
        agent.memory._turns.append({"role": "user", "content": "hi"})
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
        with patch("src.agent.backends.claude.ClaudeSDKClient") as cls:
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
        agent.memory._turns.append({"role": "user", "content": "first"})
        await agent.llm()
        agent.memory._turns.append({"role": "user", "content": "second"})
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

class _CalcSkill(Skill):
    """Используется в integration-тестах для проверки реального tool-call'а."""

    def __init__(self):
        super().__init__()
        self.calls: list[dict] = []

    @tool("Сложить два числа. ОБЯЗАТЕЛЬНО используй эту тулзу, не считай в уме.")
    async def add(self, a: int, b: int) -> dict:
        self.calls.append({"a": a, "b": b})
        return {"sum": a + b}


@pytest.mark.integration
class TestClaudeAgentIntegration:

    @pytest.mark.asyncio
    async def test_real_claude_says_pong(self):
        agent = make_agent(model_name=CLAUDE_MODEL)
        agent.memory._turns.append({"role": "user", "content": "Reply with exactly the word: pong"})

        try:
            turns = await agent.llm()
        finally:
            await agent.close()

        text_turns = [t for t in turns if t.get("role") == "assistant" and "content" in t]
        assert text_turns, f"no assistant text turns, got: {turns}"
        # Содержимое должно быть pong, а не ошибка модели
        assert "pong" in text_turns[-1]["content"].lower(), f"got: {text_turns[-1]['content']!r}"
        assert text_turns[-1].get("_uuid")

    @pytest.mark.asyncio
    async def test_real_streaming_accumulates(self):
        """Реальный claude стримит ответ — транспорт получает множество вызовов
        send_message, каждый с НАРАСТАЮЩИМ текстом одного stream_id."""
        agent = make_agent(model_name=CLAUDE_MODEL)
        agent.memory._turns.append({"role": "user", "content": "Перечисли 5 цветов радуги через запятую."})

        try:
            await agent.llm()
        finally:
            await agent.close()

        # Стрим-вызовы (с stream_id) — без финального "Готово" без stream_id
        stream_calls = [c for c in agent.transport.send_message.call_args_list if c.kwargs.get("stream_id")]
        assert len(stream_calls) >= 2, f"ожидаем стрим из >=2 вызовов, получили {len(stream_calls)}"
        sent_texts = [c.args[0] for c in stream_calls]
        for prev, cur in zip(sent_texts, sent_texts[1:]):
            assert len(cur) >= len(prev), f"текст не накапливается: {prev!r} → {cur!r}"
        ids = {c.kwargs["stream_id"] for c in stream_calls}
        assert len(ids) == 1, f"ожидаем один stream_id, получили {ids}"

    @pytest.mark.asyncio
    async def test_real_tool_call_through_mcp(self):
        """Реальный claude должен вызвать наш MCP-tool через slon-сервер,
        и мы получим tool_call/tool_result в transport + соответствующие turn'ы."""
        calc = _CalcSkill()
        agent = make_agent(skills=[calc], model_name=CLAUDE_MODEL)
        agent.memory._turns.append({
            "role": "user",
            "content": "Используй тулзу add чтобы сложить 17 и 25. Верни только результат.",
        })

        try:
            turns = await agent.llm()
        finally:
            await agent.close()

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


# ═══════════════════════════════════════════════════════════════════════════════
# Integration — agent.stop() прерывает реального claude
# ═══════════════════════════════════════════════════════════════════════════════

from src.transport.base import BaseTransport
from src.memory.providers.base import BaseProvider as MemBaseProvider


class _StopTestTransport(BaseTransport):
    """Транспорт-«ждалка» для stop-тестов: события streaming_started /
    tool_called / agent_tool_called / processing_done — чтоб тест мог дождаться
    нужного момента и дёрнуть agent.stop()."""

    def __init__(self):
        super().__init__()
        self.streaming_started = asyncio.Event()
        self.tool_called = asyncio.Event()
        self.agent_tool_called = asyncio.Event()
        self.processing_done = asyncio.Event()
        self.messages: list[str] = []
        self.tool_calls: list[tuple[str, dict]] = []
        self.tool_results: list[tuple[str, object]] = []

    async def send_message(self, text, stream_id=None, final=True):
        self.messages.append(text)
        if stream_id:
            self.streaming_started.set()

    async def send_thinking(self, text, stream_id=None, final=False): pass
    async def send_memory_info(self, text): pass

    async def on_tool_call(self, name, args):
        self.tool_calls.append((name, args))
        self.tool_called.set()
        if name == "Agent":
            self.agent_tool_called.set()

    async def on_tool_result(self, name, result):
        self.tool_results.append((name, result))

    async def send_processing(self, active):
        if not active:
            self.processing_done.set()


class _PassthroughCompressor(MemBaseProvider):
    def __init__(self): super().__init__(consolidate_tokens=0)
    async def compress(self, turns): return turns


class _SlowSkill(Skill):
    """Тулза которая надолго засыпает — чтоб успеть прервать пока она работает.
    Сигналит entered когда вошла в тело и completed если успела закончиться (в
    норме теста не должна — её прерывает stop())."""

    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.completed = False

    @tool("Долгая операция. Используй когда тебя просят 'медленную тулзу'.")
    async def slow_thing(self, marker: str = "x") -> dict:
        self.entered.set()
        await asyncio.sleep(60)
        self.completed = True
        return {"marker": marker, "done": True}


def _make_stop_test_agent(skills=None, sdk_options=None):
    """Агент с реальным CLAUDE_MODEL, кастомным транспортом-ждалкой и
    PassthroughCompressor (чтоб не вмешивалась в добавление турнов)."""
    from src.agent.agent import Agent
    transport = _StopTestTransport()
    agent = Agent(
        id="stop_test",
        model_name=CLAUDE_MODEL,
        backend="claude",
        backend_params={"sdk_options": sdk_options} if sdk_options else None,
        agent_dir=tempfile.mkdtemp(),
        memory_compressor=_PassthroughCompressor(),
        skills=skills or [],
        transport=transport,
    )
    return agent, transport


@pytest.mark.integration
class TestClaudeStop:
    """Реальный claude должен корректно прерываться по agent.stop() в трёх
    моментах: во время стрима текста родителя, во время выполнения тула, во
    время работы субагента. После прерывания память должна быть сбалансирована
    (никаких висящих tool_calls без result'ов) и заканчиваться interrupt-маркером."""

    @staticmethod
    async def _run_until(agent, transport, text: str, wait_event: asyncio.Event,
                         wait_timeout: float = 30.0):
        """Стартует loop, посылает text, ждёт wait_event, дёргает stop, ждёт
        processing_done. Возвращает агента (НЕ закрывает — caller сам)."""
        await agent.start(run_loop=True)
        await agent.process_message([{"type": "text", "text": text}])
        await asyncio.wait_for(wait_event.wait(), timeout=wait_timeout)
        agent.stop()
        await asyncio.wait_for(transport.processing_done.wait(), timeout=30.0)

    @pytest.mark.asyncio
    async def test_stop_during_text_stream(self):
        agent, transport = _make_stop_test_agent()
        try:
            await self._run_until(
                agent, transport,
                "Напиши очень длинный рассказ про осенний лес, минимум 1000 слов. "
                "Не останавливайся, пиши развёрнуто.",
                transport.streaming_started,
            )
        finally:
            await agent.close()

        turns = list(agent.memory._turns)
        # Последний assistant-turn — interrupt-маркер
        assistant_turns = [t for t in turns if t.get("role") == "assistant" and isinstance(t.get("content"), str)]
        assert assistant_turns, f"нет assistant-турнов, turns={turns}"
        assert "прерван" in assistant_turns[-1]["content"], (
            f"ожидаем interrupt-маркер, получили: {assistant_turns[-1]['content']!r}"
        )

    @pytest.mark.asyncio
    async def test_stop_during_tool_call(self):
        """Прерываем когда тул УЖЕ ВЫПОЛНЯЕТСЯ (вошёл в тело, спит). Проверяем
        что тул не успел завершиться (slow.completed остался False) и память
        сбалансирована."""
        slow = _SlowSkill()
        agent, transport = _make_stop_test_agent(skills=[slow])
        try:
            await agent.start(run_loop=True)
            await agent.process_message([{"type": "text", "text":
                "Используй тулзу slow_thing с marker='hello'. Это медленная операция, "
                "просто запусти её и дождись результата."
            }])
            # Ждём пока тул реально вошёл в тело и спит
            await asyncio.wait_for(slow.entered.wait(), timeout=30.0)
            # Даём ему чуть-чуть «поработать»
            await asyncio.sleep(0.5)
            agent.stop()
            await asyncio.wait_for(transport.processing_done.wait(), timeout=30.0)
        finally:
            await agent.close()

        # Тул не должен был успеть завершиться (60-секундный сон)
        assert not slow.completed, "slow_thing завершилась до того как stop её прервал"

        turns = list(agent.memory._turns)
        tool_use_turns = [t for t in turns if t.get("tool_calls")]
        tool_result_turns = [t for t in turns if t.get("role") == "tool"]
        assert tool_use_turns, f"нет turn с tool_calls, turns={turns}"

        used_ids = {tc["id"] for t in tool_use_turns for tc in t.get("tool_calls", [])}
        result_ids = {t.get("tool_call_id") for t in tool_result_turns}
        assert used_ids <= result_ids, (
            f"висячие tool_calls без results: {used_ids - result_ids}, turns={turns}"
        )

        assert any("прерван" in str(t.get("content", "")) for t in turns[-3:]), (
            f"ожидаем interrupt-маркер в хвосте памяти, turns={turns}"
        )

    @pytest.mark.asyncio
    async def test_no_phantom_done_after_interrupt(self):
        """Bug-репро: после stop() недообработанный ResultMessage прерванного
        query'а остаётся в SDK-очереди — при следующем сообщении в чат вылетает
        «✅ Готово» за СТАРЫЙ query, до того как пришёл ответ на новый.

        Сценарий пользователя: LLM вызвала тул → прерываю → ничего не происходит
        → пишу что-то ещё → внезапно «✅ Готово (5 turns, $X)»."""
        slow = _SlowSkill()
        agent, transport = _make_stop_test_agent(skills=[slow])
        try:
            await agent.start(run_loop=True)

            # 1) Запрос дёргает медленный тул, прерываем пока тул работает
            await agent.process_message([{"type": "text", "text":
                "Используй тулзу slow_thing с marker='hi' и дождись результата."
            }])
            await asyncio.wait_for(slow.entered.wait(), timeout=30.0)
            await asyncio.sleep(0.5)
            agent.stop()
            await asyncio.wait_for(transport.processing_done.wait(), timeout=30.0)

            done_after_interrupt = sum(1 for m in transport.messages if "Готово" in m)
            assert done_after_interrupt == 0, (
                f"за прерванный query прилетело Готово (не должно): {done_after_interrupt}"
            )

            # 2) Второй короткий запрос. Если бы был баг — увидели бы ДВА Готово
            # (фантомный за прерванный + новый за этот).
            transport.processing_done.clear()
            await agent.process_message([{"type": "text", "text":
                "Ответь одним словом: 'ок'. Без тулов."
            }])
            await asyncio.wait_for(transport.processing_done.wait(), timeout=60.0)

            done_total = sum(1 for m in transport.messages if "Готово" in m)
            done_msgs = [m for m in transport.messages if "Готово" in m]
            assert done_total == 1, (
                f"phantom Готово detected: всего {done_total} Готово (ожидаем 1).\n"
                f"Готово-сообщения: {done_msgs}"
            )

            # Главная проверка: на 2-й запрос реально пришёл содержательный ответ.
            # Если был phantom Готово — receive_response() терминируется на старом
            # ResultMessage'е, новый запрос нормального ответа не получит.
            turns = list(agent.memory._turns)
            assistant_texts = [
                t["content"] for t in turns
                if t.get("role") == "assistant" and isinstance(t.get("content"), str)
                and "прерван" not in t["content"]
            ]
            assert assistant_texts, f"нет содержательных assistant-ответов, turns={turns}"
            last = assistant_texts[-1].lower()
            assert "ок" in last or "ok" in last, (
                f"на 2-й запрос не пришёл содержательный ответ: {assistant_texts[-1]!r}"
            )
        finally:
            await agent.close()

    @pytest.mark.asyncio
    async def test_stop_during_real_builtin_bash(self):
        """Реальный встроенный Bash тул клода со sleep 30. Проверяем что после
        stop() он ДЕЙСТВИТЕЛЬНО убит, а не висит — drain receive_response()
        должен прочитать ResultMessage быстро (<5s), не упереться в свой
        10-секундный таймаут."""
        import time
        sdk_options = {
            "system_prompt": {"type": "preset", "preset": "claude_code"},
            "setting_sources": None,
            "tools": None,
        }
        agent, transport = _make_stop_test_agent(sdk_options=sdk_options)
        try:
            await agent.start(run_loop=True)
            await agent.process_message([{"type": "text", "text":
                "Запусти Bash с командой `sleep 30 && echo done`. Просто запусти."
            }])
            await asyncio.wait_for(transport.tool_called.wait(), timeout=60.0)
            await asyncio.sleep(1.0)  # bash успел стартануть и начать sleep
            t0 = time.monotonic()
            agent.stop()
            await asyncio.wait_for(transport.processing_done.wait(), timeout=15.0)
            elapsed = time.monotonic() - t0
        finally:
            await agent.close()

        # Если CLI убил Bash — ResultMessage пришёл быстро. Если bash висит,
        # drain упрётся в свой 10-секундный timeout.
        assert elapsed < 5.0, (
            f"Bash не прервался: от stop до processing_done {elapsed:.1f}s "
            f"(близко к 10s drain-timeout — значит CLI не убил bash)."
        )

    @pytest.mark.asyncio
    async def test_stop_during_sandbox_exec(self):
        """Реальный sandbox_exec со sleep 60 в контейнере. Проверяем что после
        stop() docker-команда УБИТА, а не доработала в фоне.

        Использует asyncio.to_thread(subprocess.run) — у Python нет способа
        прервать блокирующий syscall в треде, так что подпроцесс может пережить
        cancel asyncio-задачи. Тест ловит этот случай через `podman exec ps`."""
        import shutil
        if shutil.which("podman") is None:
            pytest.skip("podman not available")

        from src.skills.sandbox import SandboxSkill
        sandbox = SandboxSkill()
        agent, transport = _make_stop_test_agent(skills=[sandbox])

        # Кастомное ожидание: ловим именно sandbox_exec, не другие тулы
        # SandboxSkill (read/glob/grep — может дёрнуть claude перед exec'ом).
        exec_called = asyncio.Event()
        orig_on_tool_call = transport.on_tool_call

        async def watch_for_exec(name, args):
            await orig_on_tool_call(name, args)
            if name == "sandbox_exec":
                exec_called.set()
        transport.on_tool_call = watch_for_exec

        try:
            await agent.start(run_loop=True)
            await agent.process_message([{"type": "text", "text":
                "Используй тулзу sandbox_exec с command='sleep 60' и timeout=120. "
                "Просто запусти и подожди результат, ничего не проверяй заранее."
            }])
            await asyncio.wait_for(exec_called.wait(), timeout=120.0)

            # Контейнер мог ещё подниматься — поллим пока sleep не появится в нём.
            # python:3.11-slim без procps. Искать через cmdline, но НЕ через
            # grep по тексту скрипта — сам sh-процесс имеет "sleep 60" в своём
            # cmdline (часть передаваемого скрипта). Проверяем точное совпадение.
            check_sleep_cmd = [
                "podman", "exec", sandbox.container_name, "sh", "-c",
                "for f in /proc/[0-9]*/cmdline; do "
                "  [ \"$(tr '\\0' ' ' < $f)\" = 'sleep 60 ' ] && exit 0; "
                "done; exit 1",
            ]
            async def wait_for_sleep_running():
                for _ in range(120):  # 60s максимум
                    proc = await asyncio.to_thread(
                        subprocess.run, check_sleep_cmd,
                        capture_output=True, text=True, timeout=10,
                    )
                    if proc.returncode == 0:
                        return
                    await asyncio.sleep(0.5)
                raise TimeoutError("sleep so не появился в контейнере")
            await wait_for_sleep_running()

            agent.stop()
            await asyncio.wait_for(transport.processing_done.wait(), timeout=15.0)

            # Дать пару секунд на распространение SIGKILL внутри контейнера
            await asyncio.sleep(2.0)

            post = subprocess.run(check_sleep_cmd, capture_output=True, text=True, timeout=10)
            assert post.returncode != 0, (
                f"sandbox_exec не убил sleep в контейнере после stop"
            )
        finally:
            sandbox.stop()  # снести контейнер
            await agent.close()

    @pytest.mark.asyncio
    async def test_stop_during_subagent(self):
        """Прерываем когда субагент УЖЕ ЧТО-ТО ДЕЛАЕТ (запустил внутри себя
        Bash). Ждём: parent дёрнул Agent → субагент дёрнул Bash → стоп. После
        stop() — parent's tool_call для Agent паренный синтетическим result'ом,
        внутренний Bash-вызов в parent's память не попал."""
        from claude_agent_sdk import AgentDefinition

        sdk_options = {
            "system_prompt": {"type": "preset", "preset": "claude_code"},
            "agents": {
                "slow-bash": AgentDefinition(
                    description="Запускает медленные bash команды",
                    prompt="Ты — bash-helper. Когда тебя зовут — обязательно "
                           "запусти Bash с командой 'sleep 30 && echo done'.",
                    tools=["Bash"],
                    model="haiku",
                ),
            },
            "setting_sources": None,
            # Дефолт claude.py — tools=[] (голый клод). Для Task-тула нужен None
            # (= все встроенные доступны).
            "tools": None,
        }
        agent, transport = _make_stop_test_agent(sdk_options=sdk_options)
        try:
            await agent.start(run_loop=True)
            await agent.process_message([{"type": "text", "text":
                "Используй Task tool с subagent_type='slow-bash'. "
                "Передай ему задачу: запустить медленную bash команду."
            }])
            # Сначала ждём что parent дёрнул Agent
            await asyncio.wait_for(transport.agent_tool_called.wait(), timeout=60.0)
            # Очищаем общий tool_called и ждём СЛЕДУЮЩИЙ on_tool_call — это уже
            # будет вызов внутри субагента (Bash).
            transport.tool_called.clear()
            await asyncio.wait_for(transport.tool_called.wait(), timeout=60.0)
            # Bash действительно стартанул в субагенте. Дадим ему чуть-чуть
            # повисеть на sleep и прерываем.
            await asyncio.sleep(0.5)
            agent.stop()
            await asyncio.wait_for(transport.processing_done.wait(), timeout=30.0)
        finally:
            await agent.close()

        turns = list(agent.memory._turns)
        tool_use_turns = [t for t in turns if t.get("tool_calls")]
        tool_result_turns = [t for t in turns if t.get("role") == "tool"]

        # Parent's tool_call для Agent должен быть в памяти. Внутренние Bash —
        # нет (по нашему фильтру parent_tool_use_id).
        agent_calls = [
            tc for t in tool_use_turns for tc in t.get("tool_calls", [])
            if tc["function"]["name"] == "Agent"
        ]
        assert agent_calls, f"нет tool_call для Agent в памяти, turns={turns}"

        bash_calls = [
            tc for t in tool_use_turns for tc in t.get("tool_calls", [])
            if tc["function"]["name"] == "Bash"
        ]
        assert not bash_calls, (
            f"внутренний Bash-вызов субагента попал в parent's память: {bash_calls}"
        )

        # Каждый Agent-вызов имеет паренный tool_result
        agent_ids = {tc["id"] for tc in agent_calls}
        result_ids = {t.get("tool_call_id") for t in tool_result_turns}
        assert agent_ids <= result_ids, (
            f"висячие Agent-tool_calls без results: {agent_ids - result_ids}, turns={turns}"
        )

        # Interrupt-маркер в хвосте
        assert any("прерван" in str(t.get("content", "")) for t in turns[-3:]), (
            f"ожидаем interrupt-маркер в хвосте, turns={turns}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Integration — PreToolUse-хук рубит Bash(run_in_background=true)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
class TestBashBackgroundHook:
    """Реальный claude + встроенный Bash: backend вешает PreToolUse-хук
    block_bash_background на matcher 'Bash'. Когда модель реально пытается
    запустить фоновую команду (run_in_background=true), хук обязан её
    зарубить, а deny-причина — дойти до модели как tool_result.

    Проверяем не саму функцию (это unit-тест в test_bash_background_hook),
    а что хук ДЕЙСТВИТЕЛЬНО навешен и срабатывает на живом ходу."""

    @pytest.mark.asyncio
    async def test_real_background_bash_denied(self):
        sdk_options = {
            "system_prompt": {"type": "preset", "preset": "claude_code"},
            "setting_sources": None,
            "tools": None,  # None = все встроенные тулы, включая Bash
        }
        agent, transport = _make_stop_test_agent(sdk_options=sdk_options)
        try:
            await agent.start(run_loop=True)
            await agent.process_message([{"type": "text", "text":
                "Используй Bash тул и ОБЯЗАТЕЛЬНО передай run_in_background=true. "
                "Команда: `echo hi`. Запусти именно в фоне (run_in_background=true), "
                "одним вызовом, ничего не проверяя заранее."
            }])
            await asyncio.wait_for(transport.processing_done.wait(), timeout=120.0)
        finally:
            await agent.close()

        # deny-причина хука должна была вернуться модели как tool_result —
        # значит хук реально перехватил живой Bash(run_in_background=true).
        # Не проверяем «команда не выполнилась»: после deny модель вправе
        # перезапустить echo синхронно — контракт именно в блокировке фона.
        blob = " ".join(str(r) for _, r in transport.tool_results)
        assert "run_in_background запрещён" in blob, (
            f"deny-причина хука не дошла до модели — фоновый Bash не заблокирован.\n"
            f"tool_calls={transport.tool_calls}\ntool_results={transport.tool_results}"
        )
