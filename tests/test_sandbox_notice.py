"""Юнит-тесты доставки уведомления о пересоздании контейнера
(SandboxSkill._deliver_recreate_notice). Чистая логика — без podman/agent,
поэтому отдельным файлом, а не в podman-гейтнутом test_sandbox_integration.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.skills.sandbox import SandboxSkill


def _skill() -> SandboxSkill:
    # __init__ не трогает podman/agent — нужны только _recreated_notice/_ensure_lock.
    return SandboxSkill()


def test_dict_result_gets_notice_key():
    sb = _skill()
    sb._recreated_notice = "RECREATED"
    out = sb._deliver_recreate_notice({"shell_id": "sh_1", "pid": 7})
    assert out == {"shell_id": "sh_1", "pid": 7, "container_recreated": "RECREATED"}
    assert sb._recreated_notice is None  # флаг погашен после доставки


def test_non_dict_result_is_wrapped():
    sb = _skill()
    sb._recreated_notice = "RECREATED"
    out = sb._deliver_recreate_notice("просто текст")
    assert out == {"result": "просто текст", "container_recreated": "RECREATED"}
    assert sb._recreated_notice is None


def test_list_result_is_wrapped():
    sb = _skill()
    sb._recreated_notice = "RECREATED"
    out = sb._deliver_recreate_notice([1, 2, 3])
    assert out == {"result": [1, 2, 3], "container_recreated": "RECREATED"}


def test_no_notice_returns_result_unchanged():
    sb = _skill()
    assert sb._recreated_notice is None
    assert sb._deliver_recreate_notice({"x": 1}) == {"x": 1}
    assert sb._deliver_recreate_notice("text") == "text"
    assert sb._deliver_recreate_notice([1]) == [1]


def test_notice_delivered_only_once():
    sb = _skill()
    sb._recreated_notice = "RECREATED"
    first = sb._deliver_recreate_notice({"a": 1})
    assert first.get("container_recreated") == "RECREATED"
    second = sb._deliver_recreate_notice({"b": 2})
    assert "container_recreated" not in second  # уже доставлено и погашено
