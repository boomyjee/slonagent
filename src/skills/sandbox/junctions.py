"""Two-pool host-side mounting via NTFS junctions (Windows) or mount --bind (Linux).

Container видит две статичных точки: /mnt/ro и /mnt/rw. Внутри pool-папок
<memory_dir>/mnt/{ro,rw} мы создаём junctions/binds на реальные хост-пути.
Container не перезапускается на изменение списка папок.

Все функции pure (от self не зависят) — SandboxSkill их вызывает с готовыми
данными из своего конфига.
"""
import logging
import os
import platform
import subprocess

log = logging.getLogger(__name__)


def dedupe_nested(paths: list[str]) -> list[str]:
    """Оставляет только верхнеуровневые пути в списке (вложенные в выбранный родитель — отбрасываем)."""
    norm = sorted({p.replace("\\", "/").rstrip("/").lower(): p for p in paths}.items())
    kept_norms: list[str] = []
    kept: list[str] = []
    for n, orig in norm:
        if any(n == k or n.startswith(k + "/") for k in kept_norms):
            continue
        kept_norms.append(n)
        kept.append(orig)
    return kept


def container_subpath(host_path: str) -> str:
    """'C:\\bla\\foo' → 'c/bla/foo', '/home/user/x' → 'home/user/x'."""
    p = host_path.replace("\\", "/").rstrip("/").lower()
    if len(p) >= 2 and p[1] == ":":
        return p[0] + "/" + p[2:].lstrip("/")
    return p.lstrip("/")


def is_link(path: str) -> bool:
    """True если path — Windows junction или Linux bind-mount."""
    if platform.system() == "Windows":
        try:
            import ctypes
            attrs = ctypes.windll.kernel32.GetFileAttributesW(path)
            return attrs != 0xFFFFFFFF and bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
        except Exception:
            return False
    # Linux: bind-mount = ismount(); regular dirs возвращают False.
    return os.path.ismount(path)


def link_target(path: str) -> str | None:
    """Возвращает target junction (Win) или bind-source (Linux). None если не читается."""
    if platform.system() == "Windows":
        try:
            return os.readlink(path)  # Python 3.8+ умеет junctions
        except OSError:
            return None
    try:
        r = subprocess.run(["findmnt", "-no", "SOURCE", path], capture_output=True, text=True)
        return r.stdout.strip() or None
    except FileNotFoundError:
        return None


def _path_eq(a: str, b: str) -> bool:
    r"""Сравнение путей: \\?\<x> и <x> считаем эквивалентными, регистр игнорируем,
    разделители унифицируем. os.readlink на junction отдаёт \\?\-префиксный путь."""
    def norm(p):
        p = p.replace("\\", "/").rstrip("/").lower()
        if p.startswith("//?/"):
            p = p[4:]
        return p
    return norm(a) == norm(b)


def create_link(link: str, target: str):
    if not os.path.isdir(target):
        raise RuntimeError(f"Sandbox: target папки не существует: {target}")
    if platform.system() == "Windows":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"mklink /J failed: {(r.stderr or r.stdout).strip()}")
    else:
        os.makedirs(link, exist_ok=True)
        r = subprocess.run(["sudo", "mount", "--bind", target, link], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"sudo mount --bind failed: {r.stderr.strip()}")


def remove_link(link: str):
    """Снимает junction (Win) или bind (Linux). На Win os.rmdir удаляет
    только reparse point — реальный target остаётся в первозданном виде."""
    if platform.system() == "Windows":
        try:
            os.rmdir(link)
        except OSError as e:
            log.warning("rmdir junction %s failed: %s", link, e)
    else:
        r = subprocess.run(["sudo", "umount", link], capture_output=True, text=True)
        if r.returncode != 0:
            log.warning("umount %s failed: %s", link, r.stderr.strip())
        try:
            os.rmdir(link)
        except OSError as e:
            log.warning("rmdir %s failed: %s", link, e)


def sync(pool_ro: str, pool_rw: str, expected: dict[str, tuple[str, str]]):
    """Приводит дерево {pool_ro,pool_rw} к виду из expected.
    expected: {host_link_path: (target, pool_name)}.

    Walk вручную через os.scandir, не os.walk: на Windows os.walk заходит внутрь
    junctions (это не symlinks, а reparse points), а нам надо обращаться с
    junction'ами как с терминалами. Реальные файлы (target junction'ов) НЕ удаляем —
    os.rmdir на junction убирает только reparse point. Чужой regular файл или
    непустая папка без managed контента — exception.
    """
    os.makedirs(pool_ro, exist_ok=True)
    os.makedirs(pool_rw, exist_ok=True)

    def cleanup(path: str):
        with os.scandir(path) as it:
            entries = list(it)
        for entry in entries:
            full = entry.path
            if not entry.is_dir(follow_symlinks=False):
                raise RuntimeError(f"Sandbox: чужая запись (не папка): {full}")
            if is_link(full):
                wanted = expected.get(full)
                if wanted and _path_eq(link_target(full) or "", wanted[0]):
                    continue
                log.info("[sandbox] removing junction %s", full)
                remove_link(full)
                continue
            cleanup(full)
            try:
                if not os.listdir(full):
                    os.rmdir(full)
                    continue
            except OSError:
                pass
            if not any(e.startswith(full + os.sep) for e in expected):
                raise RuntimeError(f"Sandbox: чужая папка с непонятным содержимым: {full}")

    cleanup(pool_ro)
    cleanup(pool_rw)

    for link, (target, pool) in expected.items():
        if os.path.exists(link) and is_link(link) and _path_eq(link_target(link) or "", target):
            continue
        os.makedirs(os.path.dirname(link), exist_ok=True)
        log.info("[sandbox] creating %s junction: %s → %s", pool, link, target)
        create_link(link, target)
