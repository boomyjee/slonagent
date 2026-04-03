import ast, asyncio, base64, json, os, re, logging, subprocess
from typing import Annotated
from agent import Skill, tool


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

    async def start(self):
        self.workspace_dir = self.workspace_dir or os.path.join(self.agent.memory.memory_dir, "workspace")
        os.makedirs(self.workspace_dir, exist_ok=True)
        sanitized = re.sub(r"[^a-z0-9]+", "_", self.agent.agent_dir.lower()).strip("_")
        self.container_name = self.container_name or f"slonagent_{sanitized}"
        self.tools_dir = os.path.join(self.workspace_dir, "tools")
        os.makedirs(self.tools_dir, exist_ok=True)

    def get_tools(self) -> list:
        return self._tools + self._scan_script_tools()

    _AST_TYPES = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}

    def _scan_script_tools(self) -> list:
        self._skill_script_map = {}
        result = []
        for fname in sorted(os.listdir(self.tools_dir)):
            if not fname.endswith(".py"):
                continue
            script_path = os.path.join(self.tools_dir, fname)
            for t in self._introspect_ast(script_path):
                t["function"]["name"] = "sandbox_" + t["function"]["name"]
                self._skill_script_map[t["function"]["name"]] = script_path
                result.append(t)
        return result

    def _introspect_ast(self, path: str) -> list:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                tree = ast.parse(f.read())
        except SyntaxError:
            return []

        tools = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if not any(
                (isinstance(b, ast.Name) and b.id == "Skill") or
                (isinstance(b, ast.Attribute) and b.attr == "Skill")
                for b in node.bases
            ):
                continue
            prefix = node.name.removesuffix("Skill").removesuffix("Memory").removesuffix("Provider").lower()
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                desc = self._get_tool_desc(item)
                if desc is None:
                    continue
                props, required = self._parse_params(item)
                tools.append({
                    "type": "function",
                    "function": {
                        "name": f"{prefix}_{item.name}",
                        "description": desc,
                        "parameters": {"type": "object", "properties": props, "required": required},
                    }
                })
        return tools

    @staticmethod
    def _get_tool_desc(func: ast.FunctionDef) -> str | None:
        for dec in func.decorator_list:
            if isinstance(dec, ast.Call):
                fn = dec.func
                if (isinstance(fn, ast.Name) and fn.id == "tool") or \
                   (isinstance(fn, ast.Attribute) and fn.attr == "tool"):
                    if dec.args and isinstance(dec.args[0], ast.Constant):
                        return dec.args[0].value
        return None

    def _parse_params(self, func: ast.FunctionDef) -> tuple[dict, list]:
        props = {}
        required = []
        args = func.args
        defaults_offset = len(args.args) - len(args.defaults)
        for i, arg in enumerate(args.args):
            if arg.arg == "self":
                continue
            annotation = arg.annotation
            schema = self._annotation_to_schema(annotation)
            props[arg.arg] = schema
            if i < defaults_offset:
                required.append(arg.arg)
        return props, required

    def _annotation_to_schema(self, node) -> dict:
        if node is None:
            return {"type": "string"}
        # Annotated[type, "desc"]
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "Annotated":
            elts = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            base = elts[0]
            desc = elts[1].value if len(elts) > 1 and isinstance(elts[1], ast.Constant) else None
            schema = self._base_type_schema(base)
            if desc:
                schema["description"] = desc
            return schema
        return self._base_type_schema(node)

    def _base_type_schema(self, node) -> dict:
        if isinstance(node, ast.Name):
            return {"type": self._AST_TYPES.get(node.id, "string")}
        # list[str] etc
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "list":
            item_type = "string"
            if isinstance(node.slice, ast.Name):
                item_type = self._AST_TYPES.get(node.slice.id, "string")
            return {"type": "array", "items": {"type": item_type}}
        return {"type": "string"}

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
        try:
            await self._ensure_container()
        except Exception as e:
            return {"error": f"Не удалось запустить контейнер: {e}"}

        fname = os.path.basename(script_path)
        cmd = [self.runtime, "exec", "-i", "-e", "PYTHONPATH=/slonagent", "-w", "/workspace",
               self.container_name, "python", "/slonagent/runner.py", f"/workspace/tools/{fname}"]

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )

        _ALLOWED = {
            "agent.transport.send_message", 
            "agent.transport.send_thinking", 
            "agent.transport.send_processing", 
            "agent.spawn_subagent",
            "agent.next_message", 
        }

        proc.stdin.write(json.dumps({"method": "call", "args": [tool_name], "kwargs": args}).encode() + b"\n")
        await proc.stdin.drain()

        while True:
            line = await proc.stdout.readline()
            if not line:
                stderr = (await proc.stderr.read()).decode()
                return {"error": f"Script exited without result. stderr: {stderr}"}

            msg = json.loads(line.decode())

            if msg["method"] == "result":
                proc.stdin.close()
                await proc.wait()
                return msg["args"][0] if msg["args"] else {}

            if msg["method"] not in _ALLOWED:
                resp = json.dumps({"error": f"method not allowed: {msg['method']}"})
            else:
                obj = self
                for attr in msg["method"].split("."):
                    obj = getattr(obj, attr)
                result = await obj(*msg.get("args", []), **msg.get("kwargs", {}))
                resp = json.dumps(result if isinstance(result, (dict, list)) else {"result": result})
            proc.stdin.write(resp.encode() + b"\n")
            await proc.stdin.drain()

    def _mounts(self) -> dict[str, str]:
        from src.skills.config import ConfigSkill
        config = next((s for s in self.agent.skills if isinstance(s, ConfigSkill)), None) if self.agent else None
        folders = config.get("sandbox.folders") or [] if config else []
        result = {}
        for f in folders:
            if len(f) >= 2 and f[1] == ":":
                drive, rest = f[0].lower(), f[2:].replace("\\", "/").lstrip("/")
                result[f] = f"/mnt/{drive}/{rest}"
            else:
                result[f] = f.replace("\\", "/")
        return result

    def resolve_path(self, container_path: str) -> str | None:
        if container_path == "/workspace":
            return self.workspace_dir
        if container_path.startswith("/workspace/"):
            return os.path.join(self.workspace_dir, container_path[len("/workspace/"):])
        for host, container in self._mounts().items():
            prefix = container.rstrip("/") + "/"
            if container_path.startswith(prefix):
                return os.path.join(host, container_path[len(prefix):].replace("/", os.sep))
        return None

    async def get_context_prompt(self, user_text: str = "") -> str:
        lines = [
            "## Sandbox",
            "Изолированный Docker-контейнер с правами root.",
            "Персистентный — файлы, установленные пакеты и состояние сохраняются между вызовами.",
            "Доступные пути: /workspace — рабочая директория (чтение и запись).",
        ]
        mounts = self._mounts()
        if mounts:
            lines.append("Примонтированные папки хост-машины (только чтение):")
            for host, container in mounts.items():
                lines.append(f"  - {host}  →  {container}")
        lines.append(
            "Чтобы примонтировать папку с хост-машины, попроси пользователя написать в чат команду (команда пойдет в обход тебя):\n"
            "  /config write sandbox.folders[] <абсолютный путь к папке>"
        )
        lines.append(
            "Python-скрипты в /workspace/tools/ автоматически становятся инструментами.\n"
            "Определи Skill-подклассы — они будут найдены автоматически:\n"
            "  from agent import Skill, tool\n"
            "  class MySkill(Skill):\n"
            "      @tool('Описание')\n"
            "      async def my_tool(self, arg: Annotated[str, 'Desc']) -> dict:\n"
            "          return {'result': 'ok'}"
        )
        return "\n".join(lines)

    @staticmethod
    async def _run(*args, **kwargs):
        return await asyncio.to_thread(subprocess.run, *args, **kwargs)

    def stop(self):
        subprocess.run([self.runtime, "rm", "-f", self.container_name], capture_output=True)
        logging.info("[exec] Контейнер %s остановлен", self.container_name)

    def _volume_args(self):
        lib_dir = self._lib_dir()
        args = ["-v", f"{self.workspace_dir}:/workspace", "-v", f"{lib_dir}:/slonagent:ro"]
        for host, container in self._mounts().items():
            args += ["-v", f"{host}:{container}:ro"]
        return args

    async def _ensure_container(self):
        volume_args = self._volume_args()
        desired_destinations = {"/workspace", "/slonagent"} | set(self._mounts().values())
        env_image = f"{self.container_name}_env"

        inspect = await self._run(
            [self.runtime, "inspect", "--format",
             "{{.State.Running}}\n{{range .Mounts}}{{.Destination}}\n{{end}}",
             self.container_name],
            capture_output=True, text=True, encoding="utf-8",
        )
        if inspect.returncode != 0:
            img = await self._run([self.runtime, "image", "exists", env_image], capture_output=True)
            image = env_image if img.returncode == 0 else self.image
            await self._run([self.runtime, "run", "-d", "--no-hosts", "--name", self.container_name, *volume_args, image, "sleep", "infinity"], check=True)
            logging.info("[exec] Контейнер %s создан (образ: %s)", self.container_name, image)
        else:
            lines = inspect.stdout.strip().splitlines()
            running = lines[0] == "true"
            actual_destinations = {l.strip() for l in lines[1:] if l.strip()}

            if not running:
                await self._run([self.runtime, "start", self.container_name], check=True)
                logging.info("[exec] Контейнер %s запущен", self.container_name)
            elif actual_destinations != desired_destinations:
                logging.info("[exec] Монтирования изменились, сохраняем образ и пересоздаём")
                await self._run([self.runtime, "commit", self.container_name, env_image], check=True)
                await self._run([self.runtime, "rm", "-f", self.container_name], capture_output=True)
                await self._run([self.runtime, "run", "-d", "--no-hosts", "--name", self.container_name, *volume_args, env_image, "sleep", "infinity"], check=True)
                logging.info("[exec] Контейнер %s пересоздан с образом %s", self.container_name, env_image)

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
