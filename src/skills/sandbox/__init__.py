import asyncio, base64, hashlib, json, os, re, logging, shlex, subprocess, threading
from typing import Annotated
from agent import Skill, tool

from src.skills.sandbox import script_tools

_SHELL_LOG_DIR = "/tmp/_sandbox_shells"


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
        # Кеш _ensure_container: пропускаем тяжёлые проверки если хеш набора
        # маунтов не менялся. На смене конфига хеш ломается → пересоздаём.
        self._mounts_hash: str | None = None

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

    def _mounts(self) -> list[tuple[str, str, bool]]:
        """[(host_path, container_path, readonly)] из конфига sandbox.ro / sandbox.rw."""
        from src.skills.config import ConfigSkill
        config = next((s for s in self.agent.skills if isinstance(s, ConfigSkill)), None) if self.agent else None
        if not config:
            return []

        def container_subpath(host: str) -> str:
            p = host.replace("\\", "/").rstrip("/").lower()
            if len(p) >= 2 and p[1] == ":":
                return p[0] + "/" + p[2:].lstrip("/")
            return p.lstrip("/")

        result: list[tuple[str, str, bool]] = []
        for p in config.get("sandbox.ro") or []:
            result.append((p, f"/mnt/ro/{container_subpath(p)}", True))
        for p in config.get("sandbox.rw") or []:
            result.append((p, f"/mnt/rw/{container_subpath(p)}", False))
        return result

    def resolve_path(self, container_path: str) -> str | None:
        if container_path == "/workspace":
            return self.workspace_dir
        if container_path.startswith("/workspace/"):
            return os.path.join(self.workspace_dir, container_path[len("/workspace/"):])
        for host, container, _ro in self._mounts():
            if container_path == container:
                return host
            prefix = container.rstrip("/") + "/"
            if container_path.startswith(prefix):
                return os.path.join(host, container_path[len(prefix):].replace("/", os.sep))
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
        mounts = self._mounts()
        if mounts:
            lines.append("Доступные хост-папки:")
            for host, container, ro in mounts:
                lines.append(f"  - [{'RO' if ro else 'RW'}] {host}  →  {container}")
        lines.append(
            "Чтобы добавить папку с хост-машины, попроси пользователя написать в чат:\n"
            "  /config write sandbox.ro[] <абсолютный путь>   # только для чтения\n"
            "  /config write sandbox.rw[] <абсолютный путь>   # для чтения и записи\n"
            "При смене списка контейнер один раз пересоздаётся (state сохраняется через committed image)."
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
        # `-t 0` пропускает 10-секундное ожидание SIGTERM. Контейнер крутит
        # `sleep infinity` — терять там нечего, можно сразу SIGKILL.
        subprocess.run([self.runtime, "rm", "-f", "-t", "0", self.container_name], capture_output=True)
        logging.info("[exec] Контейнер %s остановлен", self.container_name)

    def _volume_args(self):
        lib_dir = self._lib_dir()
        args = ["-v", f"{self.workspace_dir}:/workspace", "-v", f"{lib_dir}:/slonagent:ro"]
        for host, container, ro in self._mounts():
            args += ["-v", f"{host}:{container}:ro" if ro else f"{host}:{container}"]
        return args

    @staticmethod
    def _norm(path: str) -> str:
        """Normalize path for comparison: Windows→WSL mount format, lowercase."""
        p = path.replace("\\", "/").rstrip("/").lower()
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
        volume_args = self._volume_args()
        desired_mounts = {
            (self._norm(self.workspace_dir), "/workspace"),
            (self._norm(self._lib_dir()), "/slonagent"),
        }
        for host, container, _ro in self._mounts():
            desired_mounts.add((self._norm(host), container))
        # Хеш набора маунтов: на стабильном конфиге пропускаем дорогие проверки
        # (machine info + container inspect). На смене конфига хеш не совпадёт →
        # пробежимся ещё раз и закешируем новое значение.
        new_hash = hashlib.sha1(repr(sorted(desired_mounts)).encode()).hexdigest()
        if self._mounts_hash == new_hash:
            return
        await self._ensure_machine()
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
                await self._run([self.runtime, "rm", "-f", "-t", "0", self.container_name], capture_output=True)
                await self._run([self.runtime, "run", "-d", "--no-hosts", "--name", self.container_name, *volume_args, env_image, "sleep", "infinity"], check=True)
                logging.info("[exec] Контейнер %s пересоздан с образом %s", self.container_name, env_image)
        self._mounts_hash = new_hash

    @tool(
        "Выполнить команду внутри Docker-контейнера. "
        "Всегда доступна директория /workspace. "
        "Папки хост-машины монтируются по WSL-схеме: C:\\\\foo → /mnt/c/foo. "
        "background=true для долгоживущих процессов (сервер, watcher) — вернёт shell_id, "
        "читать вывод через sandbox_read_shell, убивать через sandbox_kill_shell."
    )
    async def exec(
        self,
        command: Annotated[str, "Строка команды для выполнения (bash/sh синтаксис)."],
        timeout: Annotated[int, "Таймаут в секундах (по умолчанию 120). Игнорируется в background-режиме."] = None,
        workdir: Annotated[str, "Рабочая директория внутри контейнера (по умолчанию /workspace)."] = "/workspace",
        background: Annotated[bool, "Запустить в фоне и сразу вернуть shell_id."] = False,
    ):
        try:
            await self._ensure_container()
        except Exception as e:
            return {"error": f"Не удалось запустить контейнер: {e}"}

        if background:
            # mv после запуска: nohup пишет в .tmp лог, мы переименовываем в {pid}.log
            # (mv в пределах одной FS не закрывает open fd — процесс продолжает писать
            # в тот же inode по новому имени).
            # ВАЖНО: TMP=... должен быть отдельным statement'ом до `&`, иначе
            # `&&` цепляется к `&` и присваивание уезжает в фоновый сабшелл.
            wrapped = (
                f"mkdir -p {_SHELL_LOG_DIR}; "
                f"TMP={_SHELL_LOG_DIR}/.tmp.$$.log; "
                f"nohup bash -lc {shlex.quote(command)} > $TMP 2>&1 & "
                f"PID=$!; "
                f"mv $TMP {_SHELL_LOG_DIR}/$PID.log; "
                f"echo $PID"
            )
            docker_cmd = [self.runtime, "exec", "-e", "PYTHONPATH=/slonagent", "-w", workdir,
                          self.container_name, "bash", "-lc", wrapped]
            logging.info("[exec] background: %s", command)
            try:
                proc = await self._run(docker_cmd, capture_output=True, text=True,
                                       encoding="utf-8", errors="replace", timeout=10)
            except Exception as e:
                return {"error": f"Не удалось запустить фоновый процесс: {e}"}
            pid = proc.stdout.strip()
            if not pid.isdigit():
                return {"error": f"Не удалось получить pid: {proc.stdout!r} / {proc.stderr!r}"}
            return {"shell_id": f"sh_{pid}", "pid": int(pid),
                    "log": f"{_SHELL_LOG_DIR}/{pid}.log"}

        if timeout is None:
            timeout = self.default_timeout
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

    @tool("Прочитать накопленный вывод фонового процесса по shell_id из exec(background=true). "
          "Возвращает {output, alive, lines_total}.")
    async def read_shell(
        self,
        shell_id: Annotated[str, "shell_id из exec(background=true)."],
        offset: Annotated[int, "С какой строки читать (1-based, по умолчанию 1)."] = 1,
        limit: Annotated[int, "Максимум строк (по умолчанию 500)."] = 500,
    ):
        if not shell_id.startswith("sh_") or not shell_id[3:].isdigit():
            return {"error": f"Невалидный shell_id: {shell_id}"}
        pid = shell_id[3:]
        log_path = f"{_SHELL_LOG_DIR}/{pid}.log"
        # Liveness: процесс жив только если /proc/$PID существует И состояние не Z (zombie).
        # `kill -0` возвращает success на zombie'ах, поэтому отдельно проверяем State.
        cmd = (
            f"P={shlex.quote(pid)}; F={shlex.quote(log_path)}; "
            f"if [ ! -f \"$F\" ]; then echo __NOLOG__; exit 0; fi; "
            f"echo __TOTAL__$(wc -l < \"$F\"); "
            f"sed -n {int(offset)},{int(offset)+int(limit)-1}p \"$F\"; "
            f"if [ -e /proc/$P/status ] && [ \"$(awk '/^State:/ {{print $2}}' /proc/$P/status 2>/dev/null)\" != Z ]; then "
            f"echo __ALIVE__1; else echo __ALIVE__0; fi"
        )
        proc = await self._run(
            [self.runtime, "exec", self.container_name, "bash", "-lc", cmd],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
        out = proc.stdout
        if "__NOLOG__" in out:
            return {"error": f"Лог не найден: {log_path}"}
        total = 0
        alive = False
        lines = []
        for line in out.splitlines():
            if line.startswith("__TOTAL__"):
                try: total = int(line[9:])
                except ValueError: pass
            elif line.startswith("__ALIVE__"):
                alive = line.endswith("1")
            else:
                lines.append(line)
        return {"output": "\n".join(lines), "alive": alive, "lines_total": total, "offset": offset}

    @tool("Прибить фоновый процесс по shell_id. SIGTERM, через 0.5с SIGKILL если выжил.")
    async def kill_shell(
        self,
        shell_id: Annotated[str, "shell_id из exec(background=true)."],
    ):
        if not shell_id.startswith("sh_") or not shell_id[3:].isdigit():
            return {"error": f"Невалидный shell_id: {shell_id}"}
        pid = shell_id[3:]
        cmd = f"kill -TERM {pid} 2>/dev/null; sleep 0.5; kill -KILL {pid} 2>/dev/null; true"
        await self._run([self.runtime, "exec", self.container_name, "bash", "-lc", cmd],
                        capture_output=True, text=True, timeout=10)
        return {"status": "killed", "pid": int(pid)}

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

    @tool("Прочитать файл. Текстовые файлы возвращают содержимое с префиксом номера строки "
          "(`<lineno>→<line>`), изображения передаются в LLM для анализа.")
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
            # Match Claude Code's addLineNumbers: правое выравнивание в 6 символов,
            # стрелка-разделитель. Числа ≥6 знаков выводятся без паддинга.
            def _fmt(n: int, line: str) -> str:
                ns = str(n)
                prefix = ns if len(ns) >= 6 else ns.rjust(6)
                if not line.endswith("\n"):
                    line += "\n"
                return f"{prefix}→{line}"
            numbered = "".join(_fmt(start + i + 1, line) for i, line in enumerate(chunk))
            return {"content": numbered, "total_lines": len(lines),
                    "returned_lines": len(chunk), "offset": start + 1}
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

    @tool("Применить несколько edit-операций к файлу атомарно. "
          "edits — список {old_string, new_string, replace_all?}. "
          "Если хоть одна не сматчится — НИКАКАЯ не применяется (rollback в памяти до записи).")
    def multi_edit(
        self,
        path: Annotated[str, "Путь к файлу."],
        edits: Annotated[list[dict], "Список объектов {old_string, new_string, replace_all?:bool}. Применяются последовательно."],
    ):
        host_path, err = self._check_path(path)
        if err:
            return err
        if not edits:
            return {"error": "edits пустой"}
        try:
            with open(host_path, encoding="utf-8") as f:
                content = f.read()
            replacements = []
            for i, edit in enumerate(edits, 1):
                if not isinstance(edit, dict):
                    return {"error": f"edit #{i}: не объект"}
                old = edit.get("old_string")
                new = edit.get("new_string")
                if old is None or new is None:
                    return {"error": f"edit #{i}: нужны old_string и new_string"}
                replace_all = bool(edit.get("replace_all", False))
                count = content.count(old)
                if count == 0:
                    return {"error": f"edit #{i}: old_string не найден"}
                if count > 1 and not replace_all:
                    return {"error": f"edit #{i}: old_string найден {count} раз — установи replace_all=true или дай больше контекста"}
                content = content.replace(old, new) if replace_all else content.replace(old, new, 1)
                replacements.append(count if replace_all else 1)
            with open(host_path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"status": "ok", "edits": len(edits), "replacements": replacements}
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

    _VCS_EXCLUDE = {".git", ".svn", ".hg", ".bzr", ".jj", ".sl"}

    # Минимальный mapping rg --type → glob-патернов. Не полный, но покрывает
    # самые частые языки. Для редких типов лучше явный glob.
    _RG_TYPES = {
        "py": ["*.py", "*.pyi"],
        "js": ["*.js", "*.mjs", "*.cjs", "*.jsx"],
        "ts": ["*.ts", "*.tsx"],
        "rust": ["*.rs"], "rs": ["*.rs"],
        "go": ["*.go"],
        "java": ["*.java"],
        "c": ["*.c", "*.h"],
        "cpp": ["*.cpp", "*.cxx", "*.cc", "*.hpp", "*.hxx", "*.hh"],
        "rb": ["*.rb"], "ruby": ["*.rb"],
        "sh": ["*.sh", "*.bash"],
        "html": ["*.html", "*.htm"],
        "css": ["*.css"],
        "json": ["*.json"],
        "yaml": ["*.yml", "*.yaml"],
        "md": ["*.md", "*.markdown"],
        "toml": ["*.toml"],
        "xml": ["*.xml"],
    }

    @tool("Поиск текста по файлам (regex). "
          "Дефолтный output_mode='files_with_matches' возвращает только пути (отсортировано по mtime). "
          "'content' возвращает строки с номерами + контекстом (-A/-B/-C или context). "
          "'count' возвращает счётчик матчей по файлам. "
          "Авто-исключаются VCS-директории (.git, .svn, .hg, .bzr, .jj, .sl).")
    def grep(
        self,
        pattern: Annotated[str, "Регулярное выражение для поиска."],
        path: Annotated[str, "Путь к файлу или директории."],
        glob: Annotated[str, "Glob-фильтр файлов, например '*.py' или '*.{ts,tsx}'."] = "",
        type: Annotated[str, "Тип файлов (rg --type): py, js, ts, rust, go, java, c, cpp, rb, sh, html, css, json, yaml, md, toml, xml."] = "",
        output_mode: Annotated[str, "'content' | 'files_with_matches' (дефолт) | 'count'."] = "files_with_matches",
        head_limit: Annotated[int, "Максимум результатов. По умолчанию 250. 0 = без лимита."] = 250,
        offset: Annotated[int, "Сколько результатов пропустить (для пагинации). По умолчанию 0."] = 0,
        context: Annotated[int, "Симметричный контекст (-C): N строк до и после. Применяется в content."] = 0,
        before: Annotated[int, "Контекст перед матчем (-B). Игнорируется если задан context."] = 0,
        after: Annotated[int, "Контекст после матча (-A). Игнорируется если задан context."] = 0,
        case_insensitive: Annotated[bool, "Регистронезависимый поиск (-i)."] = False,
        line_numbers: Annotated[bool, "Показывать номера строк в content-режиме (-n). Дефолт true."] = True,
        multiline: Annotated[bool, "Многострочный матчинг (-U --multiline-dotall)."] = False,
    ):
        import re, fnmatch
        host_path, err = self._check_path(path)
        if err:
            return err
        flags = 0
        if case_insensitive: flags |= re.IGNORECASE
        if multiline: flags |= re.MULTILINE | re.DOTALL
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return {"error": f"Невалидный regex: {e}"}
        if output_mode not in ("content", "files_with_matches", "count"):
            return {"error": f"Неизвестный output_mode: {output_mode}"}

        # Файлы для скана.
        type_globs = self._RG_TYPES.get(type.lower()) if type else None
        glob_patterns = [glob] if glob else None

        def _matches_filter(fname: str) -> bool:
            if glob_patterns and not any(fnmatch.fnmatch(fname, g) for g in glob_patterns):
                return False
            if type_globs and not any(fnmatch.fnmatch(fname, g) for g in type_globs):
                return False
            return True

        files = []
        if os.path.isfile(host_path):
            files = [host_path]
        else:
            for root, dirs, fnames in os.walk(host_path):
                # Исключаем VCS-директории.
                dirs[:] = [d for d in dirs if d not in self._VCS_EXCLUDE]
                for fn in fnames:
                    if not _matches_filter(fn):
                        continue
                    files.append(os.path.join(root, fn))

        def rel(fp):
            r = os.path.relpath(fp, host_path) if os.path.isdir(host_path) else os.path.basename(fp)
            return r.replace(os.sep, "/")

        def _apply_pagination(items: list) -> tuple[list, int | None]:
            """[items, applied_limit_if_truncated]. head_limit=0 → без лимита."""
            sliced = items[offset:]
            if head_limit == 0:
                return sliced, None
            limit = head_limit
            truncated = len(sliced) > limit
            return sliced[:limit], (limit if truncated else None)

        if output_mode == "files_with_matches":
            matched = []
            for fpath in files:
                try:
                    with open(fpath, encoding="utf-8", errors="replace") as f:
                        if regex.search(f.read()):
                            matched.append(fpath)
                except (UnicodeDecodeError, PermissionError):
                    continue
            # Сортировка по mtime (oldest first) — как rg --sort=modified.
            try:
                matched.sort(key=lambda p: os.path.getmtime(p))
            except OSError:
                pass
            paged, applied = _apply_pagination(matched)
            result = {"mode": "files_with_matches",
                      "filenames": [rel(p) for p in paged],
                      "numFiles": len(paged)}
            if applied is not None: result["appliedLimit"] = applied
            if offset: result["appliedOffset"] = offset
            return result

        if output_mode == "count":
            entries = []
            for fpath in files:
                try:
                    with open(fpath, encoding="utf-8", errors="replace") as f:
                        n = len(regex.findall(f.read()))
                    if n: entries.append((rel(fpath), n))
                except (UnicodeDecodeError, PermissionError):
                    continue
            paged, applied = _apply_pagination(entries)
            content_lines = [f"{p}:{n}" for p, n in paged]
            result = {"mode": "count",
                      "content": "\n".join(content_lines),
                      "filenames": [],
                      "numFiles": len(paged),
                      "numMatches": sum(n for _, n in paged)}
            if applied is not None: result["appliedLimit"] = applied
            if offset: result["appliedOffset"] = offset
            return result

        # output_mode == "content"
        # Контекст: -C / context имеет приоритет над -B/-A.
        ctx_before = context if context else before
        ctx_after = context if context else after
        all_lines = []
        for fpath in files:
            try:
                with open(fpath, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except (UnicodeDecodeError, PermissionError):
                continue
            file_lines = text.splitlines()
            r = rel(fpath)
            def _fmt(line_idx: int) -> str:  # 0-based -> с/без номера
                num = line_idx + 1
                body = file_lines[line_idx] if line_idx < len(file_lines) else ""
                return f"{r}:{num}:{body}" if line_numbers else f"{r}:{body}"
            if multiline:
                for m in regex.finditer(text):
                    start_line = text[:m.start()].count("\n")
                    end_line = start_line + m.group(0).count("\n")
                    for i in range(start_line, end_line + 1):
                        if i < len(file_lines):
                            all_lines.append(_fmt(i))
            else:
                for i, line in enumerate(file_lines):
                    if regex.search(line):
                        lo = max(0, i - ctx_before)
                        hi = min(len(file_lines) - 1, i + ctx_after)
                        for j in range(lo, hi + 1):
                            all_lines.append(_fmt(j))
        paged, applied = _apply_pagination(all_lines)
        result = {"mode": "content",
                  "content": "\n".join(paged),
                  "numFiles": 0,
                  "filenames": [],
                  "numLines": len(paged)}
        if applied is not None: result["appliedLimit"] = applied
        if offset: result["appliedOffset"] = offset
        return result

    @tool("Найти файлы по glob-паттерну. Поддерживает '**' для рекурсии и {a,b} для альтернатив. "
          "Сортирует по mtime (oldest first, как rg --sort=modified). "
          "Авто-исключаются VCS-директории (.git и т.п.). Лимит 100, флаг truncated если обрезано.")
    def glob(
        self,
        pattern: Annotated[str, "Glob-паттерн, например **/*.py или *.{ts,tsx}."],
        path: Annotated[str, "Директория для поиска."],
    ):
        import glob as glob_module
        host_path, err = self._check_path(path)
        if err:
            return err
        if not os.path.isdir(host_path):
            return {"error": f"Не директория: {path}"}
        # Поддержка brace-expansion: *.{ts,tsx} → [*.ts, *.tsx].
        expanded = []
        m = re.match(r"^(.*)\{([^}]+)\}(.*)$", pattern)
        if m:
            pre, opts, post = m.group(1), m.group(2), m.group(3)
            for o in opts.split(","):
                expanded.append(f"{pre}{o.strip()}{post}")
        else:
            expanded = [pattern]
        matches: dict[str, float] = {}
        for pat in expanded:
            full_pat = os.path.join(host_path, pat)
            for fpath in glob_module.iglob(full_pat, recursive=True):
                if not os.path.isfile(fpath):
                    continue
                rel_path = os.path.relpath(fpath, host_path).replace(os.sep, "/")
                # Исключаем VCS-директории на любом уровне.
                if any(part in self._VCS_EXCLUDE for part in rel_path.split("/")):
                    continue
                try:
                    mtime = os.path.getmtime(fpath)
                except OSError:
                    mtime = 0.0
                matches[rel_path] = mtime
        # rg --sort=modified — ascending (oldest first).
        ordered = sorted(matches.items(), key=lambda x: x[1])
        LIMIT = 100
        truncated = len(ordered) > LIMIT
        return {"filenames": [p for p, _ in ordered[:LIMIT]],
                "numFiles": min(len(ordered), LIMIT),
                "truncated": truncated}
