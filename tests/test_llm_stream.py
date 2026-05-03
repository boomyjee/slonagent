"""
Integration tests for Agent.llm() streaming accumulation.

Verifies the stream -> final turn path works correctly against both:
- Gemini (quirky: sends tool_calls without index, role on every chunk)
- OpenAI (spec-compliant reference)

Covers: text stream, parallel tool_calls, and the full memory round-trip
(llm -> dispatch -> memory -> llm) which is what actually breaks if the
saved turn has any malformed fields.

Run:
    .venv\\Scripts\\python -m pytest tests/test_llm_stream.py -v -m integration
"""
import json
import os
import sys
import tempfile
from typing import Annotated

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent import Agent, Skill, tool
from src.memory.providers.base import BaseProvider
from src.transport.base import BaseTransport

pytestmark = pytest.mark.integration


class PassthroughCompressor(BaseProvider):
    def __init__(self): super().__init__(consolidate_tokens=0)
    async def compress(self, turns): return turns


class WeatherSkill(Skill):
    @tool("Get current weather in a city")
    async def get_weather(self, city: Annotated[str, "City name"]) -> dict:
        return {"temp": 20, "city": city}


GEMINI_URL_DEFAULT = "https://generativelanguage.googleapis.com/v1beta/openai/"
OPENROUTER_URL_DEFAULT = "https://openrouter.ai/api/v1"


def _make_agent(model: str, api_key: str, base_url: str,
                backend: str = "openai", backend_params: dict | None = None) -> Agent:
    return Agent(
        id="test",
        model_name=model,
        api_key=api_key,
        base_url=base_url,
        backend=backend,
        backend_params=backend_params,
        agent_dir=tempfile.mkdtemp(),
        memory_compressor=PassthroughCompressor(),
        skills=[WeatherSkill()],
        transport=BaseTransport(),
    )


def _gemini_params() -> dict:
    key = os.environ.get("GEMINI_KEY")
    if not key:
        pytest.skip("GEMINI_KEY не задан")
    return {
        "model": os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview"),
        "api_key": key,
        "base_url": os.environ.get("GEMINI_URL", GEMINI_URL_DEFAULT),
        "backend": "openai",
        "backend_params": None,
    }


def _kimi_params() -> dict:
    key = os.environ.get("OPENROUTER_KEY") or os.environ.get("KIMI_KEY")
    if not key:
        pytest.skip("OPENROUTER_KEY/KIMI_KEY не задан")
    return {
        "model": os.environ.get("KIMI_MODEL", "moonshotai/kimi-k2.6"),
        "api_key": key,
        "base_url": os.environ.get("KIMI_URL", OPENROUTER_URL_DEFAULT),
        "backend": "openai",
        "backend_params": None,
    }


PROVIDERS = [
    pytest.param(_gemini_params, id="gemini"),
    pytest.param(_kimi_params, id="kimi"),
]


@pytest.mark.parametrize("get_params", PROVIDERS)
async def test_text_stream(get_params):
    """Text stream produces a clean turn with role='assistant' (not concatenated)."""
    agent = _make_agent(**get_params())
    await agent.start(run_loop=False)
    await agent.memory.add_turn({"role": "user", "content": "Say hello in 3 words."})

    turn = await agent.llm()

    assert turn["role"] == "assistant", f"role was mangled: {turn['role']!r}"
    assert turn.get("content"), "text stream returned no content"
    assert "tool_calls" not in turn


@pytest.mark.parametrize("get_params", PROVIDERS)
async def test_no_empty_send_message_during_stream(get_params):
    """transport.send_message не должен дёргаться с пустым/whitespace-only текстом.

    Регрессия: kimi через openrouter постоянно слал пустые сообщения в стриминг —
    бэкенд должен фильтровать чанки без полезного контента.
    """
    transport = _RecordingTransport()
    p = get_params()
    agent = Agent(
        id="test", model_name=p["model"], api_key=p["api_key"], base_url=p["base_url"],
        backend=p["backend"], backend_params=p["backend_params"],
        agent_dir=tempfile.mkdtemp(),
        memory_compressor=PassthroughCompressor(),
        transport=transport,
    )
    await agent.start(run_loop=False)
    await agent.memory.add_turn({"role": "user", "content": "Say hello in 3 words."})

    await agent.llm()

    msg_calls = [c for c in transport.calls if c[0] == "send_message"]
    empties = [c for c in msg_calls if not (c[1] or "").strip()]
    assert not empties, f"send_message вызывался с пустым/whitespace текстом: {empties!r}"


@pytest.mark.parametrize("get_params", PROVIDERS)
async def test_parallel_tool_calls_stream(get_params):
    """Parallel tool_calls are accumulated correctly without leaking stream artifacts."""
    agent = _make_agent(**get_params())
    await agent.start(run_loop=False)
    await agent.memory.add_turn({
        "role": "user",
        "content": "What's the weather in Tokyo and Paris? Call get_weather for each city.",
    })

    turn = await agent.llm()

    assert turn["role"] == "assistant", f"role was mangled: {turn['role']!r}"
    tool_calls = turn.get("tool_calls") or []
    assert len(tool_calls) >= 1, f"expected tool_calls, got: {turn}"

    for tc in tool_calls:
        # `index` is a streaming-only field — it must NOT leak into the final turn
        # (non-streaming responses never contain it either).
        assert "index" not in tc, f"stream artifact 'index' leaked into tool_call: {tc}"
        assert tc.get("id"), f"tool_call missing id: {tc}"
        assert tc.get("type") == "function"
        assert tc["function"].get("name")
        assert tc["function"].get("arguments") is not None


class _RecordingTransport(BaseTransport):
    """Captures every send_message/send_thinking call for assertions."""
    def __init__(self):
        super().__init__()
        self.calls = []

    async def send_message(self, text, stream_id=None, final=True):
        self.calls.append(("send_message", text, final))

    async def send_thinking(self, text, stream_id=None, final=False):
        self.calls.append(("send_thinking", text, final))

    async def send_processing(self, active): pass
    async def send_system_prompt(self, text): pass
    async def on_tool_call(self, name, args): pass
    async def on_tool_result(self, name, result): pass


def _make_replay_stream(raw_chunks: list[dict]):
    """Turn a list of serialized delta dicts into an async iterator of ChatCompletionChunk."""
    from openai.types.chat import ChatCompletionChunk

    chunks = []
    for i, raw in enumerate(raw_chunks):
        extra = raw.get("model_extra") or {}
        delta_payload = {
            "content": raw.get("content"),
            "role": raw.get("role"),
            "tool_calls": raw.get("tool_calls") or None,
            **extra,
        }
        chunks.append(ChatCompletionChunk.model_validate({
            "id": f"chunk-{i}",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "gemini-test",
            "choices": [{"index": 0, "delta": delta_payload, "finish_reason": None}],
        }))
    # Last chunk: set finish_reason=stop on an empty delta.
    chunks[-1] = ChatCompletionChunk.model_validate({
        "id": "final",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "gemini-test",
        "choices": [{"index": 0, "delta": chunks[-1].choices[0].delta.model_dump(exclude_none=True),
                     "finish_reason": "stop"}],
    })

    class AsyncIter:
        def __init__(self, items): self._items = iter(items)
        def __aiter__(self): return self
        async def __anext__(self):
            try: return next(self._items)
            except StopIteration: raise StopAsyncIteration
    return AsyncIter(chunks)


async def _replay(raw_chunks: list[dict]) -> tuple[dict, _RecordingTransport]:
    """Inject recorded chunks into agent.llm() via a fake client, return (turn, transport)."""
    transport = _RecordingTransport()
    agent = Agent(
        id="replay",
        model_name="gemini-test",
        api_key="dummy",
        base_url="http://dummy",
        agent_dir=tempfile.mkdtemp(),
        memory_compressor=PassthroughCompressor(),
        transport=transport,
    )
    await agent.start(run_loop=False)

    async def fake_create(**kw):
        return _make_replay_stream(raw_chunks)

    agent.backend_impl.client.chat.completions.create = fake_create
    await agent.memory.add_turn({"role": "user", "content": "replay"})
    turn = await agent.llm()
    return turn, transport


@pytest.mark.asyncio
async def test_pro_preview_no_flag_no_close_tag():
    """gemini-3.1-pro-preview regression: no google.thought flag, no </thought>.

    In this case the model emits `<thought>...` in the first chunk and then streams
    both reasoning and response text without ever closing the tag or setting the
    google.thought flag on any chunk. The agent must NOT leak this content via
    send_message — it should all go to send_thinking, with empty final content.
    """
    chunks = [
        {"content": "<thought>Thinking Process:\n\n1. Analyze..."},
        {"content": " more reasoning here"},
        {"content": " and even more reasoning without a close tag"},
    ]
    turn, transport = await _replay(chunks)

    leaked = [c for c in transport.calls if c[0] == "send_message" and "<thought>" in c[1]]
    assert not leaked, f"thoughts with <thought> tag leaked into send_message: {leaked!r}"

    thoughts_in_message = [c for c in transport.calls if c[0] == "send_message" and "Thinking Process" in c[1]]
    assert not thoughts_in_message, f"thought content leaked into send_message: {thoughts_in_message!r}"

    thinkings = [c for c in transport.calls if c[0] == "send_thinking"]
    assert thinkings, "expected thoughts to be sent via send_thinking"
    final_thinking = next((c for c in thinkings if c[2] is True), None)
    assert final_thinking, "expected final=True on send_thinking"

    assert not turn.get("content"), f"expected no content when only thoughts, got: {turn.get('content')!r}"


@pytest.mark.asyncio
async def test_flag_based_thought_with_clean_transition():
    """Normal case: chunks have google.thought flag + <thought>...</thought> XML."""
    chunks = [
        {"content": "<thought>reasoning part 1", "model_extra": {"extra_content": {"google": {"thought": True}}}},
        {"content": " reasoning part 2", "model_extra": {"extra_content": {"google": {"thought": True}}}},
        {"content": "</thought>The final answer is 42."},
    ]
    turn, transport = await _replay(chunks)

    messages = [c for c in transport.calls if c[0] == "send_message"]
    assert messages, "expected send_message calls for response text"
    for _, text, _ in messages:
        assert "<thought>" not in text, f"XML tag leaked into send_message: {text!r}"
        assert "</thought>" not in text, f"XML tag leaked into send_message: {text!r}"

    assert turn.get("content") == "The final answer is 42."


@pytest.mark.asyncio
async def test_literal_thought_tag_in_response_preserved():
    """If the model's response mentions <thought> literally (e.g. quoting git history),
    it must NOT be treated as a thinking marker — once response text has started,
    <thought> in subsequent content is literal."""
    chunks = [
        {"content": "<thought>analysis", "model_extra": {"extra_content": {"google": {"thought": True}}}},
        {"content": "</thought>Был коммит, который убирал "},
        {"content": "<thought> из вывода — он ломал парсинг."},
    ]
    turn, transport = await _replay(chunks)

    assert turn.get("content") == "Был коммит, который убирал <thought> из вывода — он ломал парсинг."
    full_text = "".join(c[1] for c in transport.calls if c[0] == "send_message")
    assert "<thought>" in full_text, "literal <thought> must be preserved in message text"


async def test_gemini_thought_not_leaked_to_content():
    """Gemini thinking must not leak into saved turn content.

    Before the google.thought-flag based fix, the openai-compat <thought>...</thought>
    tags could end up in turn['content'] (e.g. if the closing tag was missing), and
    regex-based cleanup also stripped *literal* <thought> if the user asked for it.
    """
    agent = _make_agent(**_gemini_params())
    await agent.start(run_loop=False)
    await agent.memory.add_turn({
        "role": "user",
        "content": "Скажи коротко одним словом: столица Франции?",
    })

    turn = await agent.llm()
    content = turn.get("content") or ""
    assert "<thought>" not in content, f"structural <thought> leaked into content: {content!r}"
    assert "</thought>" not in content, f"structural </thought> leaked into content: {content!r}"
    assert content.strip(), "expected non-empty answer, got empty content"


@pytest.mark.parametrize("get_params", PROVIDERS)
async def test_memory_round_trip(get_params):
    """Saved turn is consumable by the API on the next call (llm -> dispatch -> memory -> llm).

    This is the real integrity check: if any field in the accumulated turn is malformed
    (e.g. leaked stream-only fields, mangled role, truncated tool_call structure),
    the next llm() will 400 when the provider validates the assistant message.
    """
    agent = _make_agent(**get_params())
    await agent.start(run_loop=False)
    await agent.memory.add_turn({
        "role": "user",
        "content": "What's the weather in Tokyo and Paris? Call get_weather for each city.",
    })

    turn1 = await agent.llm()
    if not turn1.get("tool_calls"):
        pytest.skip("model did not choose to call tools")

    result_turns = await agent.dispatch_tool_calls(turn1)
    await agent.memory.add_turn(turn1, *result_turns)

    # Second call: the provider must accept its own prior assistant turn back.
    turn2 = await agent.llm()
    assert turn2["role"] == "assistant"
    assert turn2.get("content"), f"expected final text answer, got: {turn2}"
