"""
Тесты класса Skill: автодискавери @tool / @bypass, генерация схем, диспетчеризация.

Запуск:
    venv\\Scripts\\python -m pytest tests/test_skill.py -v
"""
import json
import os
import sys
from typing import Annotated

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent import Skill, tool, bypass


# ── fixtures ──────────────────────────────────────────────────────────────────

class SimpleSkill(Skill):
    @tool("Складывает два числа")
    def add(self, a: Annotated[int, "первое"], b: Annotated[int, "второе"] = 0) -> int:
        return a + b

    @tool("Говорит привет")
    def greet(self, name: Annotated[str, "имя"]) -> str:
        return f"Привет, {name}!"

    @bypass("hello", description="Приветствие", standalone=True)
    def _hello(self, args: str) -> str:
        return f"hello {args}".strip()

    @bypass("echo")
    def _echo(self, args: str) -> str:
        return args


# ═══════════════════════════════════════════════════════════════════════════════
# @tool — схемы
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolSchema:

    def setup_method(self):
        self.skill = SimpleSkill()

    def test_tool_count(self):
        assert len(self.skill.get_tools()) == 2

    def test_tool_names(self):
        names = {t["function"]["name"] for t in self.skill.get_tools()}
        assert "simple_add" in names
        assert "simple_greet" in names

    def test_tool_description(self):
        tool_map = {t["function"]["name"]: t for t in self.skill.get_tools()}
        assert tool_map["simple_add"]["function"]["description"] == "Складывает два числа"
        assert tool_map["simple_greet"]["function"]["description"] == "Говорит привет"

    def test_required_params(self):
        tool_map = {t["function"]["name"]: t for t in self.skill.get_tools()}
        # a — обязательный, b — с дефолтом
        assert tool_map["simple_add"]["function"]["parameters"]["required"] == ["a"]
        assert tool_map["simple_greet"]["function"]["parameters"]["required"] == ["name"]

    def test_param_types(self):
        tool_map = {t["function"]["name"]: t for t in self.skill.get_tools()}
        props = tool_map["simple_add"]["function"]["parameters"]["properties"]
        assert props["a"]["type"] == "integer"
        assert props["b"]["type"] == "integer"

    def test_param_descriptions(self):
        tool_map = {t["function"]["name"]: t for t in self.skill.get_tools()}
        props = tool_map["simple_add"]["function"]["parameters"]["properties"]
        assert props["a"]["description"] == "первое"
        assert props["b"]["description"] == "второе"

    def test_tool_type_field(self):
        for t in self.skill.get_tools():
            assert t["type"] == "function"

    def test_class_suffix_stripped_from_name(self):
        class MyProviderSkill(Skill):
            @tool("test")
            def do_thing(self): return {}

        s = MyProviderSkill()
        names = [t["function"]["name"] for t in s.get_tools()]
        assert "my_do_thing" in names


# ═══════════════════════════════════════════════════════════════════════════════
# dispatch_tool_call
# ═══════════════════════════════════════════════════════════════════════════════

class TestDispatchToolCall:

    def setup_method(self):
        self.skill = SimpleSkill()

    @pytest.mark.asyncio
    async def test_sync_tool(self):
        tc = {"function": {"name": "simple_add", "arguments": '{"a": 3, "b": 4}'}}
        result = await self.skill.dispatch_tool_call(tc)
        assert result == 7

    @pytest.mark.asyncio
    async def test_optional_param_default(self):
        tc = {"function": {"name": "simple_add", "arguments": '{"a": 5}'}}
        result = await self.skill.dispatch_tool_call(tc)
        assert result == 5

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self):
        tc = {"function": {"name": "simple_nonexistent", "arguments": "{}"}}
        result = await self.skill.dispatch_tool_call(tc)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_invalid_json_args_returns_empty(self):
        tc = {"function": {"name": "simple_add", "arguments": "not-json"}}
        result = await self.skill.dispatch_tool_call(tc)
        # пустые args → TypeError → error dict
        assert "error" in result

    @pytest.mark.asyncio
    async def test_async_tool(self):
        class AsyncSkill(Skill):
            @tool("async tool")
            async def fetch(self, url: Annotated[str, "url"]) -> dict:
                return {"url": url}

        s = AsyncSkill()
        tc = {"function": {"name": "async_fetch", "arguments": '{"url": "http://example.com"}'}}
        result = await s.dispatch_tool_call(tc)
        assert result == {"url": "http://example.com"}


# ═══════════════════════════════════════════════════════════════════════════════
# @bypass
# ═══════════════════════════════════════════════════════════════════════════════

class TestBypass:

    def setup_method(self):
        self.skill = SimpleSkill()

    def test_is_bypass_command_true(self):
        assert self.skill.is_bypass_command("/hello world") is True

    def test_is_bypass_command_false(self):
        assert self.skill.is_bypass_command("hello world") is False

    def test_is_bypass_command_unknown(self):
        assert self.skill.is_bypass_command("/unknown_cmd") is False

    def test_get_bypass_commands_includes_described(self):
        cmds = self.skill.get_bypass_commands()
        assert "hello" in cmds

    def test_get_bypass_commands_excludes_undescribed(self):
        # echo не имеет description
        cmds = self.skill.get_bypass_commands()
        assert "echo" not in cmds

    def test_get_bypass_commands_standalone_only(self):
        cmds = self.skill.get_bypass_commands(standalone_only=True)
        assert "hello" in cmds

    @pytest.mark.asyncio
    async def test_dispatch_bypass_with_args(self):
        result = await self.skill.dispatch_bypass("/hello Alice")
        assert result == "hello Alice"

    @pytest.mark.asyncio
    async def test_dispatch_bypass_no_args(self):
        result = await self.skill.dispatch_bypass("/hello")
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_dispatch_echo(self):
        result = await self.skill.dispatch_bypass("/echo test message")
        assert result == "test message"
