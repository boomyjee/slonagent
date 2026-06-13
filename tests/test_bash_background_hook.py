"""Юнит-тесты PreToolUse-хука, запрещающего Bash(run_in_background=true)
(claude.block_bash_background). Чистая логика решения — без claude.exe/SDK,
поэтому отдельным файлом.
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.agent.backends.claude import block_bash_background


def _run(input_data):
    return asyncio.run(block_bash_background(input_data, "tool_use_id", {}))


def test_background_true_denied():
    out = _run({"tool_input": {"command": "sleep 999", "run_in_background": True}})
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert "run_in_background" in hso["permissionDecisionReason"]


def test_background_false_allowed():
    assert _run({"tool_input": {"command": "ls", "run_in_background": False}}) == {}


def test_background_absent_allowed():
    assert _run({"tool_input": {"command": "ls"}}) == {}


def test_empty_tool_input_allowed():
    assert _run({"tool_input": {}}) == {}


def test_missing_tool_input_allowed():
    assert _run({"tool_name": "Bash"}) == {}


def test_none_input_allowed():
    assert _run(None) == {}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("Готово.")
