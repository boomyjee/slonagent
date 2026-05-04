"""End-to-end tests против живого podman'а: SandboxSkill, container lifecycle,
ro/rw bind-mounts, config changes, resolve_path.

Запуск:
    .venv\\Scripts\\python -m pytest tests/test_sandbox_integration.py -v -m integration
"""
import asyncio
import os
import subprocess
import sys

import pytest
import pytest_asyncio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent import Agent, Skill
from src.memory.providers.base import BaseProvider
from src.skills.config import ConfigSkill
from src.skills.sandbox import SandboxSkill


pytestmark = pytest.mark.integration


def _podman_available() -> bool:
    try:
        r = subprocess.run(["podman", "version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


if not _podman_available():
    pytest.skip("podman not available", allow_module_level=True)


class PassthroughCompressor(BaseProvider):
    def __init__(self): super().__init__(consolidate_tokens=0)
    async def compress(self, turns): return turns


async def _make_agent(tmp_path):
    sb = SandboxSkill()
    cs = ConfigSkill()
    agent = Agent(
        id="sb_test",
        model_name="dummy",
        api_key="",
        base_url="",
        agent_dir=str(tmp_path),
        memory_compressor=PassthroughCompressor(),
        skills=[sb, cs],
    )
    await agent.start(run_loop=False)
    return agent, sb, cs


def _cleanup(sb: SandboxSkill):
    # Если _ensure_container ни разу не успешно отработал — контейнера и образа
    # нет, podman-вызовы только зря тратят 0.5-1с каждый. Проверяем по hash'у.
    if getattr(sb, "_mounts_hash", None) is None:
        return
    try:
        sb.stop()
    except Exception as e:
        print(f"[cleanup] stop failed: {e}", file=sys.stderr)
    try:
        env_image = f"{sb.container_name}_env"
        subprocess.run(["podman", "image", "rm", "-f", env_image],
                       capture_output=True, timeout=10)
    except Exception:
        pass


@pytest.fixture
async def sandbox(tmp_path):
    """Function-scoped: каждый тест получает свежий Agent+container.
    Используй для тестов которые меняют config / container lifecycle."""
    agent, sb, cs = await _make_agent(tmp_path)
    try:
        yield agent, sb, cs
    finally:
        _cleanup(sb)


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def shared_sandbox(tmp_path_factory):
    """Session-scoped: один контейнер на весь прогон, для тестов которые
    запускают exec но не меняют config (TestWorkspace/TestBackgroundShell).
    Контейнер и образ удаляются один раз в самом конце сессии."""
    tmp_path = tmp_path_factory.mktemp("shared_sandbox")
    agent, sb, cs = await _make_agent(str(tmp_path))
    try:
        yield agent, sb, cs
    finally:
        _cleanup(sb)


# ─── Container lifecycle ──────────────────────────────────────────────────────

class TestContainerLifecycle:

    async def test_first_start_creates_container(self, sandbox):
        _, sb, _ = sandbox
        await sb._ensure_container()
        r = subprocess.run(["podman", "inspect", sb.container_name, "--format", "{{.State.Running}}"],
                           capture_output=True, text=True)
        assert r.returncode == 0, "container not created"
        assert r.stdout.strip() == "true"

    async def test_state_preserved_across_recreate(self, sandbox, tmp_path):
        """Установленный пакет переживает commit+recreate при изменении конфига."""
        _, sb, cs = sandbox
        r = await sb.exec("apt-get update && apt-get install -y --no-install-recommends jq", timeout=180)
        assert r["exit_code"] == 0, r
        check_before = await sb.exec("which jq")
        assert check_before["exit_code"] == 0

        extra = tmp_path / "trigger_recreate"
        extra.mkdir()
        cs._save({"sandbox": {"rw": [str(extra)]}})

        check_after = await sb.exec("which jq")
        assert check_after["exit_code"] == 0, "package lost after recreate"

    async def test_stopped_container_starts(self, sandbox):
        _, sb, _ = sandbox
        await sb._ensure_container()
        subprocess.run(["podman", "stop", sb.container_name], capture_output=True)
        # Сбрасываем кеш чтобы _ensure_container прошёл inspect и стартанул.
        sb._mounts_hash = None
        await sb._ensure_container()
        r = subprocess.run(["podman", "inspect", sb.container_name, "--format", "{{.State.Running}}"],
                           capture_output=True, text=True)
        assert r.stdout.strip() == "true"


# ─── Workspace ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio(loop_scope="session")
class TestWorkspace:
    """Тесты не меняют config — переиспользуют один контейнер на сессию."""

    async def test_exec_basic_echo(self, shared_sandbox):
        _, sb, _ = shared_sandbox
        r = await sb.exec("echo hello-from-sandbox")
        assert r["exit_code"] == 0
        assert "hello-from-sandbox" in r["stdout"]

    async def test_workspace_persists_between_calls(self, shared_sandbox):
        _, sb, _ = shared_sandbox
        await sb.exec("echo persist > /workspace/persist_marker.txt")
        r = await sb.exec("cat /workspace/persist_marker.txt")
        assert "persist" in r["stdout"]

    async def test_workspace_write_visible_on_host(self, shared_sandbox):
        _, sb, _ = shared_sandbox
        await sb.exec("echo from-container > /workspace/visible_host.txt")
        host_path = os.path.join(sb.workspace_dir, "visible_host.txt")
        assert os.path.exists(host_path)
        assert "from-container" in open(host_path, encoding="utf-8").read()

    async def test_host_write_visible_in_container(self, shared_sandbox):
        _, sb, _ = shared_sandbox
        await sb._ensure_container()
        host_path = os.path.join(sb.workspace_dir, "visible_container.txt")
        with open(host_path, "w", encoding="utf-8") as f:
            f.write("from host\n")
        r = await sb.exec("cat /workspace/visible_container.txt")
        assert "from host" in r["stdout"]


# ─── RW mount ────────────────────────────────────────────────────────────────

class TestRwBind:

    async def test_added_path_visible_in_container(self, sandbox, tmp_path):
        _, sb, cs = sandbox
        src = tmp_path / "rw_src"
        src.mkdir()
        (src / "marker.txt").write_text("hello", encoding="utf-8")
        cs._save({"sandbox": {"rw": [str(src)]}})

        sub = _container_subpath(str(src))
        r = await sb.exec(f"cat /mnt/rw/{sub}/marker.txt")
        assert r["exit_code"] == 0, r
        assert "hello" in r["stdout"]

    async def test_container_write_reaches_host(self, sandbox, tmp_path):
        _, sb, cs = sandbox
        src = tmp_path / "rw_writable"
        src.mkdir()
        cs._save({"sandbox": {"rw": [str(src)]}})

        sub = _container_subpath(str(src))
        r = await sb.exec(f"echo container-wrote > /mnt/rw/{sub}/from_container.txt")
        assert r["exit_code"] == 0, r
        host_file = src / "from_container.txt"
        assert host_file.exists(), "файл не дошёл до хоста"
        assert host_file.read_text(encoding="utf-8").strip() == "container-wrote"

    async def test_host_write_reaches_container(self, sandbox, tmp_path):
        _, sb, cs = sandbox
        src = tmp_path / "rw_live"
        src.mkdir()
        cs._save({"sandbox": {"rw": [str(src)]}})
        await sb._ensure_container()

        (src / "live.txt").write_text("from host", encoding="utf-8")

        sub = _container_subpath(str(src))
        r = await sb.exec(f"cat /mnt/rw/{sub}/live.txt")
        assert "from host" in r["stdout"]

    async def test_multiple_paths(self, sandbox, tmp_path):
        _, sb, cs = sandbox
        a = tmp_path / "a"; a.mkdir(); (a / "f.txt").write_text("A", encoding="utf-8")
        b = tmp_path / "b"; b.mkdir(); (b / "f.txt").write_text("B", encoding="utf-8")
        cs._save({"sandbox": {"rw": [str(a), str(b)]}})

        ra = await sb.exec(f"cat /mnt/rw/{_container_subpath(str(a))}/f.txt")
        rb = await sb.exec(f"cat /mnt/rw/{_container_subpath(str(b))}/f.txt")
        assert "A" in ra["stdout"]
        assert "B" in rb["stdout"]


# ─── RO mount ────────────────────────────────────────────────────────────────

class TestRoBind:

    async def test_read_works(self, sandbox, tmp_path):
        _, sb, cs = sandbox
        src = tmp_path / "ro_src"
        src.mkdir()
        (src / "data.txt").write_text("readable", encoding="utf-8")
        cs._save({"sandbox": {"ro": [str(src)]}})

        sub = _container_subpath(str(src))
        r = await sb.exec(f"cat /mnt/ro/{sub}/data.txt")
        assert "readable" in r["stdout"]

    async def test_write_blocked(self, sandbox, tmp_path):
        _, sb, cs = sandbox
        src = tmp_path / "ro_immutable"
        src.mkdir()
        cs._save({"sandbox": {"ro": [str(src)]}})

        sub = _container_subpath(str(src))
        r = await sb.exec(f"echo nope > /mnt/ro/{sub}/out.txt 2>&1; echo EC=$?")
        assert "EC=0" not in r["stdout"], "запись в /mnt/ro прошла"
        assert not (src / "out.txt").exists()


# ─── Config changes ──────────────────────────────────────────────────────────

class TestConfigChanges:

    async def test_remove_path_unbinds(self, sandbox, tmp_path):
        """После выпиливания пути из конфига его контент в контейнере не виден.

        Замечание: сама директория /mnt/rw/<sub>/ может остаться пустой в образе
        (podman при mkdir -p создаёт путь, commit его сохраняет), поэтому
        проверяем по конкретному файлу — он должен исчезнуть."""
        _, sb, cs = sandbox
        src = tmp_path / "rw_temp"
        src.mkdir()
        (src / "secret.txt").write_text("visible", encoding="utf-8")
        cs._save({"sandbox": {"rw": [str(src)]}})
        sub = _container_subpath(str(src))

        r1 = await sb.exec(f"cat /mnt/rw/{sub}/secret.txt")
        assert "visible" in r1["stdout"]

        cs._save({"sandbox": {"rw": []}})

        r2 = await sb.exec(f"cat /mnt/rw/{sub}/secret.txt 2>&1; echo EC=$?")
        assert "visible" not in r2["stdout"], f"файл всё ещё доступен: {r2}"
        assert "EC=0" not in r2["stdout"]

    async def test_toggle_ro_to_rw(self, sandbox, tmp_path):
        _, sb, cs = sandbox
        src = tmp_path / "toggle"
        src.mkdir()
        sub = _container_subpath(str(src))

        cs._save({"sandbox": {"ro": [str(src)]}})
        r_ro = await sb.exec(f"echo a > /mnt/ro/{sub}/x.txt 2>&1; echo EC=$?")
        assert "EC=0" not in r_ro["stdout"]

        cs._save({"sandbox": {"rw": [str(src)]}})
        r_rw = await sb.exec(f"echo b > /mnt/rw/{sub}/x.txt 2>&1; echo EC=$?")
        assert "EC=0" in r_rw["stdout"]
        assert (src / "x.txt").read_text(encoding="utf-8").strip() == "b"

    async def test_cache_invalidates_on_config_change(self, sandbox, tmp_path):
        """После первого _ensure_container хеш закеширован; смена конфига должна
        его инвалидировать и привести к пересозданию контейнера."""
        _, sb, cs = sandbox
        await sb._ensure_container()
        first_hash = sb._mounts_hash
        first_id = subprocess.run(
            ["podman", "inspect", "--format", "{{.Id}}", sb.container_name],
            capture_output=True, text=True,
        ).stdout.strip()

        src = tmp_path / "new_path"
        src.mkdir()
        cs._save({"sandbox": {"rw": [str(src)]}})
        await sb._ensure_container()

        assert sb._mounts_hash != first_hash, "hash не обновился"
        new_id = subprocess.run(
            ["podman", "inspect", "--format", "{{.Id}}", sb.container_name],
            capture_output=True, text=True,
        ).stdout.strip()
        assert new_id != first_id, "контейнер не был пересоздан"


# ─── resolve_path (pure) ─────────────────────────────────────────────────────

class TestResolvePath:
    """Pure-functional тесты — но кейсы с mount-конфигом меняют ConfigSkill,
    поэтому используем function-scoped sandbox чтоб тесты не пересекались."""

    async def test_workspace_root(self, sandbox):
        _, sb, _ = sandbox
        assert sb.resolve_path("/workspace") == sb.workspace_dir

    async def test_workspace_subpath(self, sandbox):
        _, sb, _ = sandbox
        # /workspace ветка не нормализует слэши — это историческое поведение,
        # не баг (Windows API ест прямые слеши тоже).
        assert sb.resolve_path("/workspace/foo/bar.txt") == os.path.join(sb.workspace_dir, "foo/bar.txt")

    async def test_ro_path(self, sandbox, tmp_path):
        _, sb, cs = sandbox
        src = tmp_path / "ro_resolve"
        src.mkdir()
        cs._save({"sandbox": {"ro": [str(src)]}})
        sub = _container_subpath(str(src))
        assert sb.resolve_path(f"/mnt/ro/{sub}/file.txt") == os.path.join(str(src), "file.txt")

    async def test_rw_exact_match(self, sandbox, tmp_path):
        _, sb, cs = sandbox
        src = tmp_path / "rw_resolve"
        src.mkdir()
        cs._save({"sandbox": {"rw": [str(src)]}})
        sub = _container_subpath(str(src))
        assert sb.resolve_path(f"/mnt/rw/{sub}") == str(src)

    async def test_unmounted_returns_none(self, sandbox):
        _, sb, _ = sandbox
        assert sb.resolve_path("/mnt/ro/random/whatever") is None
        assert sb.resolve_path("/etc/passwd") is None


def _container_subpath(host: str) -> str:
    p = host.replace("\\", "/").rstrip("/").lower()
    if len(p) >= 2 and p[1] == ":":
        return p[0] + "/" + p[2:].lstrip("/")
    return p.lstrip("/")


# ─── Read tool ────────────────────────────────────────────────────────────────

class TestRead:
    """read должен возвращать контент с padded line numbers (Claude Code addLineNumbers формат)."""

    async def test_padded_line_numbers(self, sandbox):
        _, sb, _ = sandbox
        with open(os.path.join(sb.workspace_dir, "read_basic.py"), "w", encoding="utf-8") as f:
            f.write("def foo():\n    return 1\n")
        r = sb.read("/workspace/read_basic.py")
        assert "content" in r, r
        # Right-aligned in 6-char field, then arrow.
        assert r["content"].startswith("     1→def foo():\n     2→    return 1\n"), r["content"]
        assert r["total_lines"] == 2
        assert r["returned_lines"] == 2

    async def test_offset_and_limit(self, sandbox):
        _, sb, _ = sandbox
        path = os.path.join(sb.workspace_dir, "read_range.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
        r = sb.read("/workspace/read_range.txt", offset=3, limit=2)
        assert r["returned_lines"] == 2
        assert r["offset"] == 3
        assert "     3→line3" in r["content"]
        assert "     4→line4" in r["content"]
        assert "line5" not in r["content"]

    async def test_image_returns_parts(self, sandbox):
        _, sb, _ = sandbox
        # Smallest valid PNG (1x1 transparent).
        png_bytes = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
        )
        path = os.path.join(sb.workspace_dir, "tiny.png")
        with open(path, "wb") as f:
            f.write(png_bytes)
        r = sb.read("/workspace/tiny.png")
        assert "_parts" in r
        assert r["_parts"][0]["type"] == "image_url"
        assert r["_parts"][0]["image_url"]["url"].startswith("data:image/png;base64,")


# ─── Grep tool ────────────────────────────────────────────────────────────────

class TestGrep:

    @staticmethod
    def _setup(sb):
        for sub in ("", "sub", ".git"):
            os.makedirs(os.path.join(sb.workspace_dir, sub), exist_ok=True)
        with open(os.path.join(sb.workspace_dir, "a.py"), "w", encoding="utf-8") as f:
            f.write("def foo():\n    return 1\n")
        with open(os.path.join(sb.workspace_dir, "b.js"), "w", encoding="utf-8") as f:
            f.write("function FOO() { return 2; }\n")
        with open(os.path.join(sb.workspace_dir, "sub", "c.py"), "w", encoding="utf-8") as f:
            f.write("class Bar:\n    pass\n")
        with open(os.path.join(sb.workspace_dir, ".git", "noise.py"), "w", encoding="utf-8") as f:
            f.write("def hidden():\n    return 0\n")

    async def test_default_mode_files_with_matches(self, sandbox):
        _, sb, _ = sandbox
        self._setup(sb)
        r = sb.grep(pattern=r"\bfoo\b", path="/workspace")
        assert r["mode"] == "files_with_matches"
        # default case-sensitive: "FOO" в b.js не матчится \bfoo\b
        assert "a.py" in r["filenames"]
        assert "b.js" not in r["filenames"]

    async def test_vcs_dirs_excluded(self, sandbox):
        _, sb, _ = sandbox
        self._setup(sb)
        r = sb.grep(pattern="def", path="/workspace")
        assert all(".git" not in f for f in r["filenames"]), r["filenames"]

    async def test_type_filter(self, sandbox):
        _, sb, _ = sandbox
        self._setup(sb)
        r_py = sb.grep(pattern="return", path="/workspace", type="py")
        assert "a.py" in r_py["filenames"]
        assert "b.js" not in r_py["filenames"]
        r_js = sb.grep(pattern="return", path="/workspace", type="js")
        assert "a.py" not in r_js["filenames"]
        assert "b.js" in r_js["filenames"]

    async def test_glob_filter(self, sandbox):
        _, sb, _ = sandbox
        self._setup(sb)
        r = sb.grep(pattern="return", path="/workspace", glob="*.py")
        assert "a.py" in r["filenames"]
        assert "b.js" not in r["filenames"]

    async def test_case_insensitive(self, sandbox):
        _, sb, _ = sandbox
        self._setup(sb)
        r = sb.grep(pattern="foo", path="/workspace", case_insensitive=True)
        # b.js имеет FOO uppercase
        assert "b.js" in r["filenames"]
        assert "a.py" in r["filenames"]

    async def test_content_with_context(self, sandbox):
        _, sb, _ = sandbox
        self._setup(sb)
        r = sb.grep(pattern="return", path="/workspace", output_mode="content", context=1)
        assert r["mode"] == "content"
        # Контекст: для return в a.py должен также вытянуть def foo()
        assert "a.py:1:def foo():" in r["content"]
        assert "a.py:2:    return 1" in r["content"]

    async def test_content_no_line_numbers(self, sandbox):
        _, sb, _ = sandbox
        self._setup(sb)
        r = sb.grep(pattern="return", path="/workspace", output_mode="content", line_numbers=False)
        # Без номеров: формат a.py:contents
        assert any(line.startswith("a.py:    return") for line in r["content"].split("\n")), r["content"]

    async def test_count_mode(self, sandbox):
        _, sb, _ = sandbox
        self._setup(sb)
        r = sb.grep(pattern="def", path="/workspace", output_mode="count")
        assert r["mode"] == "count"
        assert r["numFiles"] >= 1
        assert r["numMatches"] >= 1
        assert "a.py:1" in r["content"]

    async def test_pagination(self, sandbox):
        _, sb, _ = sandbox
        for i in range(5):
            with open(os.path.join(sb.workspace_dir, f"f{i}.py"), "w", encoding="utf-8") as f:
                f.write("hit\n")
        r1 = sb.grep(pattern="hit", path="/workspace", head_limit=2, offset=0)
        assert r1["numFiles"] == 2
        assert r1.get("appliedLimit") == 2
        r2 = sb.grep(pattern="hit", path="/workspace", head_limit=2, offset=2)
        assert r2["numFiles"] == 2
        assert r2.get("appliedOffset") == 2
        # Не пересекаются
        assert set(r1["filenames"]).isdisjoint(set(r2["filenames"]))

    async def test_multiline(self, sandbox):
        _, sb, _ = sandbox
        with open(os.path.join(sb.workspace_dir, "ml.py"), "w", encoding="utf-8") as f:
            f.write("class Foo:\n    pass\n\nclass Bar:\n    pass\n")
        # Pattern that spans newline: class \w+:\n.*pass
        r = sb.grep(pattern=r"class \w+:\n\s+pass", path="/workspace", multiline=True, output_mode="content")
        assert r["mode"] == "content"
        assert "ml.py" in r["content"]


# ─── Glob tool ────────────────────────────────────────────────────────────────

class TestGlob:

    async def test_recursive_double_star(self, sandbox):
        _, sb, _ = sandbox
        os.makedirs(os.path.join(sb.workspace_dir, "deep", "nested"), exist_ok=True)
        for p in ("a.py", "deep/b.py", "deep/nested/c.py"):
            with open(os.path.join(sb.workspace_dir, p), "w") as f: f.write("x")
        r = sb.glob(pattern="**/*.py", path="/workspace")
        names = set(r["filenames"])
        assert "a.py" in names
        assert "deep/b.py" in names
        assert "deep/nested/c.py" in names

    async def test_brace_expansion(self, sandbox):
        _, sb, _ = sandbox
        for p in ("x.ts", "y.tsx", "z.js"):
            with open(os.path.join(sb.workspace_dir, p), "w") as f: f.write("x")
        r = sb.glob(pattern="*.{ts,tsx}", path="/workspace")
        names = set(r["filenames"])
        assert "x.ts" in names
        assert "y.tsx" in names
        assert "z.js" not in names

    async def test_mtime_sort_oldest_first(self, sandbox):
        _, sb, _ = sandbox
        import time
        # Создаём в обратном алфавитном порядке.
        for name in ("c.txt", "b.txt", "a.txt"):
            with open(os.path.join(sb.workspace_dir, name), "w") as f: f.write("x")
            time.sleep(0.05)
        r = sb.glob(pattern="*.txt", path="/workspace")
        # oldest first — c.txt создан первым, a.txt последним.
        assert r["filenames"] == ["c.txt", "b.txt", "a.txt"], r["filenames"]

    async def test_vcs_excluded(self, sandbox):
        _, sb, _ = sandbox
        os.makedirs(os.path.join(sb.workspace_dir, ".git"), exist_ok=True)
        with open(os.path.join(sb.workspace_dir, "real.py"), "w") as f: f.write("x")
        with open(os.path.join(sb.workspace_dir, ".git", "noise.py"), "w") as f: f.write("x")
        r = sb.glob(pattern="**/*.py", path="/workspace")
        assert "real.py" in r["filenames"]
        assert all(".git" not in f for f in r["filenames"])


# ─── MultiEdit tool ───────────────────────────────────────────────────────────

class TestMultiEdit:

    async def test_sequential_edits(self, sandbox):
        _, sb, _ = sandbox
        path = os.path.join(sb.workspace_dir, "me.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write("foo\nbar\nbaz\n")
        r = sb.multi_edit(path="/workspace/me.py", edits=[
            {"old_string": "foo", "new_string": "FOO"},
            {"old_string": "bar", "new_string": "BAR"},
        ])
        assert r["status"] == "ok"
        assert r["edits"] == 2
        assert open(path, encoding="utf-8").read() == "FOO\nBAR\nbaz\n"

    async def test_rollback_on_failure(self, sandbox):
        _, sb, _ = sandbox
        path = os.path.join(sb.workspace_dir, "me_fail.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write("alpha\nbeta\n")
        original = open(path, encoding="utf-8").read()
        r = sb.multi_edit(path="/workspace/me_fail.py", edits=[
            {"old_string": "alpha", "new_string": "AAA"},
            {"old_string": "NOTHERE", "new_string": "X"},
        ])
        assert "error" in r
        # Файл не должен измениться.
        assert open(path, encoding="utf-8").read() == original

    async def test_replace_all_per_edit(self, sandbox):
        _, sb, _ = sandbox
        path = os.path.join(sb.workspace_dir, "me_all.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write("x x x\n")
        r = sb.multi_edit(path="/workspace/me_all.py", edits=[
            {"old_string": "x", "new_string": "Y", "replace_all": True},
        ])
        assert r["status"] == "ok"
        assert open(path, encoding="utf-8").read() == "Y Y Y\n"

    async def test_unique_required_without_replace_all(self, sandbox):
        _, sb, _ = sandbox
        path = os.path.join(sb.workspace_dir, "me_unique.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write("dup\ndup\n")
        r = sb.multi_edit(path="/workspace/me_unique.py", edits=[
            {"old_string": "dup", "new_string": "X"},
        ])
        assert "error" in r
        assert "найден" in r["error"]


# ─── Background shell ─────────────────────────────────────────────────────────

@pytest.mark.asyncio(loop_scope="session")
class TestBackgroundShell:
    """Тесты не меняют config — переиспользуют один контейнер на сессию."""

    async def test_background_runs_and_kill(self, shared_sandbox):
        _, sb, _ = shared_sandbox
        # Долгоиграющий цикл который шлёт строки в stdout каждые 0.2с.
        cmd = "for i in $(seq 1 50); do echo line-$i; sleep 0.2; done"
        r = await sb.exec(command=cmd, background=True)
        assert "shell_id" in r, r
        assert r["pid"] > 0
        sid = r["shell_id"]

        # Дать процессу выдать пару строк.
        await asyncio.sleep(0.6)

        rs = await sb.read_shell(shell_id=sid)
        assert "output" in rs, rs
        assert "line-1" in rs["output"]
        assert rs["alive"] is True

        # Прибиваем.
        rk = await sb.kill_shell(shell_id=sid)
        assert rk["status"] == "killed"

        await asyncio.sleep(0.3)
        rs2 = await sb.read_shell(shell_id=sid)
        assert rs2["alive"] is False

    async def test_completed_process_marked_dead(self, shared_sandbox):
        _, sb, _ = shared_sandbox
        r = await sb.exec(command="echo hello-bg && sleep 0.3", background=True)
        assert "shell_id" in r
        sid = r["shell_id"]
        # Wait for it to exit.
        await asyncio.sleep(1.0)
        rs = await sb.read_shell(shell_id=sid)
        assert "hello-bg" in rs["output"]
        assert rs["alive"] is False

    async def test_invalid_shell_id(self, shared_sandbox):
        _, sb, _ = shared_sandbox
        r = await sb.read_shell(shell_id="not-a-real-id")
        assert "error" in r
        r = await sb.kill_shell(shell_id="invalid")
        assert "error" in r


# ─── RPC passthrough host ↔ container ─────────────────────────────────────────

class TestRpcPassthrough:
    """Реальный podman + reальный RPC канал. dispatch_tool_call дёргается
    напрямую — без LLM/loop, чтобы изолировать именно проброс команд и
    функций между host'ом и кастомным скриптом-скиллом в контейнере."""

    @staticmethod
    def _drop_tool(tmp_path, filename: str, body: str):
        tools_dir = tmp_path / "memory" / "workspace" / "tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        (tools_dir / filename).write_text(body, encoding="utf-8")

    @staticmethod
    def _tool_call(name: str, args: dict, call_id="c1") -> dict:
        import json as _j
        return {"id": call_id, "type": "function",
                "function": {"name": name, "arguments": _j.dumps(args)}}

    async def test_function_returns_value(self, sandbox, tmp_path):
        """Forward path: dispatch_tool_call → exec script in container → result."""
        self._drop_tool(tmp_path, "math_tools.py",
            "from agent import Skill, tool\n"
            "from typing import Annotated\n"
            "class MathSkill(Skill):\n"
            "    @tool('Удваивает число')\n"
            "    async def double(self, x: Annotated[int, 'число']) -> dict:\n"
            "        return {'result': x * 2}\n"
        )
        _, sb, _ = sandbox
        sb.get_tools()  # триггерит _scan_script_tools — без него dispatch не найдёт имя
        result = await sb.dispatch_tool_call(
            self._tool_call("sandbox_math_double", {"x": 21})
        )
        assert result == {"result": 42}

    async def test_function_with_kwargs_and_types(self, sandbox, tmp_path):
        """Маршаллинг типов: int/str/list/dict через JSON-сериализацию RPC."""
        self._drop_tool(tmp_path, "echo_tools.py",
            "from agent import Skill, tool\n"
            "from typing import Annotated\n"
            "class EchoSkill(Skill):\n"
            "    @tool('Эхо аргументов')\n"
            "    async def args(self, n: Annotated[int, 'n'],\n"
            "                   s: Annotated[str, 's'],\n"
            "                   xs: Annotated[list, 'xs']) -> dict:\n"
            "        return {'n': n, 's': s, 'xs': xs, 'kinds': [type(n).__name__, type(s).__name__, type(xs).__name__]}\n"
        )
        _, sb, _ = sandbox
        sb.get_tools()
        result = await sb.dispatch_tool_call(
            self._tool_call("sandbox_echo_args", {"n": 7, "s": "ok", "xs": [1, 2, 3]})
        )
        assert result == {"n": 7, "s": "ok", "xs": [1, 2, 3],
                          "kinds": ["int", "str", "list"]}

    async def test_function_exception_propagates_with_traceback(self, sandbox, tmp_path):
        """Исключение из тулзы проходит через RPC с трейсом и именем файла."""
        self._drop_tool(tmp_path, "boom_tools.py",
            "from agent import Skill, tool\n"
            "from typing import Annotated\n"
            "class BoomSkill(Skill):\n"
            "    @tool('Кидает ошибку')\n"
            "    async def crash(self, msg: Annotated[str, 'msg']) -> dict:\n"
            "        raise RuntimeError(msg)\n"
        )
        _, sb, _ = sandbox
        sb.get_tools()
        result = await sb.dispatch_tool_call(
            self._tool_call("sandbox_boom_crash", {"msg": "bang"})
        )
        # SandboxSkill ловит исключение и возвращает {"error": "..."} с trace+stderr
        assert "error" in result
        assert "RuntimeError" in result["error"]
        assert "bang" in result["error"]

    async def test_callback_to_host_via_agent_proxy(self, tmp_path):
        """Reverse path: тулза в sandbox зовёт self.agent.transport.send_message
        → RPC обратно на хост → метод исполняется на CaptureTransport'е."""
        from src.transport.base import BaseTransport

        class CaptureTransport(BaseTransport):
            def __init__(self):
                super().__init__()
                self.messages: list[str] = []

            async def send_message(self, text, stream_id=None, final=True):
                self.messages.append(text)

        self._drop_tool(tmp_path, "ping_tools.py",
            "from agent import Skill, tool\n"
            "from typing import Annotated\n"
            "class PingSkill(Skill):\n"
            "    @tool('Шлёт сообщение через transport родителя')\n"
            "    async def send(self, msg: Annotated[str, 'msg']) -> dict:\n"
            "        await self.agent.transport.send_message(msg)\n"
            "        return {'sent': True}\n"
        )

        # Кастомный agent: ставим CaptureTransport до start.
        transport = CaptureTransport()
        sb = SandboxSkill()
        cs = ConfigSkill()
        agent = Agent(
            id="sb_callback",
            model_name="dummy",
            api_key="",
            base_url="",
            agent_dir=str(tmp_path),
            memory_compressor=PassthroughCompressor(),
            skills=[sb, cs],
            transport=transport,
        )
        await agent.start(run_loop=False)
        try:
            sb.get_tools()
            result = await sb.dispatch_tool_call(
                self._tool_call("sandbox_ping_send", {"msg": "hello from container"})
            )
            assert result == {"sent": True}
            assert transport.messages == ["hello from container"]
        finally:
            _cleanup(sb)

    async def test_spawn_subagent_runs_echo_loop_via_rpc(self, tmp_path):
        """Substantial: тулза в sandbox вызывает self.agent.spawn_subagent,
        стартует под-агентов loop через прокси, шлёт через process_message,
        echo backend отвечает в parent transport (он же sub transport — наследуется).
        Хост-сторона ловит echo в CaptureTransport, проверяет содержимое."""
        from src.transport.base import BaseTransport

        class CaptureTransport(BaseTransport):
            def __init__(self):
                super().__init__()
                self.messages: list[str] = []
                self.done = asyncio.Event()

            async def send_message(self, text, stream_id=None, final=True):
                if final:
                    self.messages.append(text)
                    self.done.set()

        self._drop_tool(tmp_path, "spawn_tools.py",
            "import asyncio\n"
            "from typing import Annotated\n"
            "from agent import Skill, tool\n"
            "class SpawnSkill(Skill):\n"
            "    @tool('Спавнит sub-agent с echo backend и крутит его loop')\n"
            "    async def run(self, name: Annotated[str, 'имя'],\n"
            "                  msg: Annotated[str, 'msg']) -> dict:\n"
            "        sub = await self.agent.spawn_subagent(\n"
            "            name, backend='echo', skills=[], memory_providers=[]\n"
            "        )\n"
            "        loop_task = asyncio.create_task(sub.loop())\n"
            "        try:\n"
            "            await sub.process_message([{'type':'text','text': msg}])\n"
            "            # Echo отрабатывает <1s. Отдыхаем чтобы host'у дойти.\n"
            "            await asyncio.sleep(1.0)\n"
            "        finally:\n"
            "            loop_task.cancel()\n"
            "            try: await loop_task\n"
            "            except BaseException: pass\n"
            "        return {'spawned': name}\n"
        )

        # Кастомный agent с CaptureTransport — sub-agent наследует transport
        # из parent (default override в spawn_subagent), echo пишет туда.
        transport = CaptureTransport()
        sb = SandboxSkill()
        cs = ConfigSkill()
        agent = Agent(
            id="sb_subagent",
            model_name="dummy",
            api_key="",
            base_url="",
            agent_dir=str(tmp_path),
            memory_compressor=PassthroughCompressor(),
            skills=[sb, cs],
            transport=transport,
        )
        # spawn_subagent зовёт Agent.from_config(self._config, ...) — _config
        # сетится только в from_config, не в __init__. Подкладываем минимум.
        agent._config = {"model_name": "dummy", "api_key": "", "base_url": ""}
        await agent.start(run_loop=False)
        try:
            sb.get_tools()
            result = await sb.dispatch_tool_call(
                self._tool_call("sandbox_spawn_run", {"name": "kid", "msg": "hello"})
            )
            assert result == {"spawned": "kid"}
            assert transport.done.is_set(), "no final send_message captured"
            echoes = [m for m in transport.messages if m.startswith("эхо: ")]
            assert echoes, f"no echo prefix in messages: {transport.messages!r}"
            assert "hello" in echoes[0]
            sub_dir = tmp_path / "memory" / "subagents" / "kid"
            assert sub_dir.is_dir()
        finally:
            _cleanup(sb)
