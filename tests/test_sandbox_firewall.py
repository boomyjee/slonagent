"""E2E-тест _ensure_windows_firewall_rule: репродуцирует баг, при котором
Defender блокирует входящие к python (sandbox_proxy из контейнера не
коннектится), и проверяет что фикс восстанавливает связь.

Bug на пользовательских машинах возникает по двум причинам:
1) Public профиль с DefaultInboundAction=Block + нет Allow-правила;
2) Кастомное Inbound Block-правило на python.exe (артефакт случайного 'Deny'
   в UAC-промпте Defender'а при первом запуске).

Здесь репродуцируем (2) — это более надёжный способ из-под скрипта, не зависит
от network category и поведения NLA. Фикс обрабатывает оба сценария одинаково:
отключает конкурирующие Block-правила на тот же exe и создаёт Inbound Allow.

Требует admin (firewall rule management) и WSL (TCP-probe из VM).

Запуск из admin PowerShell:
    .venv\\Scripts\\python -m pytest tests/test_sandbox_firewall.py -v
"""
import asyncio
import ctypes
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

if sys.platform != "win32":
    pytest.skip("Windows-only test", allow_module_level=True)

from src.skills.sandbox import SandboxSkill

pytestmark = pytest.mark.integration

RULE_NAME = SandboxSkill._FIREWALL_RULE_NAME
TEST_BLOCK_NAME = "slonagent test block"
WSL_ADAPTER = "vEthernet (WSL)"


def _ps(cmd: str, timeout: int = 30) -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                       capture_output=True, text=True, timeout=timeout)
    return (r.stdout or "").strip()


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _has_wsl() -> bool:
    try:
        r = subprocess.run(["wsl", "-e", "true"], capture_output=True, timeout=15)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _wsl_adapter_host_ip() -> str | None:
    out = _ps(f"(Get-NetIPAddress -InterfaceAlias '{WSL_ADAPTER}' "
              f"-AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress")
    return out or None


def _rule_exists(name: str) -> bool:
    return _ps(f"if (Get-NetFirewallRule -DisplayName '{name}' "
               f"-ErrorAction SilentlyContinue) {{ 'y' }}") == "y"


def _wsl_can_connect(host: str, port: int, timeout: int = 3) -> bool:
    bash_cmd = (
        f"timeout {timeout} bash -c '</dev/tcp/{host}/{port}' "
        f"&& echo ok || echo fail"
    )
    r = subprocess.run(
        ["wsl", "-e", "bash", "-c", bash_cmd],
        capture_output=True, text=True, timeout=timeout + 10,
    )
    return "ok" in (r.stdout or "")


if not _is_admin():
    pytest.skip("admin required", allow_module_level=True)
if not _has_wsl():
    pytest.skip("WSL required", allow_module_level=True)
if not _wsl_adapter_host_ip():
    pytest.skip(f"{WSL_ADAPTER} adapter not found", allow_module_level=True)


@pytest.fixture(scope="module")
def defender_enabled():
    """На свежей Windows все 3 профиля Defender enabled. На dev-машинах часто
    выключены — Block-правила тогда не активируются. Включаем на время теста.
    NB: Set-NetFirewallProfile -Enabled ждёт GpoBoolean ('True'/'False'),
    PowerShell-литералы $true/$false здесь НЕ работают."""
    snapshot = _ps(
        "Get-NetFirewallProfile | ForEach-Object { '{0}={1}' -f $_.Name, $_.Enabled }"
    )
    states = {}
    for line in snapshot.splitlines():
        if "=" in line:
            n, v = line.split("=", 1)
            states[n.strip()] = v.strip().lower() == "true"

    _ps("Set-NetFirewallProfile -Profile Domain, Private, Public -Enabled True")
    time.sleep(1.0)
    try:
        yield
    finally:
        for name, was_enabled in states.items():
            en = "True" if was_enabled else "False"
            _ps(f"Set-NetFirewallProfile -Name {name} -Enabled {en}")


@pytest.fixture
def block_python_inbound(defender_enabled):
    """Имитирует «случайно созданное после UAC-Deny» Block-правило на тот же
    image path, под которым Defender видит наш процесс. Восстанавливает
    enabled-состояние ВСЕХ python.exe Block-правил на teardown — наш фикс
    их отключает, но для других тестовых прогонов нужны как были."""
    real_py = SandboxSkill._windows_process_image_path().replace("'", "''")
    # Snapshot всех enabled Inbound Block-правил на python.exe
    snapshot = _ps(
        "Get-NetFirewallApplicationFilter -All -PolicyStore PersistentStore | "
        f"Where-Object {{ $_.Program -imatch 'python\\.exe' }} | "
        "ForEach-Object { Get-NetFirewallRule -AssociatedNetFirewallApplicationFilter $_ } | "
        "Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' -and $_.Enabled -eq 'True' } | "
        "Select-Object -ExpandProperty Name",
        timeout=60,
    )
    pre_enabled_blocks = [n for n in snapshot.splitlines() if n.strip()]

    _ps(f"Remove-NetFirewallRule -DisplayName '{TEST_BLOCK_NAME}' -ErrorAction SilentlyContinue")
    _ps(
        f"New-NetFirewallRule -DisplayName '{TEST_BLOCK_NAME}' "
        f"-Direction Inbound -Action Block -Program '{real_py}' -Profile Any | Out-Null"
    )
    time.sleep(0.7)
    try:
        yield
    finally:
        _ps(f"Remove-NetFirewallRule -DisplayName '{TEST_BLOCK_NAME}' -ErrorAction SilentlyContinue")
        _ps(f"Remove-NetFirewallRule -DisplayName '{RULE_NAME}' -ErrorAction SilentlyContinue")
        for name in pre_enabled_blocks:
            _ps(f"Enable-NetFirewallRule -Name '{name}' -ErrorAction SilentlyContinue")


@pytest.fixture
def host_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("0.0.0.0", 0))
    sock.listen(8)
    port = sock.getsockname()[1]

    def accept_loop():
        while True:
            try:
                c, _ = sock.accept()
                c.close()
            except OSError:
                return
    threading.Thread(target=accept_loop, daemon=True).start()

    try:
        yield port
    finally:
        sock.close()


def _assert_localhost_reachable(port: int):
    c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    c.settimeout(2)
    try:
        c.connect(("127.0.0.1", port))
    finally:
        c.close()


def test_block_rule_prevents_connection(block_python_inbound, host_listener):
    """Sanity: при наличии Block-правила на python.exe соединение должно
    блокироваться (без этого нет смысла что-то чинить)."""
    _assert_localhost_reachable(host_listener)
    host_ip = _wsl_adapter_host_ip()
    assert not _wsl_can_connect(host_ip, host_listener), (
        f"WSL→{host_ip}:{host_listener} прошло несмотря на Block-правило — "
        f"тест не репродуцирует баг на этой машине"
    )


def test_fix_disables_block_and_creates_allow(block_python_inbound, host_listener):
    """С фиксом: вызов _ensure_windows_firewall_rule отключает конкурирующий
    Block + создаёт Allow → соединение проходит."""
    _assert_localhost_reachable(host_listener)
    host_ip = _wsl_adapter_host_ip()

    asyncio.run(SandboxSkill._ensure_windows_firewall_rule())
    assert _rule_exists(RULE_NAME), f"фикс должен был создать '{RULE_NAME}'"

    time.sleep(1.0)
    assert _wsl_can_connect(host_ip, host_listener), (
        f"после фикса WSL→{host_ip}:{host_listener} должен проходить"
    )
