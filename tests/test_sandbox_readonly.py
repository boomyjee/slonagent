"""Unit-тесты: sandbox-write/edit/multi_edit не должны записывать в ro-маунт.

Внутри podman-контейнера ro-маунт защищён `:ro` флагом — exec на write
падает permission denied. Но host-тулы (sandbox_write/edit/multi_edit)
пишут на хосте напрямую через resolve_path → если не проверять флаг ro,
агент обходит read-only мapping.
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
    ws = tmp_path / "workspace"
    ws.mkdir()
    s = SandboxSkill(workspace_dir=str(ws))
    # _mounts() обычно тянет config через self.agent.agent_dir. В тестах
    # SandboxSkill без agent — даём пустой список по умолчанию, отдельные
    # тесты могут переопределить через _mount_ro_rw.
    s._mounts = lambda: []
    return s


def _mount_ro_rw(sb, tmp_path):
    """Регистрируем два маунта: ro на host:/ro_src → container:/mnt/ro/x,
    rw на host:/rw_src → container:/mnt/rw/y."""
    ro_src = tmp_path / "ro_src"; ro_src.mkdir()
    rw_src = tmp_path / "rw_src"; rw_src.mkdir()
    sb._mounts = lambda: [
        (str(ro_src), "/mnt/ro/x", True),
        (str(rw_src), "/mnt/rw/y", False),
    ]
    return ro_src, rw_src


# ─── resolve_path ─────────────────────────────────────────────────────────

def test_resolve_path_ro_for_read(sb, tmp_path):
    ro_src, _ = _mount_ro_rw(sb, tmp_path)
    assert sb.resolve_path("/mnt/ro/x/file.txt") == os.path.join(str(ro_src), "file.txt")


def test_resolve_path_ro_for_write_blocked(sb, tmp_path):
    _mount_ro_rw(sb, tmp_path)
    assert sb.resolve_path("/mnt/ro/x/file.txt", for_write=True) is None


def test_resolve_path_rw_for_write_allowed(sb, tmp_path):
    _, rw_src = _mount_ro_rw(sb, tmp_path)
    assert sb.resolve_path("/mnt/rw/y/file.txt", for_write=True) == os.path.join(str(rw_src), "file.txt")


def test_resolve_path_workspace_for_write_allowed(sb):
    assert sb.resolve_path("/workspace/foo.txt", for_write=True) == os.path.join(sb.workspace_dir, "foo.txt")


# ─── write ─────────────────────────────────────────────────────────────────

def test_write_to_ro_rejected(sb, tmp_path):
    ro_src, _ = _mount_ro_rw(sb, tmp_path)
    res = sb.write("/mnt/ro/x/leak.txt", "evil")
    assert "error" in res
    assert "Только чтение" in res["error"]
    assert not (ro_src / "leak.txt").exists()


def test_write_to_rw_allowed(sb, tmp_path):
    _, rw_src = _mount_ro_rw(sb, tmp_path)
    res = sb.write("/mnt/rw/y/ok.txt", "hello")
    assert res.get("status") == "ok"
    assert (rw_src / "ok.txt").read_text(encoding="utf-8") == "hello"


def test_write_to_unknown_rejected(sb):
    res = sb.write("/nowhere/bad.txt", "x")
    assert "error" in res
    assert "Доступ запрещён" in res["error"]


# ─── edit ──────────────────────────────────────────────────────────────────

def test_edit_to_ro_rejected(sb, tmp_path):
    ro_src, _ = _mount_ro_rw(sb, tmp_path)
    (ro_src / "f.txt").write_text("old", encoding="utf-8")
    res = sb.edit("/mnt/ro/x/f.txt", "old", "new")
    assert "error" in res
    assert "Только чтение" in res["error"]
    assert (ro_src / "f.txt").read_text(encoding="utf-8") == "old"


def test_edit_to_rw_allowed(sb, tmp_path):
    _, rw_src = _mount_ro_rw(sb, tmp_path)
    (rw_src / "f.txt").write_text("old", encoding="utf-8")
    res = sb.edit("/mnt/rw/y/f.txt", "old", "new")
    assert res.get("status") == "ok"
    assert (rw_src / "f.txt").read_text(encoding="utf-8") == "new"


# ─── multi_edit ────────────────────────────────────────────────────────────

def test_multi_edit_to_ro_rejected(sb, tmp_path):
    ro_src, _ = _mount_ro_rw(sb, tmp_path)
    (ro_src / "f.txt").write_text("a\nb\n", encoding="utf-8")
    res = sb.multi_edit("/mnt/ro/x/f.txt", [{"old_string": "a", "new_string": "A"}])
    assert "error" in res
    assert "Только чтение" in res["error"]
    assert (ro_src / "f.txt").read_text(encoding="utf-8") == "a\nb\n"


def test_multi_edit_to_rw_allowed(sb, tmp_path):
    _, rw_src = _mount_ro_rw(sb, tmp_path)
    (rw_src / "f.txt").write_text("a\nb\n", encoding="utf-8")
    res = sb.multi_edit("/mnt/rw/y/f.txt", [
        {"old_string": "a", "new_string": "A"},
        {"old_string": "b", "new_string": "B"},
    ])
    assert res.get("status") == "ok"
    assert (rw_src / "f.txt").read_text(encoding="utf-8") == "A\nB\n"


# ─── read tools НЕ должны быть сломаны ────────────────────────────────────

def test_read_from_ro_still_works(sb, tmp_path):
    ro_src, _ = _mount_ro_rw(sb, tmp_path)
    (ro_src / "f.txt").write_text("hello", encoding="utf-8")
    res = sb.read("/mnt/ro/x/f.txt")
    assert "error" not in res
    assert "hello" in res.get("content", "")


# ─── /slonagent (хардкоженный ro-маунт container_lib) ─────────────────────

def test_resolve_slonagent_root_read(sb):
    assert sb.resolve_path("/slonagent") == sb._lib_dir()


def test_resolve_slonagent_file_read(sb):
    expected = os.path.join(sb._lib_dir(), "agent.py")
    assert sb.resolve_path("/slonagent/agent.py") == expected


def test_resolve_slonagent_for_write_blocked(sb):
    assert sb.resolve_path("/slonagent/agent.py", for_write=True) is None


def test_read_slonagent_agent_py(sb):
    """sandbox_read должен читать /slonagent/agent.py (real stub из container_lib)."""
    res = sb.read("/slonagent/agent.py")
    assert "error" not in res
    assert "get_agent" in res.get("content", "")


def test_write_to_slonagent_blocked(sb):
    res = sb.write("/slonagent/evil.py", "import os; os.system('rm -rf /')")
    assert "error" in res
    assert "Только чтение" in res["error"]
    assert not os.path.exists(os.path.join(sb._lib_dir(), "evil.py"))
