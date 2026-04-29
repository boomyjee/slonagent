"""End-to-end tests против живого podman'а: SandboxSkill, container lifecycle,
ro/rw bind-mounts, config changes, resolve_path.

Запуск:
    .venv\\Scripts\\python -m pytest tests/test_sandbox_integration.py -v -m integration
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent import Agent, Skill
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


class PassthroughCompressor(Skill):
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
    agent, sb, cs = await _make_agent(tmp_path)
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

class TestWorkspace:

    async def test_exec_basic_echo(self, sandbox):
        _, sb, _ = sandbox
        r = await sb.exec("echo hello-from-sandbox")
        assert r["exit_code"] == 0
        assert "hello-from-sandbox" in r["stdout"]

    async def test_workspace_persists_between_calls(self, sandbox):
        _, sb, _ = sandbox
        await sb.exec("echo persist > /workspace/marker.txt")
        r = await sb.exec("cat /workspace/marker.txt")
        assert "persist" in r["stdout"]

    async def test_workspace_write_visible_on_host(self, sandbox):
        _, sb, _ = sandbox
        await sb.exec("echo from-container > /workspace/host.txt")
        host_path = os.path.join(sb.workspace_dir, "host.txt")
        assert os.path.exists(host_path)
        assert "from-container" in open(host_path, encoding="utf-8").read()

    async def test_host_write_visible_in_container(self, sandbox):
        _, sb, _ = sandbox
        await sb._ensure_container()
        host_path = os.path.join(sb.workspace_dir, "from_host.txt")
        with open(host_path, "w", encoding="utf-8") as f:
            f.write("from host\n")
        r = await sb.exec("cat /workspace/from_host.txt")
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
