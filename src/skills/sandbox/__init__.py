import asyncio, base64, json, os, re, logging, subprocess, threading
from typing import Annotated
from agent import Skill, tool

from src.skills.sandbox import junctions, script_tools


class SandboxSkill(Skill):
    def __init__(
        self,
        workspace_dir: str | None = None,
        image: str = "python:3.11-slim",
        default_timeout: int = 120,
        runtime: str = "podman",
        container_name: str = None,
    ):
        super().__init__()
        self.workspace_dir = workspace_dir
        self.container_name = container_name
        self.tools_dir: str = ""
        self.image = image
        self.default_timeout = default_timeout
        self.runtime = runtime
        self._skill_script_map: dict[str, str] = {}
        # Кеш: контейнер уже проверен, machine жива, mount-набор совпадает.
        # Сбрасывается только если podman exec упадёт.
        self._container_ready: bool = False

    async def start(self):
        if self.agent.agent_dir is None:
            raise RuntimeError("SandboxSkill requires agent_dir — can't be used on ephemeral agents")
        self.workspace_dir = self.workspace_dir or os.path.join(self.agent.memory.memory_dir, "workspace")
        os.makedirs(self.workspace_dir, exist_ok=True)
        sanitized = re.sub(r"[^a-z0-9]+", "_", self.agent.agent_dir.lower()).strip("_")
        self.container_name = self.container_name or f"slonagent_{sanitized}"
        self.tools_dir = os.path.join(self.workspace_dir, "tools")
        os.makedirs(self.tools_dir, exist_ok=True)

    def get_tools(self) -> list:
        return self._tools + self._scan_script_tools()

    def _scan_script_tools(self) -> list:
        self._skill_script_map = {}
        result = []
        for fname in sorted(os.listdir(self.tools_dir)):
            path = os.path.join(self.tools_dir, fname)
            if fname.endswith(".py"):
                script_path = path
            elif os.path.isdir(path) and os.path.isfile(os.path.join(path, "__init__.py")):
                script_path = os.path.join(path, "__init__.py")
            else:
                continue
            for t in script_tools.introspect(script_path):
                t["function"]["name"] = "sandbox_" + t["function"]["name"]
                self._skill_script_map[t["function"]["name"]] = script_path
                result.append(t)
        return result

    def _lib_dir(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "container_lib")

    async def dispatch_tool_call(self, tool_call: dict) -> dict:
        name = tool_call["function"]["name"]
        if name in self._skill_script_map:
            script_path = self._skill_script_map[name]
            args = json.loads(tool_call["function"].get("arguments") or "{}")
            return await self._dispatch_skill_script(script_path, name.removeprefix("sandbox_"), args)
        return await super().dispatch_tool_call(tool_call)

    async def _dispatch_skill_script(self, script_path, tool_name, args):
        from src.skills.sandbox.container_lib.rpc import Channel, Proxy
        from src.skills.sandbox.web_transport_bridge import WebTransportBridge
        from src.transport.base import BaseTransport
        from src.transport.multi import MultiTransport
        from src.transport.web import WebTransport as HostWebTransport
        from src.memory.memory import Memory
        from agent import Agent

        try:
            await self._ensure_container()
        except Exception as e:
            return {"error": f"Не удалось запустить контейнер: {e}"}

        rel = os.path.relpath(script_path, self.workspace_dir).replace("\\", "/")
        cmd = [self.runtime, "exec", "-i", "-e", "PYTHONPATH=/slonagent", "-w", "/workspace",
               self.container_name, "python", "/slonagent/runner.py", f"/workspace/{rel}"]

        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
        )

        # Drain stderr in a background thread — if the pipe buffer fills (~64KB
        # on Windows) the container blocks on its next stderr write, and since
        # our RPC reply also goes to stdout we'd deadlock. We also keep a copy
        # so that if the script dies before Channel starts (e.g. ImportError),
        # the RPC reader sees EOF and fails pending calls with generic "channel
        # closed" — we can still surface the real traceback from stderr.
        stderr_buf: list[str] = []
        def _drain_stderr():
            for line in iter(proc.stderr.readline, b""):
                decoded = line.decode("utf-8", errors="replace").rstrip()
                stderr_buf.append(decoded)
                logging.info("[sandbox-err] %s", decoded)
        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True, name="sandbox-stderr")
        stderr_thread.start()

        def readline():
            line = proc.stdout.readline()
            return line.decode("utf-8") if line else ""

        def writeline(msg):
            proc.stdin.write(json.dumps(msg, ensure_ascii=False).encode() + b"\n")
            proc.stdin.flush()

        allowed = {
            Agent: {"transport", "memory", "spawn_subagent", "next_message",
                    "loop", "get_agent_dir", "process_message"},
            BaseTransport: {
                "send_message", "send_thinking", "send_memory_info", "send_processing",
                "send_system_prompt", "on_tool_call", "on_tool_result",
                "inject_message", "send_app_url",
            },
            HostWebTransport: None,
            Memory: {"clear", "add_turn"},
        }

        # Pin async handlers to the host's main loop — aiogram, uvicorn and
        # friends create sessions bound to whichever loop started them, so
        # callbacks like transport.send_message MUST run on that loop.
        ch = Channel(readline, writeline, ref_prefix="h", allowed=allowed,
                     async_loop=asyncio.get_running_loop())
        ch.register("WebTransportBridge", WebTransportBridge(self))
        ch.register("MultiTransport", MultiTransport)
        ch.start()

        call_error: Exception | None = None
        result: dict | None = None
        try:
            r = await Proxy(ch, "runner").run_tool(name=tool_name, args=args, agent=self.agent)
            result = r if isinstance(r, dict) else {"result": r}
        except Exception as e:
            call_error = e
        finally:
            ch.close()
            try: proc.stdin.close()
            except Exception: pass
            await asyncio.to_thread(proc.wait)
            stderr_thread.join(timeout=2.0)

        if call_error is not None:
            err_text = "\n".join(stderr_buf).strip()
            result = {"error": err_text or str(call_error)}

        return result

    def _pool_dirs(self) -> tuple[str, str]:
        """(<memory_dir>/mnt/ro, <memory_dir>/mnt/rw) — host-side pool roots, в которых
        живут junctions/binds. Контейнер видит их как /mnt/ro и /mnt/rw."""
        base = os.path.join(self.agent.memory.memory_dir, "mnt")
        return os.path.join(base, "ro"), os.path.join(base, "rw")

    def _read_pool_config(self) -> tuple[list[str], list[str]]:
        """Читает sandbox.ro / sandbox.rw из ConfigSkill, дедупит вложенные пути в каждом пуле."""
        from src.skills.config import ConfigSkill
        config = next((s for s in self.agent.skills if isinstance(s, ConfigSkill)), None) if self.agent else None
        if not config:
            return [], []
        return junctions.dedupe_nested(config.get("sandbox.ro") or []), \
               junctions.dedupe_nested(config.get("sandbox.rw") or [])

    def _expected_links(self) -> dict[str, tuple[str, str]]:
        """{host_link_path → (target, pool_name)}. pool_name — 'ro' или 'rw' (для логов)."""
        ro_paths, rw_paths = self._read_pool_config()
        pool_ro, pool_rw = self._pool_dirs()
        result: dict[str, tuple[str, str]] = {}
        for paths, pool_dir, pool in [(ro_paths, pool_ro, "ro"), (rw_paths, pool_rw, "rw")]:
            for p in paths:
                link = os.path.join(pool_dir, junctions.container_subpath(p).replace("/", os.sep))
                result[link] = (p, pool)
        return result

    def resolve_path(self, container_path: str) -> str | None:
        if container_path == "/workspace":
            return self.workspace_dir
        if container_path.startswith("/workspace/"):
            return os.path.join(self.workspace_dir, container_path[len("/workspace/"):])
        pool_ro, pool_rw = self._pool_dirs()
        for prefix, base in (("/mnt/ro/", pool_ro), ("/mnt/rw/", pool_rw)):
            if container_path.startswith(prefix):
                return os.path.join(base, container_path[len(prefix):].replace("/", os.sep))
        return None

    async def get_context_prompt(self, user_text: str = "") -> str:
        lines = [
            "## Sandbox",
            "Изолированный Docker-контейнер с правами root.",
            "Персистентный — файлы, установленные пакеты и состояние сохраняются между вызовами.",
            "Пути:",
            "  /workspace — рабочая директория (чтение и запись).",
            "  /mnt/ro/<drive>/<path> — папки хоста только для чтения.",
            "  /mnt/rw/<drive>/<path> — папки хоста для чтения и записи.",
        ]
        ro_paths, rw_paths = self._read_pool_config()
        if ro_paths or rw_paths:
            lines.append("Доступные хост-папки:")
            for p in ro_paths:
                lines.append(f"  - [RO] {p}  →  /mnt/ro/{junctions.container_subpath(p)}")
            for p in rw_paths:
                lines.append(f"  - [RW] {p}  →  /mnt/rw/{junctions.container_subpath(p)}")
        lines.append(
            "Чтобы добавить папку с хост-машины, попроси пользователя написать в чат:\n"
            "  /config write sandbox.ro[] <абсолютный путь>   # только для чтения\n"
            "  /config write sandbox.rw[] <абсолютный путь>   # для чтения и записи"
        )
        lines.append(
            "Python-скрипты в /workspace/tools/ автоматически становятся инструментами.\n"
            "Перед тем как писать скилл — прочитай /slonagent/SKILLS.md "
            "(это оглавление со ссылками на детали)."
        )
        return "\n".join(lines)

    @staticmethod
    async def _run(*args, **kwargs):
        return await asyncio.to_thread(subprocess.run, *args, **kwargs)

    def stop(self):
        subprocess.run([self.runtime, "rm", "-f", self.container_name], capture_output=True)
        logging.info("[exec] Контейнер %s остановлен", self.container_name)

    def _volume_args(self):
        pool_ro, pool_rw = self._pool_dirs()
        os.makedirs(pool_ro, exist_ok=True)
        os.makedirs(pool_rw, exist_ok=True)
        return [
            "-v", f"{self.workspace_dir}:/workspace",
            "-v", f"{self._lib_dir()}:/slonagent:ro",
            "-v", f"{pool_ro}:/mnt/ro:ro",
            "-v", f"{pool_rw}:/mnt/rw",
        ]

    @staticmethod
    def _norm(path: str) -> str:
        """Normalize path for comparison: Windows→WSL mount format, lowercase."""
        p = path.replace("\\", "/").rstrip("/").lower()
        # os.readlink на junction отдаёт `\\?\<target>` (Windows path namespace).
        # Сравнения должны игнорировать этот префикс.
        if p.startswith("//?/"):
            p = p[4:]
        # Convert Windows drive path to WSL: e:/foo → /mnt/e/foo
        if len(p) >= 2 and p[1] == ":":
            p = f"/mnt/{p[0]}{p[2:]}"
        return p

    async def _ensure_machine(self):
        info = await self._run(
            [self.runtime, "machine", "info", "--format", "{{.Host.MachineState}}"],
            capture_output=True, text=True,
        )
        if info.returncode != 0 or info.stdout.strip().lower() != "running":
            logging.info("[exec] Starting podman machine...")
            await self._run([self.runtime, "machine", "start"], check=True)

    async def _ensure_container(self):
        # Junctions sync — лёгкий (просто scandir), делаем всегда: config мог
        # поменяться без events, а sync обнаружит и поправит.
        pool_ro, pool_rw = self._pool_dirs()
        junctions.sync(pool_ro, pool_rw, self._expected_links())
        # Тяжёлые проверки (podman machine info, podman inspect) — один раз
        # за жизнь инстанса. Если контейнер потом сломается — exec вернёт
        # ошибку, и пользователь увидит её прямо в чате.
        if self._container_ready:
            return
        await self._ensure_machine()
        volume_args = self._volume_args()
        desired_mounts = {
            (self._norm(self.workspace_dir), "/workspace"),
            (self._norm(self._lib_dir()), "/slonagent"),
            (self._norm(pool_ro), "/mnt/ro"),
            (self._norm(pool_rw), "/mnt/rw"),
        }
        env_image = f"{self.container_name}_env"

        inspect = await self._run(
            [self.runtime, "inspect", "--format",
             '{{.State.Running}}\n{{range .Mounts}}{{.Source}}\t{{.Destination}}\n{{end}}',
             self.container_name],
            capture_output=True, text=True, encoding="utf-8",
        )
        if inspect.returncode != 0:
            img = await self._run([self.runtime, "image", "exists", env_image], capture_output=True)
            image = env_image if img.returncode == 0 else self.image
            run = await self._run([self.runtime, "run", "-d", "--no-hosts", "--name", self.container_name, *volume_args, image, "sleep", "infinity"], capture_output=True, text=True)
            if run.returncode != 0:
                raise RuntimeError(f"podman run failed ({run.returncode}): {run.stderr.strip()}")
            logging.info("[exec] Контейнер %s создан (образ: %s)", self.container_name, image)
        else:
            lines = inspect.stdout.strip().splitlines()
            running = lines[0] == "true"
            actual_mounts = set()
            for l in lines[1:]:
                parts = l.strip().split("\t")
                if len(parts) == 2:
                    actual_mounts.add((self._norm(parts[0]), parts[1]))

            if not running:
                await self._run([self.runtime, "start", self.container_name], check=True)
                logging.info("[exec] Контейнер %s запущен", self.container_name)
            elif actual_mounts != desired_mounts:
                logging.info("[exec] Монтирования изменились, сохраняем образ и пересоздаём")
                await self._run([self.runtime, "commit", self.container_name, env_image], check=True)
                await self._run([self.runtime, "rm", "-f", self.container_name], capture_output=True)
                await self._run([self.runtime, "run", "-d", "--no-hosts", "--name", self.container_name, *volume_args, env_image, "sleep", "infinity"], check=True)
                logging.info("[exec] Контейнер %s пересоздан с образом %s", self.container_name, env_image)
        self._container_ready = True

    @tool(
        "Выполнить команду внутри Docker-контейнера. "
        "Всегда доступна директория /workspace. "
        "Папки хост-машины монтируются по WSL-схеме: C:\\\\foo → /mnt/c/foo."
    )
    async def exec(
        self,
        command: Annotated[str, "Строка команды для выполнения (bash/sh синтаксис)."],
        timeout: Annotated[int, "Таймаут в секундах (по умолчанию 120)."] = None,
        workdir: Annotated[str, "Рабочая директория внутри контейнера (по умолчанию /workspace)."] = "/workspace",
    ):
        if timeout is None:
            timeout = self.default_timeout

        try:
            await self._ensure_container()
        except Exception as e:
            return {"error": f"Не удалось запустить контейнер: {e}"}

        docker_cmd = [self.runtime, "exec", "-e", "PYTHONPATH=/slonagent", "-w", workdir, self.container_name, "bash", "-lc", command]
        logging.info("[exec] Запуск команды: %s", command)

        try:
            proc = await self._run(
                docker_cmd,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout,
            )
        except FileNotFoundError:
            err = f"{self.runtime} not found. Установи {self.runtime} и добавь его в PATH."
            logging.error("[exec] %s", err)
            return {"error": err}
        except subprocess.TimeoutExpired:
            err = f"Команда превысила таймаут {timeout} секунд и была прервана."
            logging.error("[exec] %s", err)
            return {"error": err}
        except Exception as e:
            err = f"Ошибка при запуске {self.runtime}: {e}"
            logging.error("[exec] %s", err)
            return {"error": err}

        stdout = proc.stdout
        stderr = proc.stderr

        CHAR_LIMIT = 40000
        res = {}
        if len(stdout)>CHAR_LIMIT or len(stderr)>CHAR_LIMIT:
            res['error'] = "Overflow, output truncated"
            stdout = stdout[:CHAR_LIMIT]
            stderr = stderr[:CHAR_LIMIT]

        logging.info("[exec] exit_code=%d", proc.returncode)
        if stdout: logging.info("[exec] stdout:\n%s", stdout.rstrip())
        if stderr: logging.warning("[exec] stderr:\n%s", stderr.rstrip())

        return {**res, "stdout": stdout, "stderr": stderr, "exit_code": proc.returncode}

    _IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp"}
    _IMAGE_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                   "gif": "image/gif", "webp": "image/webp"}

    def _check_path(self, path: str) -> tuple[str | None, dict | None]:
        host_path = self.resolve_path(path)
        if host_path is None:
            return None, {"error": f"Доступ запрещён: {path}"}
        if not os.path.exists(host_path):
            return None, {"error": f"Файл не найден: {path}"}
        return host_path, None

    @tool("Прочитать файл. Текстовые файлы возвращают содержимое, изображения передаются в LLM для анализа.")
    def read(
        self,
        path: Annotated[str, "Путь к файлу (например /workspace/notes.txt или /mnt/c/project/main.py)."],
        offset: Annotated[int, "Начальная строка (1-based). По умолчанию 1."] = 1,
        limit: Annotated[int, "Максимальное число строк. По умолчанию 2000."] = 2000,
    ):
        host_path, err = self._check_path(path)
        if err:
            return err
        ext = os.path.splitext(host_path)[1].lower().lstrip(".")
        if ext in self._IMAGE_EXTS:
            with open(host_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            mime = self._IMAGE_MIME.get(ext, "image/jpeg")
            return {"_parts": [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]}
        try:
            with open(host_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            start = max(0, offset - 1)
            chunk = lines[start:start + limit]
            return {"content": "".join(chunk), "total_lines": len(lines), "returned_lines": len(chunk), "offset": start + 1}
        except Exception as e:
            return {"error": str(e)}

    @tool("Заменить текст в файле. old_string должен быть уникальным фрагментом файла.")
    def edit(
        self,
        path: Annotated[str, "Путь к файлу."],
        old_string: Annotated[str, "Текст для замены (должен быть уникальным в файле)."],
        new_string: Annotated[str, "Новый текст."],
        replace_all: Annotated[bool, "Заменить все вхождения (по умолчанию false)."] = False,
    ):
        host_path, err = self._check_path(path)
        if err:
            return err
        try:
            with open(host_path, encoding="utf-8") as f:
                content = f.read()
            count = content.count(old_string)
            if count == 0:
                return {"error": "old_string не найден в файле"}
            if count > 1 and not replace_all:
                return {"error": f"old_string найден {count} раз — используй replace_all=true или передай более длинный фрагмент"}
            new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
            with open(host_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            return {"status": "ok", "replacements": count if replace_all else 1}
        except Exception as e:
            return {"error": str(e)}

    @tool("Создать новый файл или полностью перезаписать существующий.")
    def write(
        self,
        path: Annotated[str, "Путь к файлу."],
        content: Annotated[str, "Содержимое файла."],
    ):
        host_path = self.resolve_path(path)
        if host_path is None:
            return {"error": f"Доступ запрещён: {path}"}
        try:
            os.makedirs(os.path.dirname(host_path), exist_ok=True)
            with open(host_path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"status": "ok", "path": path}
        except Exception as e:
            return {"error": str(e)}

    @tool("Поиск текста по файлам (regex). Возвращает совпавшие строки с номерами.")
    def grep(
        self,
        pattern: Annotated[str, "Регулярное выражение для поиска."],
        path: Annotated[str, "Путь к файлу или директории."],
        glob_filter: Annotated[str, "Фильтр файлов, например *.py (опционально)."] = "",
        max_results: Annotated[int, "Максимум результатов. По умолчанию 50."] = 50,
    ):
        import re, fnmatch
        host_path, err = self._check_path(path)
        if err:
            return err
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return {"error": f"Невалидный regex: {e}"}
        results = []
        files = []
        if os.path.isfile(host_path):
            files = [host_path]
        else:
            for root, _, fnames in os.walk(host_path):
                for fn in fnames:
                    if glob_filter and not fnmatch.fnmatch(fn, glob_filter):
                        continue
                    files.append(os.path.join(root, fn))
        for fpath in files:
            try:
                with open(fpath, encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if regex.search(line):
                            rel = os.path.relpath(fpath, host_path) if os.path.isdir(host_path) else os.path.basename(fpath)
                            results.append(f"{rel}:{i}: {line.rstrip()}")
                            if len(results) >= max_results:
                                return {"matches": results, "truncated": True}
            except (UnicodeDecodeError, PermissionError):
                continue
        return {"matches": results, "truncated": False}

    @tool("Найти файлы по glob-паттерну.")
    def glob(
        self,
        pattern: Annotated[str, "Glob-паттерн, например **/*.py или *.txt."],
        path: Annotated[str, "Директория для поиска."],
    ):
        import fnmatch
        host_path, err = self._check_path(path)
        if err:
            return err
        if not os.path.isdir(host_path):
            return {"error": f"Не директория: {path}"}
        matches = []
        for root, dirs, fnames in os.walk(host_path):
            # Skip hidden dirs
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in fnames:
                rel = os.path.relpath(os.path.join(root, fn), host_path).replace(os.sep, "/")
                if fnmatch.fnmatch(rel, pattern):
                    matches.append(rel)
        matches.sort()
        return {"files": matches[:500], "total": len(matches)}
