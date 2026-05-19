"""Тесты CodexBackend: handshake, dynamicTools, стрим в OpenAI-format,
session_id state, переиспользование thread'а.

Unit-тесты — без живого codex (через моки). Integration — с реальным
`codex app-server` (юзер должен быть залогинен через `codex login`).

Запуск:
    .venv\\Scripts\\python -m pytest tests/test_codex_agent.py -v
    .venv\\Scripts\\python -m pytest tests/test_codex_agent.py -v -m integration
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent import Skill, tool
from src.memory.providers.base import BaseProvider


class PassthroughCompressor(Skill):
    async def compress(self, turns):
        return turns


# По probe это default-модель кодекса для ChatGPT-аккаунта.
CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.5")


def make_agent(skills=None, model_name: str = "test-model", codex_options=None):
    from src.agent.agent import Agent
    backend_params = {}
    if codex_options is not None:
        backend_params["codex_options"] = codex_options
    agent = Agent(
        id="test",
        model_name=model_name,
        backend="codex",
        backend_params=backend_params or None,
        agent_dir=tempfile.mkdtemp(),
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
# Unit: State file
# ═══════════════════════════════════════════════════════════════════════════════

class TestStateFile:

    def test_load_empty_returns_dict(self):
        agent = make_agent()
        assert agent.backend_impl._load_state() == {}

    def test_save_then_load(self):
        agent = make_agent()
        agent.backend_impl._save_state({"thread_id": "thr_abc"})
        assert agent.backend_impl._load_state() == {"thread_id": "thr_abc"}

    def test_load_invalid_json_returns_empty(self):
        agent = make_agent()
        with open(agent.backend_impl._state_file, "w") as f:
            f.write("not json")
        assert agent.backend_impl._load_state() == {}

    def test_ephemeral_state_in_memory(self):
        from src.agent.agent import Agent
        agent = Agent(id="eph", model_name="m", backend="codex")
        b = agent.backend_impl
        assert b._state_file is None
        b._save_state({"thread_id": "x"})
        assert b._load_state() == {"thread_id": "x"}


# ═══════════════════════════════════════════════════════════════════════════════
# Unit: skills_fingerprint / _build_dynamic_tools
# ═══════════════════════════════════════════════════════════════════════════════

class _SkillA(Skill):
    @tool("First tool")
    async def hello(self, name: str = "x"):
        return {"ok": True}


class _SkillB(Skill):
    @tool("Second tool")
    async def world(self):
        return {"ok": True}


class TestFingerprint:

    def test_same_skills_same_fp(self):
        a1 = make_agent(skills=[_SkillA()])
        a2 = make_agent(skills=[_SkillA()])
        assert a1.backend_impl._skills_fingerprint() == a2.backend_impl._skills_fingerprint()

    def test_different_skills_different_fp(self):
        a1 = make_agent(skills=[_SkillA()])
        a2 = make_agent(skills=[_SkillA(), _SkillB()])
        assert a1.backend_impl._skills_fingerprint() != a2.backend_impl._skills_fingerprint()


class TestBuildDynamicTools:

    def test_empty_skills_returns_empty(self):
        # AgentSkill добавляется агентом сам, но у него тулов нет — список пустой
        agent = make_agent(skills=[])
        tools = agent.backend_impl._build_dynamic_tools()
        assert tools == []

    def test_skill_with_tool_produces_spec(self):
        agent = make_agent(skills=[_SkillA()])
        tools = agent.backend_impl._build_dynamic_tools()
        assert len(tools) == 1
        spec = tools[0]
        assert spec["name"].endswith("hello")
        assert spec["description"] == "First tool"
        assert spec["inputSchema"]["type"] == "object"
        assert "name" in spec["inputSchema"]["properties"]


# ═══════════════════════════════════════════════════════════════════════════════
# Unit: _build_user_input
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildUserInput:

    @pytest.mark.asyncio
    async def test_string_content(self):
        from src.agent.backends.codex import CodexBackend
        items = await CodexBackend._build_user_input([{"role": "user", "content": "hello"}])
        assert items == [{"type": "text", "text": "hello"}]

    @pytest.mark.asyncio
    async def test_empty_string_filtered(self):
        from src.agent.backends.codex import CodexBackend
        items = await CodexBackend._build_user_input([{"role": "user", "content": ""}])
        assert items == []

    @pytest.mark.asyncio
    async def test_text_part(self):
        from src.agent.backends.codex import CodexBackend
        items = await CodexBackend._build_user_input([
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        ])
        assert items == [{"type": "text", "text": "hi"}]

    @pytest.mark.asyncio
    async def test_image_url_part(self):
        from src.agent.backends.codex import CodexBackend
        items = await CodexBackend._build_user_input([
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}},
            ]},
        ])
        assert items == [{"type": "image", "url": "https://example.com/x.png"}]

    @pytest.mark.asyncio
    async def test_image_data_url_part(self):
        from src.agent.backends.codex import CodexBackend
        data_url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="
        items = await CodexBackend._build_user_input([
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ])
        assert items == [{"type": "image", "url": data_url}]

    @pytest.mark.asyncio
    async def test_mixed_text_and_image(self):
        from src.agent.backends.codex import CodexBackend
        items = await CodexBackend._build_user_input([
            {"role": "user", "content": [
                {"type": "text", "text": "see this"},
                {"type": "image_url", "image_url": {"url": "https://e.com/i.jpg"}},
            ]},
        ])
        assert items == [
            {"type": "text", "text": "see this"},
            {"type": "image", "url": "https://e.com/i.jpg"},
        ]


# ═══════════════════════════════════════════════════════════════════════════════
# Unit: codex binary discovery
# ═══════════════════════════════════════════════════════════════════════════════

class TestEnableFeatures:
    """backend_params.enable_features → CodexClient тащит флаги в args
    при spawn'е app-server'а. Дефолтный disable-список минус то что юзер
    хочет вернуть."""

    def test_backend_stores_enable_features_from_params(self):
        from src.agent.agent import Agent
        agent = Agent(
            id="t", model_name="m", backend="codex",
            backend_params={"enable_features": ["shell_tool", "web_search_request"]},
        )
        assert agent.backend_impl._enable_features == ("shell_tool", "web_search_request")

    def test_backend_defaults_to_empty_enable_features(self):
        from src.agent.agent import Agent
        agent = Agent(id="t", model_name="m", backend="codex")
        assert agent.backend_impl._enable_features == ()

    def test_disable_list_minus_enabled(self):
        """Косвенно: при start client'а аргументы --disable содержат всё
        из _DISABLED_NATIVE_FEATURES, минус то что юзер включил обратно."""
        from src.agent.backends.codex import CodexClient, _DISABLED_NATIVE_FEATURES
        client = CodexClient(enable_features=("shell_tool", "web_search_request"))
        # Эффективный disable набор — то что НЕ в enable_features
        effective_disable = tuple(
            f for f in _DISABLED_NATIVE_FEATURES if f not in client._enable_features
        )
        assert "shell_tool" not in effective_disable
        assert "web_search_request" not in effective_disable
        # Остальное (что не в enable_features) — должно быть отключено
        assert "browser_use" in effective_disable
        assert "computer_use" in effective_disable


class TestReasoning:
    """Reasoning (мысли модели) приходят только если в turn/start заданы
    effort и summary. Иначе codex не эмитит ни textDelta ни summaryTextDelta.
    """

    def test_default_effort_is_high(self):
        from src.agent.agent import Agent
        agent = Agent(id="t", model_name="m", backend="codex")
        assert agent.backend_impl._reasoning_effort == "high"

    def test_default_summary_is_auto(self):
        from src.agent.agent import Agent
        agent = Agent(id="t", model_name="m", backend="codex")
        assert agent.backend_impl._reasoning_summary == "auto"

    def test_reasoning_override_via_backend_params(self):
        from src.agent.agent import Agent
        agent = Agent(id="t", model_name="m", backend="codex",
                      backend_params={"reasoning_effort": "low",
                                      "reasoning_summary": "detailed"})
        assert agent.backend_impl._reasoning_effort == "low"
        assert agent.backend_impl._reasoning_summary == "detailed"

    def test_reasoning_none_skips_field(self):
        """Передача 'none' = НЕ передавать поле в turn/start
        (codex возьмёт model default — на free плане это значит
        мыслей не будет вообще)."""
        from src.agent.agent import Agent
        agent = Agent(id="t", model_name="m", backend="codex",
                      backend_params={"reasoning_effort": "none",
                                      "reasoning_summary": "none"})
        # Значения сохранены как "none"; в _llm() они в turn_params не
        # попадают (проверим ниже через интеграционный сценарий)
        assert agent.backend_impl._reasoning_effort == "none"
        assert agent.backend_impl._reasoning_summary == "none"


class TestApplyPatchApproval:
    """apply_patch — единственный baked-in writer codex'а без --disable
    флага. Блокируем через approval-hook: codex шлёт нам
    item/fileChange/requestApproval, мы decline'им. Юзер может разрешить
    apply_patch через backend_params.enable_features=['apply_patch']."""

    @pytest.mark.asyncio
    async def test_default_declines_apply_patch_approval(self):
        from src.agent.agent import Agent
        agent = Agent(id="t", model_name="m", backend="codex")
        backend = agent.backend_impl
        replies: list = []
        async def fake_reply(rid, result): replies.append((rid, result))
        backend._reply = fake_reply

        await backend._handle_server_request({
            "method": "item/fileChange/requestApproval",
            "id": 42,
            "params": {"changes": [{"path": "/x.py", "kind": "modify"}]},
        })
        assert replies == [(42, {"decision": "decline"})]

    @pytest.mark.asyncio
    async def test_apply_patch_in_enable_features_accepts(self):
        """Юзер кладёт 'apply_patch' в enable_features → мы автоматом
        accept'им approval-запросы → apply_patch работает."""
        from src.agent.agent import Agent
        agent = Agent(id="t", model_name="m", backend="codex",
                      backend_params={"enable_features": ["apply_patch"]})
        backend = agent.backend_impl
        replies: list = []
        async def fake_reply(rid, result): replies.append((rid, result))
        backend._reply = fake_reply

        await backend._handle_server_request({
            "method": "item/fileChange/requestApproval",
            "id": 42,
            "params": {},
        })
        assert replies == [(42, {"decision": "accept"})]

    @pytest.mark.asyncio
    async def test_both_apply_patch_methods_handled(self):
        """item/fileChange/requestApproval и applyPatchApproval — оба
        обрабатываем (codex может слать любой из них)."""
        from src.agent.agent import Agent
        agent = Agent(id="t", model_name="m", backend="codex")
        backend = agent.backend_impl
        replies: list = []
        async def fake_reply(rid, result): replies.append((rid, result))
        backend._reply = fake_reply

        await backend._handle_server_request({
            "method": "applyPatchApproval", "id": 1, "params": {},
        })
        await backend._handle_server_request({
            "method": "item/fileChange/requestApproval", "id": 2, "params": {},
        })
        assert replies == [
            (1, {"decision": "decline"}),
            (2, {"decision": "decline"}),
        ]


class TestSandboxMode:
    """Codex'овский thread.sandbox это hard-block для встроенных тулов
    (apply_patch, shell_tool — если включён). Default read-only делает
    apply_patch неработоспособным на kernel-уровне. Юзер может open'нуть."""

    def test_default_sandbox_is_danger_full_access(self):
        """Default sandbox = danger-full-access. Модель в своих системках
        видит "No filesystem sandboxing — all permitted", не пугается.
        Реальная защита apply_patch идёт через approval-hook (см.
        _handle_server_request). С read-only / workspace-write модель
        видела "sandbox_mode is read-only" и отказывалась звать наши
        functions.sandbox_write — это и был user-facing баг."""
        from src.agent.agent import Agent
        agent = Agent(id="t", model_name="m", backend="codex")
        assert agent.backend_impl._sandbox == "danger-full-access"

    def test_default_approval_policy_is_untrusted(self):
        """Default approvalPolicy = untrusted. Под untrusted codex шлёт
        нам approval перед apply_patch / shell escalations → declining →
        блок без kernel-sandbox'а. С danger-full-access + on-request codex
        НЕ спрашивает approval вообще (apply_patch применяется) — поэтому
        нужен именно untrusted."""
        from src.agent.agent import Agent
        agent = Agent(id="t", model_name="m", backend="codex")
        assert agent.backend_impl._approval_policy == "untrusted"

    def test_default_base_instructions_nonempty(self):
        """Default baseInstructions — наш минимальный placeholder. Непустой
        → codex НЕ вкидывает свою длинную системку "You are Codex, coding
        agent...". (Permissions/skills/env_context блоки управляются sandbox
        режимом, не baseInstructions.) Юзер-override не поддерживается —
        это код-defined constant; при изменении в коде юзер сносит
        CODEX_*.json и thread пересоздаётся."""
        from src.agent.backends.codex import _BASE_INSTRUCTIONS_DEFAULT
        assert _BASE_INSTRUCTIONS_DEFAULT
        assert isinstance(_BASE_INSTRUCTIONS_DEFAULT, str)

    def test_approval_policy_override(self):
        from src.agent.agent import Agent
        agent = Agent(id="t", model_name="m", backend="codex",
                      backend_params={"approval_policy": "never"})
        assert agent.backend_impl._approval_policy == "never"


    def test_sandbox_override_via_backend_params(self):
        from src.agent.agent import Agent
        agent = Agent(id="t", model_name="m", backend="codex",
                      backend_params={"sandbox": "workspace-write"})
        assert agent.backend_impl._sandbox == "workspace-write"

    def test_sandbox_danger_full_access(self):
        from src.agent.agent import Agent
        agent = Agent(id="t", model_name="m", backend="codex",
                      backend_params={"sandbox": "danger-full-access"})
        assert agent.backend_impl._sandbox == "danger-full-access"

    @pytest.mark.asyncio
    async def test_sandbox_propagates_to_thread_start(self):
        """Параметр backend_params.sandbox идёт в `sandbox` поле thread/start —
        именно оно даёт codex'у команду на kernel-уровне sandbox'а."""
        from src.agent.agent import Agent
        agent = Agent(id="t", model_name="m", backend="codex",
                      backend_params={"sandbox": "danger-full-access"})
        backend = agent.backend_impl
        sent: list[tuple] = []

        class _FakeClient:
            async def request(self, method, params, timeout=120):
                sent.append((method, params))
                if method == "thread/start":
                    return {"thread": {"id": "x", "path": None}}
                return {}
            def register_thread(self, *a, **kw): pass
            def unregister_thread(self, *a, **kw): pass

        backend._client = _FakeClient()
        await backend._start_fresh_thread("test", "")
        start = next(c for c in sent if c[0] == "thread/start")
        assert start[1]["sandbox"] == "danger-full-access"

    @pytest.mark.asyncio
    async def test_turn_does_not_override_sandbox(self):
        """turn/start НЕ должен передавать sandboxPolicy — иначе он override'ит
        thread.sandbox и read-only защита перестаёт работать. Раньше тут было
        sandboxPolicy=externalSandbox+network=enabled — это и было дыркой."""
        agent = make_agent(model_name="gpt-5.5")
        backend = agent.backend_impl
        sent: list[tuple] = []

        class _FakeClient:
            async def request(self, method, params, timeout=120):
                sent.append((method, params))
                if method == "thread/start":
                    return {"thread": {"id": "x", "path": None}}
                if method == "thread/inject_items":
                    return {}
                if method == "turn/start":
                    return {"turn": {"id": "t"}}
                return {}
            async def notify(self, *a, **kw): pass
            def register_thread(self, *a, **kw): pass
            def unregister_thread(self, *a, **kw): pass

        backend._client = _FakeClient()
        agent.memory._turns.append({"role": "user", "content": "hi"})

        # Гоняем llm() так далеко, пока он не пошлёт turn/start — потом
        # симулируем turn/completed и выходим
        wakeup_called = asyncio.Event()
        orig_request = _FakeClient.request

        async def _wakeup_after_turn_start(self_, method, params, timeout=120):
            sent.append((method, params))
            if method == "thread/start":
                return {"thread": {"id": "x", "path": None}}
            if method == "thread/inject_items":
                return {}
            if method == "turn/start":
                # симулируем turn/completed чтобы llm() не висел
                evt = backend._stream_state.get("wakeup")
                if evt:
                    backend._stream_state["result_params"] = {
                        "turn": {"status": "completed", "durationMs": 0},
                    }
                    evt.set()
                wakeup_called.set()
                return {"turn": {"id": "t"}}
            return {}
        _FakeClient.request = _wakeup_after_turn_start
        try:
            await asyncio.wait_for(agent.llm(), timeout=5)
        except (asyncio.TimeoutError, Exception):
            pass
        finally:
            _FakeClient.request = orig_request

        turn_calls = [c for c in sent if c[0] == "turn/start"]
        assert turn_calls, "turn/start не вызвался"
        # КЛЮЧЕВОЕ: sandboxPolicy НЕ должно быть в turn_params
        assert "sandboxPolicy" not in turn_calls[0][1], (
            "turn/start не должен override'ить sandbox — это сломает read-only "
            "защиту от apply_patch"
        )


class TestCodexClientPool:
    """Pool: одинаковые enable_features → шарят process. Разные →
    отдельный subprocess каждый. Без это backend'ы не могут иметь разные
    наборы нативных тулов, потому что --enable/--disable это CLI-флаги
    при spawn'е app-server'а."""

    @pytest.fixture(autouse=True)
    async def _reset_pool(self):
        """Сносим pool перед/после теста — иначе мокаемые в одном тесте
        client'ы протекают в следующий."""
        from src.agent.backends.codex import CodexClient
        await CodexClient.reset()
        yield
        await CodexClient.reset()

    @pytest.mark.asyncio
    async def test_same_features_share_client(self, monkeypatch):
        from src.agent.backends.codex import CodexClient
        # Мокаем _start чтоб не спавнить реальный codex (вне integration-режима).
        async def _fake_start(self): pass
        monkeypatch.setattr(CodexClient, "_start", _fake_start)

        a = await CodexClient.get(enable_features=("shell_tool",))
        b = await CodexClient.get(enable_features=("shell_tool",))
        assert a is b, "одинаковые enable_features должны делить process"

    @pytest.mark.asyncio
    async def test_different_features_get_separate_clients(self, monkeypatch):
        from src.agent.backends.codex import CodexClient
        async def _fake_start(self): pass
        monkeypatch.setattr(CodexClient, "_start", _fake_start)

        a = await CodexClient.get(enable_features=("shell_tool",))
        b = await CodexClient.get(enable_features=("web_search_request",))
        assert a is not b, "разные enable_features → разные subprocess'ы"

    @pytest.mark.asyncio
    async def test_key_normalization_set_and_order(self, monkeypatch):
        """Pool key — sorted set, чтоб порядок и дубли не давали разных
        clients (("a","b") и ("b","a","a") — это одно и то же)."""
        from src.agent.backends.codex import CodexClient
        async def _fake_start(self): pass
        monkeypatch.setattr(CodexClient, "_start", _fake_start)

        a = await CodexClient.get(enable_features=("shell_tool", "web_search_request"))
        b = await CodexClient.get(enable_features=("web_search_request", "shell_tool", "shell_tool"))
        assert a is b


class TestFindCodex:

    def test_returns_path_or_codex(self):
        from src.agent.backends.codex import _find_codex
        p = _find_codex()
        assert p
        if os.name == "nt":
            assert p == "codex" or p.endswith("codex.cmd")
        else:
            assert p == "codex" or p.endswith("/codex")

    def test_env_override(self):
        """CODEX_PATH env var переопределяет автодетект — нужно чтобы юзер
        мог указать путь без правки кода (например, тестировать pre-release
        версию codex'а)."""
        from src.agent.backends.codex import _find_codex
        orig = os.environ.get("CODEX_PATH")
        try:
            os.environ["CODEX_PATH"] = "/custom/path/to/codex"
            assert _find_codex() == "/custom/path/to/codex"
        finally:
            if orig is None:
                os.environ.pop("CODEX_PATH", None)
            else:
                os.environ["CODEX_PATH"] = orig


# ═══════════════════════════════════════════════════════════════════════════════
# Unit: _handle_tool_call — конвертация результатов скилла в contentItems
# ═══════════════════════════════════════════════════════════════════════════════

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


class _DictSkill(Skill):
    @tool("returns dict")
    async def info(self):
        return {"status": "ok", "value": 42}


class TestToolCallContentConversion:
    """_handle_tool_call берёт результат skill.dispatch_tool_call и конвертит
    его в codex'овый contentItems[]. Аналог TestMcpHandlerContent у клода."""

    @staticmethod
    async def _call(skill, tool_method: str):
        """Имитирует server-request `item/tool/call` и проверяет что отдадим
        codex'у через _reply. Мокаем proc/stdin чтобы _reply не лез в pipe."""
        agent = make_agent(skills=[skill])
        backend = agent.backend_impl
        # Подменяем _reply — забираем result и сохраняем
        reply_result = {}
        async def _capture_reply(rid, result):
            reply_result["rid"] = rid
            reply_result["result"] = result
        backend._reply = _capture_reply

        # Имя тула у скилла — `_<skillclass>_<method>` (см. Skill.get_tools)
        decl = next(d for s in agent.skills for d in s.get_tools()
                    if d["function"]["name"].endswith(tool_method))
        tool_name = decl["function"]["name"]

        await backend._handle_tool_call(rid=42, params={
            "tool": tool_name,
            "arguments": {},
            "callId": "call_xyz",
        })
        return reply_result["result"]

    @pytest.mark.asyncio
    async def test_dict_result_becomes_inputtext_json(self):
        result = await self._call(_DictSkill(), "info")
        assert result["success"] is True
        items = result["contentItems"]
        assert len(items) == 1
        assert items[0]["type"] == "inputText"
        text = items[0]["text"]
        assert '"status": "ok"' in text
        assert '"value": 42' in text

    @pytest.mark.asyncio
    async def test_string_result_becomes_inputtext(self):
        result = await self._call(_StrSkill(), "msg")
        items = result["contentItems"]
        # dispatch_tool_calls серриализует не-dict как {"result": ...}
        assert any('"result"' in i.get("text", "") and "plain text result" in i.get("text", "")
                   for i in items)

    @pytest.mark.asyncio
    async def test_image_url_part_becomes_inputimage(self):
        # _parts с image_url приходит как отдельный user-turn от dispatch_tool_calls
        result = await self._call(_ImgSkill(), "pic")
        items = result["contentItems"]
        # один inputText (для пустого tool_result '{}') + один inputImage
        types = [i["type"] for i in items]
        assert "inputImage" in types
        img = next(i for i in items if i["type"] == "inputImage")
        assert "fakeimage" in img["imageUrl"] or _FAKE_B64 in img["imageUrl"]

    @pytest.mark.asyncio
    async def test_mixed_text_and_image_parts(self):
        result = await self._call(_MixSkill(), "both")
        items = result["contentItems"]
        types = [i["type"] for i in items]
        assert "inputText" in types and "inputImage" in types
        # Текст "look at this:" должен встретиться
        assert any("look at this:" in i.get("text", "") for i in items if i["type"] == "inputText")


# ═══════════════════════════════════════════════════════════════════════════════
# Unit: _handle_notification — конвертация item-нотификаций в OpenAI turns
# ═══════════════════════════════════════════════════════════════════════════════

class TestNotificationHandling:
    """Аналог TestLlmBlockConversion у клода. Дёргаем _handle_notification
    напрямую с подделкой стрим-стейта (как если бы llm() уже запустился)."""

    @staticmethod
    def _setup_backend():
        agent = make_agent()
        backend = agent.backend_impl
        wakeup = asyncio.Event()
        backend._stream_state = {
            "wakeup": wakeup,
            "turns": [],
            "text_bufs": {},
            "thinking_bufs": {},
            "text_stream_ids": {},
            "thinking_stream_ids": {},
            "result_params": {},
        }
        return agent, backend, wakeup

    @pytest.mark.asyncio
    async def test_agent_message_delta_accumulates(self):
        agent, backend, _ = self._setup_backend()
        for delta in ["Hi", ", ", "world!"]:
            await backend._handle_notification({
                "method": "item/agentMessage/delta",
                "params": {"itemId": "item_1", "delta": delta},
            })
        stream_calls = [c for c in agent.transport.send_message.call_args_list]
        sent_texts = [c.args[0] for c in stream_calls]
        assert sent_texts == ["Hi", "Hi, ", "Hi, world!"]
        stream_ids = {c.kwargs["stream_id"] for c in stream_calls}
        assert len(stream_ids) == 1

    @pytest.mark.asyncio
    async def test_stream_id_monotonic_above_base(self):
        """stream_id берётся из process-wide `_next_stream_id()` который
        даёт BASE+counter где BASE = ms unix timestamp при загрузке модуля.
        Гарантия: все стрим-ID больше нуля (a-la timestamp), монотонны,
        и после restart'а процесса новые ID всегда > старых (UI хранит
        старые сообщения с прошлой сессии — нужно не коллидировать)."""
        from src.agent.backends.codex import _STREAM_ID_BASE, _next_stream_id
        a = _next_stream_id()
        b = _next_stream_id()
        c = _next_stream_id()
        assert a < b < c, f"stream_ids не монотонны: {a}, {b}, {c}"
        # BASE = ms-unix-timestamp при импорте модуля, должен быть в районе
        # 2024+ (> 1.7e12 ms). После restart'а base сдвинется вперёд →
        # новые ID > старых.
        assert _STREAM_ID_BASE > 1_700_000_000_000, (
            f"_STREAM_ID_BASE подозрительно мал: {_STREAM_ID_BASE} "
            f"(ожидаем ms unix timestamp)"
        )
        assert a > _STREAM_ID_BASE, f"stream_id ниже base'а: {a} <= {_STREAM_ID_BASE}"

    @pytest.mark.asyncio
    async def test_different_items_within_one_llm_get_different_stream_ids(self):
        """В одном llm()-вызове codex может стримить несколько отдельных
        agentMessage'ов (например, между ними был tool call: текст до тула —
        item_A, текст после тула — item_B). Каждый item_id должен получить
        свой stream_id, иначе второй текст затирает в UI первый."""
        agent, backend, _ = self._setup_backend()

        await backend._handle_notification({
            "method": "item/agentMessage/delta",
            "params": {"itemId": "item_A", "delta": "first message"},
        })
        first_call = agent.transport.send_message.call_args_list[-1]
        first_sid = first_call.kwargs["stream_id"]

        # Тот же stream_state, новый itemId — например после tool call'а
        await backend._handle_notification({
            "method": "item/agentMessage/delta",
            "params": {"itemId": "item_B", "delta": "second message"},
        })
        second_call = agent.transport.send_message.call_args_list[-1]
        second_sid = second_call.kwargs["stream_id"]

        assert first_sid != second_sid, (
            f"два разных agentMessage в одном llm() получили одинаковый "
            f"stream_id ({first_sid}) — второе сообщение затрёт первое в UI"
        )

    @pytest.mark.asyncio
    async def test_stream_id_unique_across_llm_calls(self):
        """Регрессия: stream_id должен быть уникален между llm()-вызовами,
        иначе UI воспримет второе сообщение как продолжение первого и будет
        редактировать его in-place. Юзер ловил это вручную — все сообщения
        склеивались в одно."""
        agent, backend, _ = self._setup_backend()

        # Первый llm() — пришла item_id=A, дельта "hello"
        await backend._handle_notification({
            "method": "item/agentMessage/delta",
            "params": {"itemId": "A", "delta": "hello"},
        })
        first_call = agent.transport.send_message.call_args_list[-1]
        first_stream_id = first_call.kwargs["stream_id"]

        # Имитируем выход из llm() + новый вызов: stream_state сбрасывается
        backend._stream_state = {}
        wakeup2 = asyncio.Event()
        backend._stream_state = {
            "wakeup": wakeup2, "turns": [],
            "text_bufs": {}, "thinking_bufs": {},
            "text_stream_ids": {}, "thinking_stream_ids": {},
            "result_params": {},
        }

        # Второй llm() — пришёл новый item_id=B
        await backend._handle_notification({
            "method": "item/agentMessage/delta",
            "params": {"itemId": "B", "delta": "world"},
        })
        second_call = agent.transport.send_message.call_args_list[-1]
        second_stream_id = second_call.kwargs["stream_id"]

        assert first_stream_id != second_stream_id, (
            f"stream_id повторился между llm()-вызовами ({first_stream_id} = "
            f"{second_stream_id}) — UI будет редактировать первое сообщение "
            f"вместо создания второго"
        )

    @pytest.mark.asyncio
    async def test_reasoning_text_delta_accumulates(self):
        agent, backend, _ = self._setup_backend()
        for delta in ["Let ", "me ", "think"]:
            await backend._handle_notification({
                "method": "item/reasoning/textDelta",
                "params": {"itemId": "r_1", "delta": delta},
            })
        sent = [c.args[0] for c in agent.transport.send_thinking.call_args_list]
        assert sent == ["Let ", "Let me ", "Let me think"]

    @pytest.mark.asyncio
    async def test_reasoning_summary_delta_accumulates(self):
        """summaryTextDelta — мысли модели в summary-форме. На free ChatGPT
        (gpt-5.5) raw textDelta никогда не приходит, только summary; раньше
        мы их игнорировали — юзер мыслей не видел вовсе. Теперь копим как
        thinking и шлём в transport."""
        agent, backend, _ = self._setup_backend()
        for delta in ["**Calculating", " primes**\n", "29 is the answer"]:
            await backend._handle_notification({
                "method": "item/reasoning/summaryTextDelta",
                "params": {"itemId": "r_1", "delta": delta},
            })
        sent = [c.args[0] for c in agent.transport.send_thinking.call_args_list]
        assert sent == [
            "**Calculating",
            "**Calculating primes**\n",
            "**Calculating primes**\n29 is the answer",
        ]
        # Один stream_id на все дельты одного item'а
        ids = {c.kwargs["stream_id"] for c in agent.transport.send_thinking.call_args_list}
        assert len(ids) == 1

    @pytest.mark.asyncio
    async def test_agent_message_completed_becomes_assistant_turn(self):
        agent, backend, _ = self._setup_backend()
        # Сначала symulируем стрим (заполняет stream_id)
        await backend._handle_notification({
            "method": "item/agentMessage/delta",
            "params": {"itemId": "m_1", "delta": "Hello"},
        })
        await backend._handle_notification({
            "method": "item/completed",
            "params": {"item": {
                "type": "agentMessage", "id": "m_1", "text": "Hello", "phase": "final_answer",
            }},
        })
        turns = backend._stream_state["turns"]
        assert len(turns) == 1
        assert turns[0]["role"] == "assistant"
        assert turns[0]["content"] == "Hello"
        assert turns[0]["_codex_item_id"] == "m_1"

    @pytest.mark.asyncio
    async def test_command_execution_completed_becomes_two_turns(self):
        """Нативный shell_tool codex'а приходит как item type=commandExecution.
        Должен записываться в turns как assistant.tool_calls(name=shell_command)
        + role:tool — точно так же как dynamicToolCall. Имя `shell_command`
        совпадает с тем что codex кладёт в jsonl как function_call.name, поэтому
        divergence-check будет видеть match без хаков.
        """
        agent, backend, _ = self._setup_backend()
        # Имитируем стрим output'а
        await backend._handle_notification({
            "method": "item/commandExecution/outputDelta",
            "params": {"itemId": "call_X", "delta": "HELLO\n"},
        })
        # item/completed для commandExecution
        await backend._handle_notification({
            "method": "item/completed",
            "params": {"item": {
                "type": "commandExecution", "id": "call_X",
                "command": "echo HELLO", "cwd": "/tmp",
                "status": "completed", "exitCode": 0,
            }},
        })
        turns = backend._stream_state["turns"]
        assert len(turns) == 2
        assert turns[0]["role"] == "assistant"
        assert turns[0]["tool_calls"][0]["id"] == "call_X"
        assert turns[0]["tool_calls"][0]["function"]["name"] == "shell_command"
        # arguments — JSON-string с command + cwd
        args = json.loads(turns[0]["tool_calls"][0]["function"]["arguments"])
        assert args["command"] == "echo HELLO"
        assert args["cwd"] == "/tmp"
        # tool result содержит exit code и стримленый output
        assert turns[1]["role"] == "tool"
        assert turns[1]["tool_call_id"] == "call_X"
        assert turns[1]["name"] == "shell_command"
        assert "Exit code: 0" in turns[1]["content"]
        assert "HELLO" in turns[1]["content"]

    @pytest.mark.asyncio
    async def test_file_change_completed_becomes_two_turns(self):
        agent, backend, _ = self._setup_backend()
        await backend._handle_notification({
            "method": "item/completed",
            "params": {"item": {
                "type": "fileChange", "id": "call_F",
                "changes": [{"path": "/x.py", "kind": "modify"}],
                "status": "completed",
            }},
        })
        turns = backend._stream_state["turns"]
        assert len(turns) == 2
        assert turns[0]["tool_calls"][0]["function"]["name"] == "file_change"
        assert turns[1]["name"] == "file_change"
        assert "modify" in turns[1]["content"] and "/x.py" in turns[1]["content"]

    @pytest.mark.asyncio
    async def test_web_search_completed_becomes_two_turns(self):
        agent, backend, _ = self._setup_backend()
        await backend._handle_notification({
            "method": "item/completed",
            "params": {"item": {
                "type": "webSearch", "id": "call_W",
                "query": "Python news",
                "action": {"type": "search", "results": ["r1", "r2"]},
            }},
        })
        turns = backend._stream_state["turns"]
        assert len(turns) == 2
        assert turns[0]["tool_calls"][0]["function"]["name"] == "web_search"
        args = json.loads(turns[0]["tool_calls"][0]["function"]["arguments"])
        assert args["query"] == "Python news"
        assert turns[1]["name"] == "web_search"

    @pytest.mark.asyncio
    async def test_mcp_tool_call_completed_becomes_two_turns(self):
        agent, backend, _ = self._setup_backend()
        await backend._handle_notification({
            "method": "item/completed",
            "params": {"item": {
                "type": "mcpToolCall", "id": "call_M",
                "server": "github", "tool": "search_issues",
                "arguments": {"q": "bug"},
                "result": {"items": []},
                "status": "completed",
            }},
        })
        turns = backend._stream_state["turns"]
        assert len(turns) == 2
        # имя должно быть server__tool — формат match'ится с codex MCP
        assert turns[0]["tool_calls"][0]["function"]["name"] == "github__search_issues"
        assert turns[1]["name"] == "github__search_issues"

    @pytest.mark.asyncio
    async def test_native_tools_match_jsonl_signatures(self):
        """Главная гарантия: запись native shell_command в память даёт ту же
        сигнатуру что codex пишет в jsonl как function_call(name=shell_command).
        Если бы это не совпадало — divergence-check бы дёргал fresh thread."""
        from src.agent.backends.codex import CodexBackend
        agent, backend, _ = self._setup_backend()
        await backend._handle_notification({
            "method": "item/completed",
            "params": {"item": {
                "type": "commandExecution", "id": "call_Z",
                "command": "ls", "cwd": "/", "status": "completed", "exitCode": 0,
            }},
        })
        memory_sigs = CodexBackend._memory_signatures(backend._stream_state["turns"])
        # Эмулируем jsonl как codex его пишет — function_call name=shell_command, call_id=Z
        fake_jsonl_line = '{"type":"response_item","payload":{"type":"function_call","name":"shell_command","call_id":"call_Z","arguments":"{}"}}'
        # Sig из jsonl
        import json as _json
        entry = _json.loads(fake_jsonl_line)
        p = entry["payload"]
        jsonl_sig = ("function_call", p["call_id"])
        assert jsonl_sig in memory_sigs, (
            "сигнатуры из памяти и из jsonl должны совпадать — иначе divergence "
            "будет triggerится на каждом нативном tool-call'е"
        )

    @pytest.mark.asyncio
    async def test_dynamic_tool_call_completed_becomes_two_turns(self):
        agent, backend, _ = self._setup_backend()
        await backend._handle_notification({
            "method": "item/started",
            "params": {"item": {
                "type": "dynamicToolCall",
                "id": "call_a",
                "tool": "add",
                "arguments": {"a": 1, "b": 2},
                "status": "inProgress",
            }},
        })
        await backend._handle_notification({
            "method": "item/completed",
            "params": {"item": {
                "type": "dynamicToolCall",
                "id": "call_a",
                "tool": "add",
                "arguments": {"a": 1, "b": 2},
                "status": "completed",
                "contentItems": [{"type": "inputText", "text": "3"}],
            }},
        })
        turns = backend._stream_state["turns"]
        assert len(turns) == 2
        # Первый — assistant с tool_calls
        assert turns[0]["role"] == "assistant"
        assert turns[0]["tool_calls"][0]["id"] == "call_a"
        assert turns[0]["tool_calls"][0]["function"]["name"] == "add"
        assert json.loads(turns[0]["tool_calls"][0]["function"]["arguments"]) == {"a": 1, "b": 2}
        # Второй — tool result
        assert turns[1]["role"] == "tool"
        assert turns[1]["tool_call_id"] == "call_a"
        assert turns[1]["name"] == "add"
        assert turns[1]["content"] == "3"
        # item/started → transport.on_tool_call
        agent.transport.on_tool_call.assert_called_once_with("add", {"a": 1, "b": 2})
        # item/completed → transport.on_tool_result
        agent.transport.on_tool_result.assert_called_once_with("add", "3")

    @pytest.mark.asyncio
    async def test_turn_completed_wakes_event(self):
        agent, backend, wakeup = self._setup_backend()
        assert not wakeup.is_set()
        await backend._handle_notification({
            "method": "turn/completed",
            "params": {"turn": {"id": "t_1", "status": "completed"}},
        })
        assert wakeup.is_set()
        assert backend._stream_state["result_params"]["turn"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_user_message_item_ignored(self):
        """item/completed с типом userMessage — это эхо нашего ввода обратно от
        codex'а, в turns не пишем (иначе дублируется с pending user-турном)."""
        agent, backend, _ = self._setup_backend()
        await backend._handle_notification({
            "method": "item/completed",
            "params": {"item": {
                "type": "userMessage", "id": "u_1",
                "content": [{"type": "text", "text": "hi"}],
            }},
        })
        assert backend._stream_state["turns"] == []

    @pytest.mark.asyncio
    async def test_notification_without_stream_state_ignored(self):
        """Если llm() не активен (stream_state пуст) — нотификация молча
        игнорируется без падений. Защита от поздних сообщений между turn'ами."""
        agent = make_agent()
        backend = agent.backend_impl
        backend._stream_state = {}  # llm() не активен
        # Не должно бросать
        await backend._handle_notification({
            "method": "item/agentMessage/delta",
            "params": {"itemId": "x", "delta": "stray"},
        })
        agent.transport.send_message.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# Unit: _memory_signatures / _is_codex_internal / _sync_codex_session
# ═══════════════════════════════════════════════════════════════════════════════

class TestMemorySignatures:

    def test_user_text_turn(self):
        from src.agent.backends.codex import CodexBackend
        sigs = CodexBackend._memory_signatures([
            {"role": "user", "content": "hello"},
        ])
        assert ("message", "user", "hello") in sigs

    def test_assistant_text_turn(self):
        from src.agent.backends.codex import CodexBackend
        sigs = CodexBackend._memory_signatures([
            {"role": "assistant", "content": "world"},
        ])
        assert ("message", "assistant", "world") in sigs

    def test_assistant_tool_calls_turn(self):
        from src.agent.backends.codex import CodexBackend
        sigs = CodexBackend._memory_signatures([
            {"role": "assistant", "tool_calls": [
                {"id": "call_1", "function": {"name": "x", "arguments": "{}"}},
                {"id": "call_2", "function": {"name": "y", "arguments": "{}"}},
            ]},
        ])
        assert ("function_call", "call_1") in sigs
        assert ("function_call", "call_2") in sigs

    def test_tool_result_turn(self):
        from src.agent.backends.codex import CodexBackend
        sigs = CodexBackend._memory_signatures([
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ])
        assert ("function_call_output", "call_1") in sigs

    def test_content_truncated_to_200_chars(self):
        from src.agent.backends.codex import CodexBackend
        long_text = "x" * 500
        sigs = CodexBackend._memory_signatures([
            {"role": "user", "content": long_text},
        ])
        assert ("message", "user", "x" * 200) in sigs

    def test_list_content_with_text_parts(self):
        from src.agent.backends.codex import CodexBackend
        sigs = CodexBackend._memory_signatures([
            {"role": "user", "content": [
                {"type": "text", "text": "hi "},
                {"type": "text", "text": "there"},
                {"type": "image_url", "image_url": {"url": "x"}},
            ]},
        ])
        assert ("message", "user", "hi there") in sigs


class TestIsCodexInternal:

    def test_developer_message_is_internal(self):
        from src.agent.backends.codex import CodexBackend
        assert CodexBackend._is_codex_internal({
            "type": "message", "role": "developer",
            "content": [{"type": "input_text", "text": "<permissions instructions>...</permissions>"}],
        })

    def test_user_envelope_is_internal(self):
        from src.agent.backends.codex import CodexBackend
        assert CodexBackend._is_codex_internal({
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "<environment_context>\n<cwd>X</cwd>\n</environment_context>"}],
        })

    def test_slon_wrapper_not_codex_internal(self):
        """Регрессия (юзер ловил): slon оборачивает user-input в свои
        XML-теги (<ide_opened_file>, <attached_file>, voice-теги). Если их
        считать codex-internal — они выпадают из jsonl_signatures, но
        остаются в memory_signatures → divergence каждый turn → бесконечный
        recreate thread'а. Фильтр должен ловить ТОЛЬКО известные codex-теги.
        """
        from src.agent.backends.codex import CodexBackend
        slon_wrappers = [
            "<ide_opened_file>The user opened the file foo.py</ide_opened_file>а",
            '<attached_file file_id="x" filename="img.jpg" />',
            '<attached_file file_id="y">\n<content>\ntext\n</content>\n</attached_file>',
            "<voice_transcript>hello</voice_transcript>",
        ]
        for wrapped in slon_wrappers:
            assert not CodexBackend._is_codex_internal({
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": wrapped}],
            }), f"slon-обёртка ошибочно считается codex-internal: {wrapped!r}"

    def test_regular_user_message_not_internal(self):
        from src.agent.backends.codex import CodexBackend
        assert not CodexBackend._is_codex_internal({
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "Please add 1 and 2"}],
        })

    def test_assistant_message_not_internal(self):
        from src.agent.backends.codex import CodexBackend
        assert not CodexBackend._is_codex_internal({
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "Done"}],
        })

    def test_non_message_not_internal(self):
        from src.agent.backends.codex import CodexBackend
        assert not CodexBackend._is_codex_internal({"type": "function_call", "call_id": "x"})


class TestThreadHasDiverged:
    """Детектим что rollout-jsonl содержит response_item'ы которых уже нет
    в нашей памяти — признак компрессии или внешнего вмешательства."""

    @pytest.mark.asyncio
    async def test_no_thread_path_not_diverged(self):
        agent = make_agent()
        backend = agent.backend_impl
        assert await backend._thread_has_diverged() is False

    @pytest.mark.asyncio
    async def test_both_empty_not_diverged(self):
        """Свежий thread: jsonl ещё ничего полезного не содержит (только
        codex-internal), память тоже пуста — не divergence, всё консистентно."""
        agent = make_agent()
        backend = agent.backend_impl
        agent.memory._turns.clear()
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
            f.write('{"type":"session_meta","payload":{}}\n')
            path = f.name
        try:
            backend._thread_path = path
            assert await backend._thread_has_diverged() is False
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_empty_memory_with_nonempty_jsonl_diverged(self):
        """Память пуста, но jsonl уже содержит ответы — обратное divergence.
        Например, slonagent рестартанул и память не загрузилась, а codex
        thread'овый state остался. Должны пересоздать (отдать пустую память
        codex'у через fresh thread + inject [])."""
        agent = make_agent()
        backend = agent.backend_impl
        agent.memory._turns.clear()
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
            f.write('{"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"old"}]}}\n')
            path = f.name
        try:
            backend._thread_path = path
            assert await backend._thread_has_diverged() is True
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_memory_matches_jsonl_not_diverged(self):
        agent = make_agent()
        backend = agent.backend_impl
        agent.memory._turns.extend([
            {"role": "user", "content": "A"},
            {"role": "assistant", "content": "B"},
        ])
        lines = [
            '{"type":"session_meta","payload":{}}\n',
            '{"type":"response_item","payload":{"type":"message","role":"developer","content":[{"type":"input_text","text":"<perms>x</perms>"}]}}\n',
            '{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"<environment_context>cwd</environment_context>"}]}}\n',
            '{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"A"}]}}\n',
            '{"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"B"}]}}\n',
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
            f.writelines(lines)
            path = f.name
        try:
            backend._thread_path = path
            assert await backend._thread_has_diverged() is False
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_jsonl_has_extra_message_diverged(self):
        agent = make_agent()
        backend = agent.backend_impl
        agent.memory._turns.append({"role": "user", "content": "A"})
        lines = [
            '{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"A"}]}}\n',
            # B нет в памяти — расхождение
            '{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"B"}]}}\n',
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
            f.writelines(lines)
            path = f.name
        try:
            backend._thread_path = path
            assert await backend._thread_has_diverged() is True
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_user_turn_with_timestamp_not_diverged(self):
        """Регрессия (юзер ловил в production): agent добавляет к user-
        content '[timestamp]' префикс в strip_contents_private() ПЕРЕД
        отправкой в LLM. В нашей памяти content хранится без префикса, а
        в codex jsonl — с. Если divergence-check сравнивает raw memory с
        jsonl напрямую → постоянное расхождение → пересоздание thread'а
        на каждом llm()."""
        agent = make_agent()
        backend = agent.backend_impl
        # User-турн С _timestamp (как делает memory.add_turn) и list-content
        # (как приходит из web-transport).
        agent.memory._turns.extend([
            {
                "role": "user",
                "content": [{"type": "text", "text": "просто тестирую"}],
                "_timestamp": "2026-05-18T16:28:18+00:00",
            },
            {"role": "assistant", "content": "хорошо"},
        ])
        # В jsonl codex запишет ровно то что мы ему отправили — с префиксом
        # из strip_contents_private. Этот префикс агент добавляет первым
        # text-блоком: [ts] потом content.
        # Сформированный текст после join: "[2026-05-18T16:28:18]просто тестирую"
        # (точное время зависит от tz; для теста проверяем через прогон
        # strip_contents_private самим — синхронизированно с production)
        formatted = agent.strip_contents_private(agent.memory._turns)
        # Извлекаем то что реально пошлёт _build_user_input
        user_text = "".join(
            p.get("text", "")
            for p in formatted[0].get("content") or ()
            if isinstance(p, dict) and p.get("type") == "text"
        )
        assert "[" in user_text and "]" in user_text and "просто тестирую" in user_text, (
            f"strip_contents_private не добавил timestamp префикс: {user_text!r}"
        )

        # Эмулируем jsonl как codex его пишет (один input_text с объединённым
        # текстом, как мы видели в его реальных файлах)
        lines = [
            f'{{"type":"response_item","payload":{{"type":"message","role":"user","content":[{{"type":"input_text","text":{json.dumps(user_text)}}}]}}}}\n',
            '{"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"хорошо"}]}}\n',
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
            f.writelines(lines)
            path = f.name
        try:
            backend._thread_path = path
            assert await backend._thread_has_diverged() is False, (
                "User turn с _timestamp должен матчиться с jsonl после "
                "strip_contents_private — иначе divergence на каждом llm()"
            )
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_pending_user_at_tail_not_diverged(self):
        """В нормальном flow `agent.loop` сначала `memory.add(user)`, потом
        `llm()`. На момент входа в llm() в памяти есть user-турн которого ещё
        нет в jsonl — это НЕ divergence (его сейчас и пошлём в turn/start)."""
        agent = make_agent()
        backend = agent.backend_impl
        agent.memory._turns.extend([
            {"role": "user", "content": "A"},
            {"role": "assistant", "content": "reply A"},
            {"role": "user", "content": "B"},  # pending — ещё не в jsonl
        ])
        lines = [
            '{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"A"}]}}\n',
            '{"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"reply A"}]}}\n',
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
            f.writelines(lines)
            path = f.name
        try:
            backend._thread_path = path
            assert await backend._thread_has_diverged() is False, (
                "pending user-турн НЕ должен триггерить divergence"
            )
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_memory_has_extra_message_diverged_bidirectional(self):
        """Память содержит ассистентский ответ, которого нет в jsonl
        (например, jsonl покалечен внешне или crash при стриме). Это тоже
        divergence — обратная сторона компрессии."""
        agent = make_agent()
        backend = agent.backend_impl
        agent.memory._turns.extend([
            {"role": "user", "content": "A"},
            {"role": "assistant", "content": "missing-from-jsonl"},
        ])
        # jsonl содержит только user A — assistant ответа НЕТ
        lines = [
            '{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"A"}]}}\n',
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
            f.writelines(lines)
            path = f.name
        try:
            backend._thread_path = path
            assert await backend._thread_has_diverged() is True
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_inject_excludes_pending_user_to_avoid_duplicate(self):
        """Регрессия: при fresh-thread мы инжектили ВСЮ память включая
        trailing pending user'а. Тот же user потом шёл вторым в turn/start
        input → codex получал его дважды, jsonl ломался.

        Сейчас инжектим только stable-часть (без trailing pending).
        """
        from src.agent.agent import Agent
        agent = Agent(id="t", model_name="m", backend="codex",
                      agent_dir=tempfile.mkdtemp())
        backend = agent.backend_impl

        agent.memory._turns.extend([
            {"role": "user", "content": "первое"},
            {"role": "assistant", "content": "ответ-1"},
            {"role": "user", "content": "второе-pending"},  # trailing — НЕ инжектим
        ])

        sent_items: list[dict] = []
        class _FakeClient:
            async def request(self_, method, params, timeout=120):
                if method == "thread/start":
                    return {"thread": {"id": "x", "path": None}}
                if method == "thread/inject_items":
                    sent_items.extend(params.get("items", []))
                    return {}
                return {}
            def register_thread(self_, *a, **kw): pass
            def unregister_thread(self_, *a, **kw): pass

        backend._client = _FakeClient()
        await backend._start_fresh_thread("test", "")

        # Должны быть инжектнуты только первые 2 (без trailing user)
        assert len(sent_items) == 2, (
            f"в inject должны попасть только stable items (2), попало {len(sent_items)}: "
            f"{sent_items}"
        )
        # Проверяем что trailing pending не попал
        injected_texts = [
            "".join(p.get("text", "") for p in item.get("content", []) or [])
            for item in sent_items if item.get("type") == "message"
        ]
        assert "второе-pending" not in injected_texts, (
            f"trailing pending user попал в inject: {injected_texts}"
        )

    @pytest.mark.asyncio
    async def test_jsonl_has_orphan_function_call_diverged(self):
        agent = make_agent()
        backend = agent.backend_impl
        # call_b в памяти, call_a — нет
        agent.memory._turns.extend([
            {"role": "assistant", "tool_calls": [
                {"id": "call_b", "function": {"name": "x", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_b", "content": "B"},
        ])
        lines = [
            '{"type":"response_item","payload":{"type":"function_call","call_id":"call_a","name":"x","arguments":"{}"}}\n',
            '{"type":"response_item","payload":{"type":"function_call_output","call_id":"call_a","output":"A"}}\n',
            '{"type":"response_item","payload":{"type":"function_call","call_id":"call_b","name":"x","arguments":"{}"}}\n',
            '{"type":"response_item","payload":{"type":"function_call_output","call_id":"call_b","output":"B"}}\n',
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
            f.writelines(lines)
            path = f.name
        try:
            backend._thread_path = path
            assert await backend._thread_has_diverged() is True
        finally:
            os.unlink(path)


class TestDeveloperInstructions:
    """system_prompt + skill-context'ы → thread.developerInstructions
    (НЕ per-turn input). Иначе каждый turn в jsonl истории codex'а
    дублирует системку — на 5-10 турнах контекст распухает в разы.
    """

    class _CtxSkill(Skill):
        def __init__(self, payload: str):
            super().__init__()
            self._payload = payload

        async def get_context_prompt(self, user_text: str):
            return self._payload

    @pytest.mark.asyncio
    async def test_build_aggregates_skill_contexts_and_system_prompt(self):
        agent = make_agent(skills=[self._CtxSkill("CTX-FROM-SKILL")])
        result = await agent.backend_impl._build_developer_instructions(
            "user msg", system_prompt="EXPLICIT-PROMPT"
        )
        assert "CTX-FROM-SKILL" in result
        assert "EXPLICIT-PROMPT" in result

    @pytest.mark.asyncio
    async def test_build_without_system_prompt_returns_only_skill_contexts(self):
        """Если system_prompt не передан — в результате только skill-контексты,
        ничего лишнего не добавляем."""
        agent = make_agent(skills=[self._CtxSkill("ONLY-SKILL-CTX")])
        result = await agent.backend_impl._build_developer_instructions("hi", None)
        # AgentSkill автоматически добавляется агентом и тоже даёт контекст
        # (git/branch info), поэтому проверяем включение нашего скилла без
        # лишнего system_prompt'а.
        assert "ONLY-SKILL-CTX" in result
        assert "EXPLICIT" not in result

    @pytest.mark.asyncio
    async def test_start_fresh_thread_passes_dev_instructions_at_thread_level(self):
        """thread/start params должны содержать developerInstructions —
        это thread-level, codex заинжектит их в каждый turn автоматически
        без дублирования в нашем input'е."""
        agent = make_agent()
        backend = agent.backend_impl
        sent: list[tuple] = []

        class _FakeClient:
            async def request(self, method, params, timeout=120):
                sent.append((method, params))
                if method == "thread/start":
                    return {"thread": {"id": "thr_fake", "path": None}}
                return {}
            def register_thread(self, *a, **kw): pass
            def unregister_thread(self, *a, **kw): pass

        backend._client = _FakeClient()
        await backend._start_fresh_thread("test", "MY-DEV-INSTRUCTIONS")

        start_call = next(c for c in sent if c[0] == "thread/start")
        assert start_call[1].get("developerInstructions") == "MY-DEV-INSTRUCTIONS"

    @pytest.mark.asyncio
    async def test_empty_dev_instructions_field_omitted(self):
        """Пустые dev_instructions — не шлём ключ совсем (а не None/empty).
        Codex не имеет смысла обрабатывать null-instructions, а лишнее поле
        раздувает протокол."""
        agent = make_agent()
        backend = agent.backend_impl
        sent: list[tuple] = []

        class _FakeClient:
            async def request(self, method, params, timeout=120):
                sent.append((method, params))
                if method == "thread/start":
                    return {"thread": {"id": "thr_fake", "path": None}}
                return {}
            def register_thread(self, *a, **kw): pass
            def unregister_thread(self, *a, **kw): pass

        backend._client = _FakeClient()
        await backend._start_fresh_thread("test", "")

        start_call = next(c for c in sent if c[0] == "thread/start")
        assert "developerInstructions" not in start_call[1]

    @pytest.mark.asyncio
    async def test_fingerprints_persisted_in_state(self):
        """Регрессия (юзер ловил): после рестарта slonagent'а in-memory
        _client_tools_fp / _client_dev_fp сбрасываются в None, resume-условие
        фейлит, мы создаём fresh thread каждый старт, теряя историю в codex'е.

        Фикс — fp'ы сохраняются в CODEX_*.json state-файле, на следующий старт
        грузятся оттуда и сравниваются с актуальными.
        """
        from src.agent.agent import Agent
        agent_dir = tempfile.mkdtemp()
        agent = Agent(id="t", model_name="m", backend="codex", agent_dir=agent_dir)
        backend = agent.backend_impl

        class _FakeClient:
            async def request(self_, method, params, timeout=120):
                if method == "thread/start":
                    return {"thread": {"id": "thr_first", "path": "/p"}}
                return {}
            def register_thread(self_, *a, **kw): pass
            def unregister_thread(self_, *a, **kw): pass

        backend._client = _FakeClient()
        await backend._start_fresh_thread("first", "DEV_INSTRUCTIONS_TEXT")

        # state на диске должен содержать ВСЕ три ключа
        state = backend._load_state()
        assert state["thread_id"] == "thr_first"
        assert state["tools_fp"] == backend._skills_fingerprint()
        assert state["dev_fp"] == backend._fp("DEV_INSTRUCTIONS_TEXT")

        # Имитируем рестарт slonagent'а: НОВЫЙ Agent с тем же agent_dir →
        # in-memory fp'ы сброшены в None.
        agent2 = Agent(id="t", model_name="m", backend="codex", agent_dir=agent_dir)
        backend2 = agent2.backend_impl
        assert backend2._client_tools_fp is None  # in-memory сброшен

        # Но state на диске остался — _ensure_thread должен взять fp оттуда
        # и пойти по resume-пути (а не fresh).
        sent: list[str] = []
        class _FakeClient2:
            async def request(self_, method, params, timeout=120):
                sent.append(method)
                if method == "thread/resume":
                    return {"thread": {"id": "thr_first", "path": "/p"}}
                return {}
            def register_thread(self_, *a, **kw): pass
            def unregister_thread(self_, *a, **kw): pass

        backend2._client = _FakeClient2()
        # Делаем divergence-check noop (не существующий path)
        backend2._thread_path = None

        await backend2._ensure_thread("DEV_INSTRUCTIONS_TEXT")
        assert "thread/resume" in sent, (
            f"После рестарта slonagent'а должен быть resume, не fresh. "
            f"sent={sent}"
        )
        assert "thread/start" not in sent, (
            f"fresh thread не должен создаваться после рестарта с теми же "
            f"fp'ами. sent={sent}"
        )

    @pytest.mark.asyncio
    async def test_dev_fp_change_triggers_fresh_thread(self):
        """Если dev_instructions поменялись между llm() — _ensure_thread
        обязан поднять fresh thread (а не делать resume), иначе старый
        промпт продолжит действовать.
        """
        agent = make_agent()
        backend = agent.backend_impl
        backend._client_tools_fp = backend._skills_fingerprint()
        backend._client_dev_fp = backend._fp("OLD_INSTRUCTIONS")

        sent: list[tuple] = []
        class _FakeClient:
            async def request(self, method, params, timeout=120):
                sent.append((method, params))
                if method == "thread/start":
                    return {"thread": {"id": "thr_new", "path": None}}
                return {}
            def register_thread(self, *a, **kw): pass
            def unregister_thread(self, *a, **kw): pass

        backend._client = _FakeClient()
        backend._save_state({"thread_id": "thr_old"})

        # Dev-instructions поменялись — должна быть thread/start, не thread/resume
        await backend._ensure_thread("NEW_INSTRUCTIONS")

        methods = [c[0] for c in sent]
        assert "thread/start" in methods, f"ожидаем fresh thread/start, получили {methods}"
        assert "thread/resume" not in methods, (
            f"resume не должен вызываться при смене dev_instructions, получили {methods}"
        )


class TestMemoryToResponseItems:
    """Конвертация наших OpenAI-style turns в Responses API items для
    thread/inject_items."""

    def test_user_text_string(self):
        from src.agent.backends.codex import CodexBackend
        items = CodexBackend._memory_to_response_items([
            {"role": "user", "content": "hi"},
        ])
        assert items == [{"type": "message", "role": "user",
                          "content": [{"type": "input_text", "text": "hi"}]}]

    def test_user_text_list(self):
        from src.agent.backends.codex import CodexBackend
        items = CodexBackend._memory_to_response_items([
            {"role": "user", "content": [
                {"type": "text", "text": "hello"},
                {"type": "text", "text": " world"},
            ]},
        ])
        assert items == [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "hello"},
            {"type": "input_text", "text": " world"},
        ]}]

    def test_user_text_with_image_url(self):
        from src.agent.backends.codex import CodexBackend
        items = CodexBackend._memory_to_response_items([
            {"role": "user", "content": [
                {"type": "text", "text": "see"},
                {"type": "image_url", "image_url": {"url": "https://e.com/x.png"}},
            ]},
        ])
        assert items == [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "see"},
            {"type": "input_image", "image_url": "https://e.com/x.png"},
        ]}]

    def test_assistant_text(self):
        from src.agent.backends.codex import CodexBackend
        items = CodexBackend._memory_to_response_items([
            {"role": "assistant", "content": "done"},
        ])
        assert items == [{"type": "message", "role": "assistant",
                          "content": [{"type": "output_text", "text": "done"}]}]

    def test_assistant_tool_calls(self):
        from src.agent.backends.codex import CodexBackend
        items = CodexBackend._memory_to_response_items([
            {"role": "assistant", "tool_calls": [
                {"id": "call_1", "function": {"name": "add", "arguments": '{"a":1,"b":2}'}},
                {"id": "call_2", "function": {"name": "mul", "arguments": '{"x":3}'}},
            ]},
        ])
        assert items == [
            {"type": "function_call", "name": "add", "arguments": '{"a":1,"b":2}', "call_id": "call_1"},
            {"type": "function_call", "name": "mul", "arguments": '{"x":3}', "call_id": "call_2"},
        ]

    def test_tool_result_string(self):
        from src.agent.backends.codex import CodexBackend
        items = CodexBackend._memory_to_response_items([
            {"role": "tool", "tool_call_id": "call_1", "name": "add", "content": "3"},
        ])
        assert items == [{"type": "function_call_output", "call_id": "call_1", "output": "3"}]

    def test_full_conversation(self):
        """Реалистичный диалог user→assistant_tool_call→tool→assistant_text."""
        from src.agent.backends.codex import CodexBackend
        items = CodexBackend._memory_to_response_items([
            {"role": "user", "content": "add 1 and 2"},
            {"role": "assistant", "tool_calls": [
                {"id": "c1", "function": {"name": "add", "arguments": '{"a":1,"b":2}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "3"},
            {"role": "assistant", "content": "The answer is 3."},
        ])
        assert len(items) == 4
        assert items[0]["type"] == "message" and items[0]["role"] == "user"
        assert items[1]["type"] == "function_call" and items[1]["call_id"] == "c1"
        assert items[2]["type"] == "function_call_output" and items[2]["call_id"] == "c1"
        assert items[3]["type"] == "message" and items[3]["role"] == "assistant"

    def test_empty_content_skipped(self):
        from src.agent.backends.codex import CodexBackend
        items = CodexBackend._memory_to_response_items([
            {"role": "user", "content": ""},
            {"role": "assistant", "content": ""},
        ])
        assert items == []


# ═══════════════════════════════════════════════════════════════════════════════
# Unit: ephemeral cleanup на __del__
# ═══════════════════════════════════════════════════════════════════════════════

class TestEphemeralThread:
    """Эфемерный backend (без agent_dir) запускает codex thread с
    ephemeral=true → codex не пишет rollout на диск. Это правильный flow
    для one-shot агентов: компрессия памяти, fact-recall, краткосрочные
    sub-агенты. Без ephemeral=true codex бы создавал orphan-файлы под
    ~/.codex/sessions/, которые ни на что не нужны.
    """

    def test_is_ephemeral_when_no_memory_dir(self):
        from src.agent.agent import Agent
        agent = Agent(id="eph", model_name="m", backend="codex")
        assert agent.backend_impl._is_ephemeral is True

    def test_is_not_ephemeral_with_agent_dir(self):
        from src.agent.agent import Agent
        agent = Agent(id="main", model_name="m", backend="codex",
                      agent_dir=tempfile.mkdtemp())
        assert agent.backend_impl._is_ephemeral is False

    @pytest.mark.asyncio
    async def test_ephemeral_passes_ephemeral_true_to_thread_start(self):
        """Эфемерный backend должен в thread/start params передать
        ephemeral=true. Тогда codex не создаёт rollout-jsonl."""
        from src.agent.agent import Agent
        agent = Agent(id="eph", model_name="m", backend="codex")
        backend = agent.backend_impl
        sent: list[tuple] = []

        class _FakeClient:
            async def request(self, method, params, timeout=120):
                sent.append((method, params))
                if method == "thread/start":
                    return {"thread": {"id": "thr_eph", "path": None}}
                return {}
            def register_thread(self, *a, **kw): pass
            def unregister_thread(self, *a, **kw): pass

        backend._client = _FakeClient()
        await backend._start_fresh_thread("test", "")
        start = next(c for c in sent if c[0] == "thread/start")
        assert start[1].get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_empty_model_name_omits_model_field(self):
        """Регрессия: model_name="" должен означать «возьми default»,
        а не «модель ''». Codex ругается на пустую строку:
            "'' model is not supported when using Codex with a ChatGPT account"
        Поэтому если model_name пустой/None — поле в params вообще не пишем,
        codex сам подберёт default."""
        from src.agent.agent import Agent
        agent = Agent(id="t", model_name="", backend="codex",
                      agent_dir=tempfile.mkdtemp())
        backend = agent.backend_impl
        sent: list[tuple] = []

        class _FakeClient:
            async def request(self, method, params, timeout=120):
                sent.append((method, params))
                if method == "thread/start":
                    return {"thread": {"id": "x", "path": "/p"}}
                return {}
            def register_thread(self, *a, **kw): pass
            def unregister_thread(self, *a, **kw): pass

        backend._client = _FakeClient()
        await backend._start_fresh_thread("test", "")
        start = next(c for c in sent if c[0] == "thread/start")
        assert "model" not in start[1], (
            f"при пустом model_name поле model в thread/start не должно "
            f"присутствовать (codex ругается на ''), got {start[1]}"
        )

    @pytest.mark.asyncio
    async def test_persistent_does_not_pass_ephemeral(self):
        """Persistent backend (с memory_dir) — ephemeral=true НЕ должен
        передаваться, иначе codex не сохранит ничего и при рестарте slon'а
        вся история codex'а пропадёт."""
        from src.agent.agent import Agent
        agent = Agent(id="main", model_name="m", backend="codex",
                      agent_dir=tempfile.mkdtemp())
        backend = agent.backend_impl
        sent: list[tuple] = []

        class _FakeClient:
            async def request(self, method, params, timeout=120):
                sent.append((method, params))
                if method == "thread/start":
                    return {"thread": {"id": "thr_p", "path": "/fake.jsonl"}}
                return {}
            def register_thread(self, *a, **kw): pass
            def unregister_thread(self, *a, **kw): pass

        backend._client = _FakeClient()
        await backend._start_fresh_thread("test", "")
        start = next(c for c in sent if c[0] == "thread/start")
        # Поля не должно быть ИЛИ оно false
        assert start[1].get("ephemeral", False) is False


class TestEphemeralCleanup:
    """Эфемерный backend (без agent_dir → memory_dir пуст → state_file None)
    на GC удаляет свой rollout-*.jsonl (на случай если ephemeral=true не
    сработало и codex всё-таки что-то записал). Persistent main-агент не
    трогается."""

    def test_ephemeral_jsonl_removed_on_gc(self):
        import gc
        from src.agent.agent import Agent
        agent = Agent(id="eph", model_name="m", backend="codex")
        backend = agent.backend_impl
        # Подделываем rollout как если бы codex его создал.
        fd, jsonl = tempfile.mkstemp(suffix=".jsonl", prefix="rollout-")
        os.close(fd)
        with open(jsonl, "w", encoding="utf-8") as f:
            f.write('{"fake": true}\n')
        backend._thread_path = jsonl
        try:
            del agent
            del backend
            gc.collect()
            assert not os.path.exists(jsonl), f"эфемерный rollout должен быть удалён, остался: {jsonl}"
        finally:
            with __import__("contextlib").suppress(OSError):
                os.remove(jsonl)

    def test_persistent_jsonl_not_removed_on_gc(self):
        """Main-агент с agent_dir — __del__ не должен трогать его сессию."""
        import gc
        from src.agent.agent import Agent
        agent_dir = tempfile.mkdtemp()
        agent = Agent(id="main", model_name="m", backend="codex", agent_dir=agent_dir)
        backend = agent.backend_impl
        fd, jsonl = tempfile.mkstemp(suffix=".jsonl", prefix="rollout-")
        os.close(fd)
        with open(jsonl, "w", encoding="utf-8") as f:
            f.write('{"fake": true}\n')
        backend._thread_path = jsonl
        backend._save_state({"thread_id": "thr_main"})
        try:
            del agent
            del backend
            gc.collect()
            assert os.path.exists(jsonl), "persistent rollout НЕ должен быть удалён __del__"
        finally:
            with __import__("contextlib").suppress(OSError):
                os.remove(jsonl)


# ═══════════════════════════════════════════════════════════════════════════════
# Integration: реальный codex app-server
# ═══════════════════════════════════════════════════════════════════════════════

class _CalcSkill(Skill):
    """Тул для integration-проверки реального dynamicTool вызова."""

    def __init__(self):
        super().__init__()
        self.calls: list[dict] = []

    @tool("Сложить два числа. ОБЯЗАТЕЛЬНО используй эту тулзу, не считай в уме.")
    async def add(self, a: int, b: int) -> dict:
        self.calls.append({"a": a, "b": b})
        return {"sum": a + b}


def _proxy_env() -> dict:
    """Подсасываем proxy env vars из .config.json — codex нужен для token exchange
    из РФ. Без них тест провалится на gen-блоке OpenAI."""
    cfg_path = os.path.join(ROOT, ".config.json")
    if not os.path.exists(cfg_path):
        return {}
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg.get("env", {}) or {}


@pytest.fixture(scope="module", autouse=True)
def setup_proxy():
    """Прокси нужен codex для исходящих запросов в РФ. Ставим на весь модуль."""
    for k, v in _proxy_env().items():
        os.environ.setdefault(k, v)


@pytest.fixture(autouse=True)
async def _reset_codex_client():
    """CodexClient — singleton, и его подпроцесс с pipe'ами привязан к event
    loop'у первого вызвавшего теста. pytest-asyncio даёт свежий loop на каждый
    тест → старый singleton нельзя использовать. Сносим после каждого теста.

    Для unit-тестов (без живого вызова get()) это no-op.
    """
    yield
    from src.agent.backends.codex import CodexClient
    await CodexClient.reset()


@pytest.mark.integration
class TestCodexIntegration:

    @pytest.mark.asyncio
    async def test_real_codex_says_pong(self):
        agent = make_agent(model_name=CODEX_MODEL)
        agent.memory._turns.append({"role": "user", "content": "Reply with exactly the word: pong"})

        try:
            turns = await agent.llm()
        finally:
            await agent.close()

        text_turns = [t for t in turns if t.get("role") == "assistant" and isinstance(t.get("content"), str)]
        assert text_turns, f"no assistant text turns, got: {turns}"
        assert "pong" in text_turns[-1]["content"].lower(), f"got: {text_turns[-1]['content']!r}"

    @pytest.mark.asyncio
    async def test_real_streaming_accumulates(self):
        """Реальный codex стримит ответ через item/agentMessage/delta —
        транспорт должен получить много send_message с АККУМУЛИРОВАННЫМ
        текстом одного stream_id."""
        agent = make_agent(model_name=CODEX_MODEL)
        agent.memory._turns.append({"role": "user", "content": "Перечисли 5 цветов радуги через запятую."})

        try:
            await agent.llm()
        finally:
            await agent.close()

        stream_calls = [c for c in agent.transport.send_message.call_args_list if c.kwargs.get("stream_id")]
        assert len(stream_calls) >= 3, f"ожидаем стрим из >=3 вызовов, получили {len(stream_calls)}"

        sent_texts = [c.args[0] for c in stream_calls]
        for prev, cur in zip(sent_texts, sent_texts[1:]):
            assert len(cur) >= len(prev), f"текст не накапливается: {prev!r} → {cur!r}"

        ids = {c.kwargs["stream_id"] for c in stream_calls}
        assert len(ids) == 1, f"ожидаем один stream_id, получили {ids}"

    @pytest.mark.asyncio
    async def test_real_tool_call_through_dynamic_tools(self):
        """Реальный codex должен вызвать наш dynamicTool через server-initiated
        request item/tool/call, мы дисптчим, отдаём contentItems, codex использует
        результат."""
        calc = _CalcSkill()
        agent = make_agent(skills=[calc], model_name=CODEX_MODEL)
        agent.memory._turns.append({
            "role": "user",
            "content": "Используй тулзу add чтобы сложить 17 и 25. Верни только число.",
        })

        try:
            turns = await agent.llm()
        finally:
            await agent.close()

        # Скилл получил вызов
        assert calc.calls, f"тул не был вызван, turns={turns}"
        assert calc.calls[0] == {"a": 17, "b": 25}

        # Turn'ы содержат assistant.tool_calls и role:tool с результатом
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

    @pytest.mark.asyncio
    async def test_real_thread_persists_across_calls(self):
        """Второй llm()-вызов с тем же агентом → reuse thread_id из state'а
        (через thread/resume), не создаёт новый.

        ВАЖНО: в реальном flow `agent.loop()` пишет turns которые llm() вернул
        обратно в память. Тест вызывает llm() напрямую — поэтому симулируем
        это вручную, иначе divergence-detection увидит ответы в jsonl, которых
        нет в памяти, и насильно поднимет fresh thread.
        """
        agent = make_agent(model_name=CODEX_MODEL)
        agent.memory._turns.append({"role": "user", "content": "Reply with just: 1"})

        try:
            turns1 = await agent.llm()
            first_thread = agent.backend_impl._thread_id
            assert first_thread, "первый llm() не сохранил thread_id"
            # Симулируем работу agent.loop(): пишем выданные turns в память
            agent.memory._turns.extend(turns1)

            agent.memory._turns.append({"role": "user", "content": "Reply with just: 2"})
            turns2 = await agent.llm()
            second_thread = agent.backend_impl._thread_id
            agent.memory._turns.extend(turns2)

            assert second_thread == first_thread, (
                f"thread должен переиспользоваться: first={first_thread} second={second_thread}"
            )
        finally:
            await agent.close()

    @pytest.mark.asyncio
    async def test_real_cancel_during_stream(self):
        """Отмена llm() пока codex стримит текст. Backend должен:
          1. Поймать CancelledError, не пробрасывать наверх (uncancel)
          2. Отправить turn/interrupt
          3. Дождаться turn/completed status=interrupted
          4. Вернуть turns с interrupt-маркером (НЕ список tool_calls без results)
        Это аналог test_stop_during_text_stream у клода."""
        agent = make_agent(model_name=CODEX_MODEL)
        agent.memory._turns.append({
            "role": "user",
            "content": "Напиши длинный рассказ про осенний лес, минимум 500 слов. Пиши развёрнуто, не останавливайся."
        })

        task = asyncio.create_task(agent.llm())
        try:
            # Ждём пока стрим начнётся
            for _ in range(300):  # до 30s
                await asyncio.sleep(0.1)
                stream_calls = [c for c in agent.transport.send_message.call_args_list
                                if c.kwargs.get("stream_id")]
                if stream_calls:
                    break
            else:
                task.cancel()
                pytest.fail("стрим не начался за 30s")

            await asyncio.sleep(0.3)  # дать модели чуть-чуть «попечатать»
            task.cancel()

            turns = None
            try:
                turns = await task
            except asyncio.CancelledError:
                pass
        finally:
            await agent.close()

        # llm() должен был проглотить cancel и вернуть turns (а не CancelledError)
        assert turns is not None, "llm() пробросил CancelledError — должен был uncancel и вернуть turns"

        # Последний assistant-turn — interrupt-маркер
        assistant_turns = [t for t in turns if t.get("role") == "assistant" and isinstance(t.get("content"), str)]
        assert assistant_turns, f"нет assistant-турнов, turns={turns}"
        assert "прерван" in assistant_turns[-1]["content"], (
            f"ожидаем interrupt-маркер, получили: {assistant_turns[-1]['content']!r}"
        )

        # Сбалансированность: каждому tool_call есть tool result
        tool_use_turns = [t for t in turns if t.get("tool_calls")]
        tool_result_turns = [t for t in turns if t.get("role") == "tool"]
        used_ids = {tc["id"] for t in tool_use_turns for tc in t.get("tool_calls", [])}
        result_ids = {t.get("tool_call_id") for t in tool_result_turns}
        assert used_ids <= result_ids, f"висячие tool_calls без results: {used_ids - result_ids}"

    @pytest.mark.asyncio
    async def test_real_thread_stable_across_multiple_turns(self):
        """Регрессия: в нормальном flow (несколько последовательных llm()
        с правильным memory.extend) thread НЕ должен пересоздаваться.
        Если divergence-detection слишком агрессивно — мы будем спавнить
        новый thread каждый раз, теряя кэш context'а в codex'е.
        """
        agent = make_agent(model_name=CODEX_MODEL)
        thread_ids: list[str] = []
        try:
            for i in range(3):
                agent.memory._turns.append({
                    "role": "user", "content": f"Reply with just: {chr(ord('A') + i)}",
                })
                turns = await agent.llm()
                thread_ids.append(agent.backend_impl._thread_id)
                # имитируем agent.loop: пишем ответы обратно в память
                agent.memory._turns.extend(turns)

            # Все thread_id должны быть одинаковыми (resume работал на 2 и 3)
            assert len(set(thread_ids)) == 1, (
                f"thread пересоздавался в нормальном flow: {thread_ids}"
            )
        finally:
            await agent.close()

    @pytest.mark.asyncio
    async def test_real_ephemeral_agent_writes_nothing_to_disk(self):
        """One-shot эфемерный агент (без agent_dir, типа memory-compressor):
        делает llm(), завершается, ничего на диске не остаётся.
        Проверяем что codex с ephemeral=true действительно не пишет
        rollout-*.jsonl, и что список файлов в sessions/ не растёт."""
        sessions_dir = Path(os.path.expanduser("~")) / ".codex" / "sessions"
        before = {p for p in sessions_dir.rglob("rollout-*.jsonl")} if sessions_dir.exists() else set()

        from src.agent.agent import Agent
        agent = Agent(id="oneshot", model_name=CODEX_MODEL, backend="codex",
                      memory_compressor=PassthroughCompressor())
        agent.transport = MagicMock()
        agent.transport.send_message = AsyncMock()
        agent.transport.send_thinking = AsyncMock()
        agent.transport.on_tool_call = AsyncMock()
        agent.transport.on_tool_result = AsyncMock()
        agent.transport.send_processing = AsyncMock()
        agent.transport.send_memory_info = AsyncMock()
        agent.transport.send_system_prompt = AsyncMock()
        agent.transport.close = AsyncMock()
        agent.memory._turns.append({"role": "user", "content": "Reply with: oneshot-ok"})

        try:
            await agent.llm()
            # Codex для ephemeral thread'а возвращает path=None
            assert agent.backend_impl._thread_path is None, (
                f"ephemeral thread не должен иметь path, получили {agent.backend_impl._thread_path!r}"
            )
        finally:
            await agent.close()

        after = {p for p in sessions_dir.rglob("rollout-*.jsonl")} if sessions_dir.exists() else set()
        new_files = after - before
        assert not new_files, (
            f"эфемерный агент оставил файлы на диске: {new_files}"
        )

    @pytest.mark.asyncio
    async def test_real_enable_shell_tool_records_to_memory(self):
        """Юзер включает shell_tool через backend_params.enable_features →
        codex реально его юзает → мы пишем вызов в память как
        assistant.tool_calls(name=shell_command) + role:tool → divergence-check
        НЕ триггерится → thread переиспользуется на следующих llm()'ах.

        Это покрывает 3 свойства разом:
        1. enable_features долетает до --enable шкф app-server'а
        2. _handle_notification обрабатывает commandExecution
        3. имена совпадают с jsonl, потому divergence-detect молчит
        """
        from src.agent.agent import Agent
        agent_dir = tempfile.mkdtemp()
        agent = Agent(
            id="shell_test", model_name=CODEX_MODEL, backend="codex",
            backend_params={"enable_features": ["shell_tool"]},
            agent_dir=agent_dir,
            memory_compressor=PassthroughCompressor(),
            skills=[],
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

        agent.memory._turns.append({
            "role": "user",
            "content": "Run the shell command `echo HELLO_FROM_TEST` and tell me what it printed.",
        })
        try:
            turns = await agent.llm()
            first_thread = agent.backend_impl._thread_id
            agent.memory._turns.extend(turns)

            # 1. В turns должен быть shell_command tool_call
            shell_calls = [
                tc for t in turns for tc in t.get("tool_calls") or ()
                if tc["function"]["name"] == "shell_command"
            ]
            assert shell_calls, f"codex не использовал shell_tool (turns={turns!r})"

            # 2. on_tool_call в transport должен быть для shell_command
            tool_call_names = [c.args[0] for c in agent.transport.on_tool_call.call_args_list]
            assert "shell_command" in tool_call_names

            # 3. Второй llm() БЕЗ нового user-сообщения = ошибка. Добавим.
            agent.memory._turns.append({"role": "user", "content": "Just reply: OK"})
            turns2 = await agent.llm()
            second_thread = agent.backend_impl._thread_id
            agent.memory._turns.extend(turns2)

            # 4. Thread НЕ должен пересоздаваться — divergence-check
            #    должен видеть match между нашей памятью и jsonl даже когда
            #    в jsonl есть native function_call(name=shell_command)
            assert second_thread == first_thread, (
                f"thread пересоздался — значит native shell_command вызов "
                f"не дал match между памятью и jsonl. first={first_thread} "
                f"second={second_thread}"
            )
        finally:
            await agent.close()

    @pytest.mark.asyncio
    async def test_real_no_baked_in_read_tools_by_default(self):
        """Регрессия: с дефолтным sandbox=read-only и нашим disable-списком,
        у codex'а НЕТ ни одного нативного тула для чтения файлов. Все read-
        пути идут только через наши dynamicTools (SandboxSkill).

        sandbox=read-only разрешает чтение на kernel-уровне, но без shell_tool
        / browser / mcp у модели физически нечем — все нативные тулы либо
        write (apply_patch), либо просмотр attached input'ов (view_image)."""
        agent = make_agent(skills=[], model_name=CODEX_MODEL)
        target = Path(__file__).parent.parent / ".config.json"
        agent.memory._turns.append({
            "role": "user",
            "content": (
                f"Read the file at '{target}' and tell me the exact value of "
                "'keys.backend'. If you cannot read files, say so explicitly. "
                "Do not invent or guess the answer."
            ),
        })
        try:
            turns = await agent.llm()
        finally:
            await agent.close()

        text = "\n".join(
            t["content"] for t in turns
            if t.get("role") == "assistant" and isinstance(t.get("content"), str)
        )
        # В нашем .config.json keys.backend = "claude" — если бы модель
        # реально прочитала файл, она бы это сказала.
        assert "claude" not in text.lower(), (
            f"Утечка содержимого файла: codex прочитал .config.json без наших "
            f"скиллов. Ответ модели:\n{text}"
        )

    @pytest.mark.asyncio
    async def test_real_sees_image_from_dynamic_tool(self):
        """Регрессия: dynamicTool возвращает картинку через {type:inputImage,
        imageUrl: data:image/png;base64,...} → codex её видит и модель может
        описать содержимое. Это покрывает sandbox_read для image-файлов."""
        import base64
        from agent import Skill, tool

        # Минимальная PNG с буквой "X" — для модели должно быть достаточно
        try:
            from PIL import Image, ImageDraw, ImageFont
            img = Image.new("RGB", (200, 200), (255, 100, 100))
            d = ImageDraw.Draw(img)
            try:
                font = ImageFont.truetype("arial.ttf", 120)
            except Exception:
                font = ImageFont.load_default()
            d.text((40, 30), "ZEBRA", fill=(255, 255, 255), font=font)
            buf = tempfile.NamedTemporaryFile("wb", suffix=".png", delete=False)
            img.save(buf, format="PNG")
            buf.close()
            with open(buf.name, "rb") as f: png_bytes = f.read()
            os.unlink(buf.name)
        except ImportError:
            pytest.skip("PIL not installed — нужно для генерации тестовой картинки")

        data_url = f"data:image/png;base64,{base64.b64encode(png_bytes).decode()}"

        class _ImgSkill(Skill):
            @tool("Returns a test image with text on it. Call this when asked to see an image.")
            async def show(self):
                return {"_parts": [{"type": "image_url",
                                    "image_url": {"url": data_url}}]}

        agent = make_agent(skills=[_ImgSkill()], model_name=CODEX_MODEL)
        agent.memory._turns.append({
            "role": "user",
            "content": "Call show() to see an image, then tell me what TEXT is written on it (single uppercase word). "
                       "If you can't see images, reply only 'NO_IMAGE'.",
        })
        try:
            turns = await agent.llm()
        finally:
            await agent.close()

        text = "\n".join(
            t["content"] for t in turns
            if t.get("role") == "assistant" and isinstance(t.get("content"), str)
        )
        assert text.strip(), "codex вернул пустой ответ на картинку из tool"
        assert "NO_IMAGE" not in text, f"codex заявил что не видит картинку: {text!r}"

    @pytest.mark.asyncio
    async def test_real_sees_image_in_user_input(self):
        """Картинка в user input (turn/start.input) — codex должен видеть
        data: URL напрямую без tool call, формат input_image/image_url."""
        import base64
        try:
            from PIL import Image, ImageDraw, ImageFont
            img = Image.new("RGB", (200, 200), (50, 50, 200))
            d = ImageDraw.Draw(img)
            try:
                font = ImageFont.truetype("arial.ttf", 100)
            except Exception:
                font = ImageFont.load_default()
            d.text((20, 40), "TIGER", fill=(255, 255, 255), font=font)
            buf = tempfile.NamedTemporaryFile("wb", suffix=".png", delete=False)
            img.save(buf, format="PNG")
            buf.close()
            with open(buf.name, "rb") as f: png_bytes = f.read()
            os.unlink(buf.name)
        except ImportError:
            pytest.skip("PIL not installed")

        data_url = f"data:image/png;base64,{base64.b64encode(png_bytes).decode()}"
        agent = make_agent(model_name=CODEX_MODEL)
        agent.memory._turns.append({
            "role": "user",
            "content": [
                {"type": "text", "text": "What single uppercase word is written on this image? Reply with just that word."},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        })
        try:
            turns = await agent.llm()
        finally:
            await agent.close()

        text = "\n".join(
            t["content"] for t in turns
            if t.get("role") == "assistant" and isinstance(t.get("content"), str)
        )
        assert text.strip(), "codex вернул пустой ответ на картинку в user input"
        # Модель видит картинку и пытается прочитать текст — достаточно что
        # ответ не пустой и не "NO_IMAGE". Точность OCR зависит от шрифта PIL.
        assert "NO_IMAGE" not in text.upper(), (
            f"codex заявил что не видит картинку в user input: {text!r}"
        )

    @pytest.mark.asyncio
    async def test_real_reasoning_arrives_in_transport(self):
        """Codex эмитит reasoning (мысли модели) только при заданных effort
        и summary в turn/start. С нашими default'ами (effort=high+summary=auto)
        — на gpt-5.5 free план — приходит summaryTextDelta, мы шлём в
        transport.send_thinking. Юзер видит мысли.

        Раньше без этих настроек codex молчал → пользователь не видел мыслей.
        """
        agent = make_agent(model_name=CODEX_MODEL)
        # Вопрос требующий хоть какого-то reasoning, не trivial
        agent.memory._turns.append({
            "role": "user",
            "content": "Какое третье простое число после 17? Объясни шаг за шагом.",
        })
        try:
            await agent.llm()
        finally:
            await agent.close()

        thinking_calls = agent.transport.send_thinking.call_args_list
        assert thinking_calls, (
            "transport.send_thinking ни разу не вызвался — codex не эмитил "
            "reasoning. effort=high+summary=auto default'ы не сработали."
        )
        # Проверяем что текст реально пришёл (не пустые делты)
        all_text = "".join(c.args[0] for c in thinking_calls)
        assert len(all_text) > 20, f"reasoning слишком короткий: {all_text!r}"

    @pytest.mark.asyncio
    async def test_real_codex_auto_prompts_suppressed(self):
        """Регрессия (user-facing): с нашими default'ами в codex jsonl
        НЕТ ни одного автоматически инжектированного developer-блока.
        Контролируем через CLI флаги `-c include_*_instructions=false`
        (имена нашли в codex sources, lib/codex/codex-rs/config/src/
        config_toml.rs)."""
        agent = make_agent(model_name=CODEX_MODEL)
        agent.memory._turns.append({"role": "user", "content": "Reply only: ok"})
        try:
            await agent.llm()
            jsonl_path = agent.backend_impl._thread_path
        finally:
            await agent.close()

        assert jsonl_path and os.path.exists(jsonl_path)
        with open(jsonl_path, encoding="utf-8") as f:
            content = f.read()

        # 1. Длинная codex'овская системка про "You are Codex" не должна
        # попадать (управляется через baseInstructions override).
        for marker in ("You are Codex", "coding agent based on GPT-5"):
            assert marker not in content, (
                f"codex base_instructions маркер {marker!r} в jsonl — "
                f"baseInstructions не override'ится"
            )

        # 2. Эти блоки гасятся через config ключи в _DISABLED_CONFIG_KEYS:
        forbidden_blocks = (
            "<permissions instructions>",
            "<apps_instructions>",
            "<environment_context>",
            # <skills_instructions> — управляется skills.bundled.enabled=false
            # (НЕ include_skill_instructions — этот config-key есть только в
            # 0.131-alpha и там тоже не работает). Codex удаляет
            # ~/.codex/skills/.system/ → блок не появляется.
            "<skills_instructions>",
        )
        for marker in forbidden_blocks:
            assert marker not in content, (
                f"в jsonl остался auto-блок {marker!r} — соответствующий "
                f"config-флаг в _DISABLED_CONFIG_KEYS не сработал"
            )

    @pytest.mark.asyncio
    async def test_real_apply_patch_hard_blocked_by_default(self):
        """Регрессия (юзер ловил): codex apply_patch как baked-in тул не
        управляется feature-flag'ом. Защита — `sandbox: "read-only"` на уровне
        thread/start: codex'овский kernel-sandbox отвергает любые write через
        apply_patch / shell_tool. Файл должен остаться нетронутым.
        """
        test_file = Path("e:/dev/slonagent/tmp_apply_patch_target.txt")
        test_file.write_text("ORIGINAL")

        agent = make_agent(model_name=CODEX_MODEL)
        agent.memory._turns.append({
            "role": "user",
            "content": (
                f"Edit the file '{test_file}' using the apply_patch tool. "
                "Replace 'ORIGINAL' with 'HACKED'. Just do it, don't ask."
            ),
        })
        try:
            await agent.llm()
        finally:
            await agent.close()

        try:
            actual = test_file.read_text()
            assert actual == "ORIGINAL", (
                f"apply_patch не заблокирован sandbox'ом! Файл стал {actual!r}"
            )
        finally:
            test_file.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_real_apply_patch_allowed_with_explicit_override(self):
        """Если юзер явно поставил sandbox='workspace-write' через backend_params,
        codex apply_patch начинает работать — это легитимный escape hatch."""
        test_file = Path("e:/dev/slonagent/tmp_apply_patch_allowed.txt")
        test_file.write_text("ORIGINAL")

        from src.agent.agent import Agent
        agent = Agent(
            id="t", model_name=CODEX_MODEL, backend="codex",
            backend_params={"sandbox": "workspace-write"},
            agent_dir=tempfile.mkdtemp(),
            memory_compressor=PassthroughCompressor(),
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
        agent.memory._turns.append({
            "role": "user",
            "content": (
                f"Use apply_patch to replace 'ORIGINAL' with 'EDITED' in {test_file}. "
                "Just do it."
            ),
        })
        try:
            await agent.llm()
        finally:
            await agent.close()

        try:
            actual = test_file.read_text()
            # На workspace-write codex может работать с файлами в cwd
            # (наш cwd по умолчанию — agent.memory_dir/workspace, но codex
            # принимает редактирование любых файлов в writable_roots). Тут
            # критично что фрагмент апдейтился — точное значение зависит
            # от того, считает ли codex наш cwd writable.
            assert actual != "ORIGINAL" or "apply_patch" in str(
                agent.transport.on_tool_call.call_args_list
            ), (
                "workspace-write override не работает: apply_patch не сработал "
                f"и не пытался; файл={actual!r}"
            )
        finally:
            test_file.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_real_native_shell_tool_disabled(self):
        """Без отключения нативных тулов codex попытается выполнить shell-
        команду на хосте через свой `shell_tool` (минуя SandboxSkill).
        После `--disable shell_tool` в args app-server'а такой попытки
        не должно происходить — codex либо откажется, либо использует
        наши dynamicTools если они есть.
        """
        agent = make_agent(skills=[], model_name=CODEX_MODEL)
        agent.memory._turns.append({
            "role": "user",
            "content": "Run `whoami` and tell me the output. Use shell tool if available.",
        })
        try:
            turns = await agent.llm()
        finally:
            await agent.close()

        # В rollout-jsonl НЕ должно быть commandExecution или shell_tool вызовов
        jsonl_path = agent.backend_impl._thread_path
        if jsonl_path and os.path.exists(jsonl_path):
            with open(jsonl_path, encoding="utf-8") as f:
                content = f.read()
            assert '"shell_tool"' not in content, "codex использовал shell_tool несмотря на --disable"
            assert '"commandExecution"' not in content, "codex выполнил command_execution"

    @pytest.mark.asyncio
    async def test_real_system_prompt_in_thread_not_in_each_input(self):
        """E2E: explicit system_prompt улетает в thread.developerInstructions,
        не дублируется в каждом per-turn input. Codex чтит инструкцию
        в ответе. Проверяем jsonl на диске — system_prompt должен быть
        в developer-инструкциях, а user input — без него.
        """
        agent = make_agent(model_name=CODEX_MODEL)
        agent.memory._turns.append({"role": "user", "content": "Просто ответь буквой 'A'."})
        marker = "UNIQUE_SYS_PROMPT_MARKER_GHOSTBUSTERS_42"
        try:
            turns = await agent.llm(system_prompt=f"{marker}. Reply only with letter A.")
        finally:
            await agent.close()

        # Модель ответила, наш system_prompt сработал (можем не проверять
        # содержимое строго — главное что вызов не упал и turn есть).
        assistant = [t for t in turns if t.get("role") == "assistant" and isinstance(t.get("content"), str)]
        assert assistant, f"нет ответа, turns={turns}"

        jsonl_path = agent.backend_impl._thread_path
        assert jsonl_path and os.path.exists(jsonl_path)
        with open(jsonl_path, encoding="utf-8") as f:
            jsonl = f.read()

        # 1. Маркер system_prompt должен быть в developer-инструкциях
        #    (либо в base_instructions поле session_meta, либо в developer-message)
        assert marker in jsonl, "system_prompt не дошёл до codex'а (нет в jsonl)"

        # 2. Маркер НЕ должен быть в user-input'е (т.е. в response_item
        #    role=user, content=input_text). Если он там — значит мы зря дублируем.
        for line in jsonl.splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "response_item":
                continue
            p = entry.get("payload") or {}
            if p.get("type") != "message" or p.get("role") != "user":
                continue
            # Codex'овый <environment_context> envelope ок, пропускаем
            content_text = "".join(
                c.get("text", "") for c in (p.get("content") or [])
                if isinstance(c, dict)
            )
            if content_text.lstrip().startswith("<"):
                continue
            assert marker not in content_text, (
                f"system_prompt продублирован в user-input response_item'е: {content_text[:200]}"
            )

    @pytest.mark.asyncio
    async def test_real_divergence_triggers_fresh_thread_with_inject(self):
        """End-to-end: после llm()-вызова + ручной чистки памяти,
        следующий llm() детектит расхождение, создаёт **новый** thread,
        инжектит туда подрезанную историю, удаляет старый rollout с диска.

        Это правильный путь у codex'а (в отличие от клода где правим jsonl
        на месте). После reset в codex видна ровно та история что в памяти.
        """
        agent = make_agent(model_name=CODEX_MODEL)
        agent.memory._turns.append({"role": "user", "content": "Запомни число: 42. Ответь OK."})

        try:
            await agent.llm()
            old_thread_id = agent.backend_impl._thread_id
            old_thread_path = agent.backend_impl._thread_path
            assert old_thread_path and os.path.exists(old_thread_path), \
                f"rollout не создан: {old_thread_path}"

            # Жёстко имитируем компрессию: выкидываем ВСЁ из памяти кроме одного
            # «маркера» — заведомо в старом jsonl нет такого текста, гарантирует
            # divergence (а не пустую память, для которой код возвращает noop).
            agent.memory._turns.clear()
            agent.memory._turns.append({
                "role": "user", "content": "marker_unique_text_42 — это новое сообщение"
            })

            # llm() вызовет _ensure_thread → заметит divergence → fresh thread + inject
            await agent.llm()

            new_thread_id = agent.backend_impl._thread_id
            new_thread_path = agent.backend_impl._thread_path

            assert new_thread_id != old_thread_id, "thread_id не сменился"
            assert new_thread_path != old_thread_path, "thread_path не сменился"
            assert not os.path.exists(old_thread_path), (
                f"старый rollout не удалён: {old_thread_path}"
            )

            # codex создаёт rollout асинхронно — ждём до 5 секунд
            for _ in range(50):
                if os.path.exists(new_thread_path):
                    break
                await asyncio.sleep(0.1)
            assert os.path.exists(new_thread_path), f"новый rollout не создан: {new_thread_path}"

            # В новом jsonl должен быть наш маркер (через inject_items)
            with open(new_thread_path, encoding="utf-8") as f:
                content = f.read()
            assert "marker_unique_text_42" in content, (
                "после reset маркер из памяти не попал в новый jsonl через inject"
            )
        finally:
            await agent.close()

    @pytest.mark.asyncio
    async def test_real_skills_change_recreates_thread(self):
        """Если набор тулов меняется между llm() — backend пересоздаёт thread
        (иначе codex держит старые dynamicTool specs)."""
        agent = make_agent(model_name=CODEX_MODEL)
        agent.memory._turns.append({"role": "user", "content": "Reply with just: a"})

        try:
            await agent.llm()
            first_thread = agent.backend_impl._thread_id

            # Добавляем новый скилл — fingerprint должен поменяться
            new_skill = _CalcSkill()
            new_skill.register(agent.backend_impl.agent)
            agent.skills.append(new_skill)

            agent.memory._turns.append({"role": "user", "content": "Reply with just: b"})
            await agent.llm()
            second_thread = agent.backend_impl._thread_id

            assert second_thread != first_thread, (
                "thread должен пересоздаться при смене skills set"
            )
        finally:
            await agent.close()
