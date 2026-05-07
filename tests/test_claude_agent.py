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
        assert len(stream_calls) >= 3, f"ожидаем стрим из >=3 вызовов, получили {len(stream_calls)}"
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
