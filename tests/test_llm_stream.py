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
from src.transport.base import BaseTransport

pytestmark = pytest.mark.integration


class PassthroughCompressor(Skill):
    async def compress(self, turns): return turns


class WeatherSkill(Skill):
    @tool("Get current weather in a city")
    async def get_weather(self, city: Annotated[str, "City name"]) -> dict:
        return {"temp": 20, "city": city}


def _load_config() -> dict:
    with open(os.path.join(ROOT, ".config.json"), encoding="utf-8") as f:
        return json.load(f)


def _make_agent(model: str, api_key: str, base_url: str) -> Agent:
    return Agent(
        id="test",
        model_name=model,
        api_key=api_key,
        base_url=base_url,
        agent_dir=tempfile.mkdtemp(),
        memory_compressor=PassthroughCompressor(),
        skills=[WeatherSkill()],
        transport=BaseTransport(),
    )


def _gemini_params() -> tuple[str, str, str]:
    cfg = _load_config()
    key = cfg.get("keys", {}).get("gemini")
    if not key:
        pytest.skip("gemini key not configured")
    return "gemini-3-flash-preview", key, cfg["keys"]["gemini_url"]


def _openai_params() -> tuple[str, str, str]:
    cfg = _load_config()
    key = cfg.get("keys", {}).get("openrouter")
    if not key:
        pytest.skip("openrouter key not configured")
    return "openai/gpt-4o-mini", key, cfg["keys"]["openrouter_url"]


PROVIDERS = [
    pytest.param(_gemini_params, id="gemini"),
    pytest.param(_openai_params, id="openai"),
]


@pytest.mark.parametrize("get_params", PROVIDERS)
async def test_text_stream(get_params):
    """Text stream produces a clean turn with role='assistant' (not concatenated)."""
    agent = _make_agent(*get_params())
    await agent.start(run_loop=False)
    await agent.memory.add_turn({"role": "user", "content": "Say hello in 3 words."})

    turn = await agent.llm()

    assert turn["role"] == "assistant", f"role was mangled: {turn['role']!r}"
    assert turn.get("content"), "text stream returned no content"
    assert "tool_calls" not in turn


@pytest.mark.parametrize("get_params", PROVIDERS)
async def test_parallel_tool_calls_stream(get_params):
    """Parallel tool_calls are accumulated correctly without leaking stream artifacts."""
    agent = _make_agent(*get_params())
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


@pytest.mark.parametrize("get_params", PROVIDERS)
async def test_memory_round_trip(get_params):
    """Saved turn is consumable by the API on the next call (llm -> dispatch -> memory -> llm).

    This is the real integrity check: if any field in the accumulated turn is malformed
    (e.g. leaked stream-only fields, mangled role, truncated tool_call structure),
    the next llm() will 400 when the provider validates the assistant message.
    """
    agent = _make_agent(*get_params())
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
