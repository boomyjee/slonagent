"""Unit-тесты sandbox-write/edit/multi_edit на line-endings.
Проверка: что пришло в content — то и на диске, без CRLF-добавок Python'а.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from src.skills.sandbox import SandboxSkill


@pytest.fixture
def sb(tmp_path):
    s = SandboxSkill(workspace_dir=str(tmp_path))
    return s


def test_write_lf_stays_lf(sb, tmp_path):
    content = "<?php\nnamespace X;\n\nclass Y {}\n"
    sb.write("/workspace/a.php", content)
    raw = (tmp_path / "a.php").read_bytes()
    assert b"\r" not in raw, f"Got CRLF/CR in file: {raw!r}"
    assert raw == content.encode("utf-8")


def test_write_crlf_stays_crlf(sb, tmp_path):
    """Если модель прислала CRLF — пишем CRLF, не множим до \\r\\r\\n."""
    content = "line1\r\nline2\r\n"
    sb.write("/workspace/b.txt", content)
    raw = (tmp_path / "b.txt").read_bytes()
    assert raw == content.encode("utf-8")


def test_edit_lf_stays_lf(sb, tmp_path):
    (tmp_path / "c.php").write_bytes(b"<?php\nfoo;\n")
    sb.edit("/workspace/c.php", "foo;", "bar;\nbaz;")
    raw = (tmp_path / "c.php").read_bytes()
    assert b"\r" not in raw
    assert raw == b"<?php\nbar;\nbaz;\n"


def test_edit_crlf_file_keeps_crlf_in_other_lines(sb, tmp_path):
    """Edit на CRLF-файле не должен трогать \\r\\n в строках, которые не матчили."""
    (tmp_path / "win.txt").write_bytes(b"aaa\r\nbbb\r\nccc\r\n")
    sb.edit("/workspace/win.txt", "bbb", "BBB")
    raw = (tmp_path / "win.txt").read_bytes()
    assert raw == b"aaa\r\nBBB\r\nccc\r\n"


def test_multi_edit_crlf_preserves_unaffected_lines(sb, tmp_path):
    (tmp_path / "win.txt").write_bytes(b"a\r\nb\r\nc\r\n")
    sb.multi_edit("/workspace/win.txt", [
        {"old_string": "a", "new_string": "A"},
    ])
    raw = (tmp_path / "win.txt").read_bytes()
    assert raw == b"A\r\nb\r\nc\r\n"


def test_multi_edit_lf_stays_lf(sb, tmp_path):
    (tmp_path / "d.php").write_bytes(b"a\nb\nc\n")
    sb.multi_edit("/workspace/d.php", [
        {"old_string": "a", "new_string": "A"},
        {"old_string": "b", "new_string": "B"},
    ])
    raw = (tmp_path / "d.php").read_bytes()
    assert b"\r" not in raw
    assert raw == b"A\nB\nc\n"
