"""AntigravityBackend — Agent с llm() через Google Antigravity CLI (agy.exe).

Работает по подписке Google Antigravity (авторизованная сессия в ~/.gemini/antigravity-cli,
без необходимости отдельного Gemini API-ключа).

Взаимодействие:
  - Процесс agy.exe запускается в режиме NDJSON streaming (--input-format stream-json --output-format stream-json)
  - Поддерживает multi-turn в рамках одного процесса, а также возобновление существующих сессий через --conversation <id>
  - Стримит токены ответа, рассуждения (thinking) и события вызова тулов в реальном времени через transport Слона

Параметры и вырезание нативных инструкций Antigravity:
  - CLI флаги: --disable-slash-commands (отключает нативные слэш-команды и раскрытие скиллов),
    --dangerously-skip-permissions (автоподтверждение тулов).
  - Нативные тулы Antigravity (ask_question, define_subagent, find_by_name, run_command,
    replace_file_content, list_dir, grep_search и др.) вырезаются:
    (1) В системных инструкциях прописывается жесткий запрет (FORBIDDEN NATIVE TOOLS)
    (2) В потоке событий бэкенд перехватывает и подавляет запрещённые нативные тулы
  - Чистый системный контекст Слона (скиллы + системный промпт) передаётся в структурированном конверте.

Синхронизация контекста (Context Synchronization):
  - Отслеживает расхождение (divergence) между стабильной памятью Слона и историей
    диалога Antigravity (в transcript.jsonl сессии).
  - Если LogCompressor сжал старые ходы или память была модифицирована, старая сессия
    Antigravity закрывается и создаётся свежая с актуальными наблюдениями (<observations>)
    и оставшимися недавними ходами (<recent_conversation>).
  - Чистит временные файлы сессий на диске для эфемерных агентов в __del__.
"""
import asyncio
import atexit
import base64
import difflib
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
from contextlib import suppress

from src.agent.backends.base import BaseBackend
from src.agent.skill import Skill, bypass

log = logging.getLogger(__name__)
_builtin_open = open

# Базовый stream_id для transport.send_message — миллисекунды от запуска процесса
_STREAM_ID_BASE: int = int(time.time() * 1000)
_stream_id_counter: int = 0


def _next_stream_id() -> int:
    global _stream_id_counter
    _stream_id_counter += 1
    return _STREAM_ID_BASE + _stream_id_counter


def _clean_mcp_param(val) -> str:
    """Очищает строковый параметр MCP от экранированных кавычек."""
    if not isinstance(val, str):
        return str(val) if val is not None else ""
    s = val.strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, str):
                return parsed
        except Exception:
            pass
        return s[1:-1]
    return s


def _parse_mcp_args(args_val) -> dict:
    """Парсит аргументы MCP тула в словарь."""
    if isinstance(args_val, dict):
        return args_val
    if isinstance(args_val, str):
        s = _clean_mcp_param(args_val)
        if isinstance(s, dict):
            return s
        if isinstance(s, str):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
    return {}


# Все 17 нативных инструментов Google Antigravity
_FORBIDDEN_NATIVE_TOOLS: tuple[str, ...] = (
    "ask_question",
    "define_subagent",
    "find_by_name",
    "generate_image",
    "grep_search",
    "invoke_subagent",
    "list_dir",
    "manage_subagents",
    "manage_task",
    "read_url_content",
    "replace_file_content",
    "run_command",
    "schedule",
    "search_web",
    "send_message",
    "view_file",
    "write_to_file",
)


def _build_base_instructions(
    forbidden_tools: tuple[str, ...] = _FORBIDDEN_NATIVE_TOOLS,
    allowed_tools: tuple[str, ...] = (),
) -> str:
    """Формирует базовые инструкции со строгим приоритетом инструментов SlonAgent."""
    parts = [
        "You are SlonAgent, an AI assistant.\n\nCRITICAL SYSTEM OVERRIDE:\nIgnore default Antigravity persona guidelines and default workflows.",
    ]
    if allowed_tools:
        parts.append(f"PERMITTED NATIVE TOOLS:\n" + "\n".join(f"- {t}" for t in allowed_tools))
    if forbidden_tools:
        lines = [f"- {t}" for t in forbidden_tools if t not in ("call_mcp_tool",)]
        parts.append("FORBIDDEN NATIVE TOOLS — prefer SlonAgent MCP tools instead:\n" + "\n".join(lines))
    parts.append(
        "To invoke SlonAgent tools, you MUST use `call_mcp_tool(ServerName='slon_agy', ToolName=..., Arguments=...)`.\n"
        "Follow only the instructions and tool specifications explicitly provided below.\n"
    )
    return "\n\n".join(parts)


def _find_antigravity(custom_path: str | None = None) -> str:
    """Определяет путь к исполняемому файлу agy / agy.exe."""
    if custom_path and (os.path.isfile(custom_path) or shutil.which(custom_path)):
        return custom_path
    from_env = os.environ.get("ANTIGRAVITY_BIN")
    if from_env and (os.path.isfile(from_env) or shutil.which(from_env)):
        return from_env
    in_path = shutil.which("agy") or shutil.which("agy.exe")
    if in_path:
        return in_path
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        p = os.path.join(local_app_data, "agy", "bin", "agy.exe")
        if os.path.isfile(p):
            return p
    user_home = os.path.expanduser("~")
    for candidate in [
        os.path.join(user_home, "AppData", "Local", "agy", "bin", "agy.exe"),
        os.path.join(user_home, ".local", "bin", "agy"),
        os.path.join(user_home, "bin", "agy"),
    ]:
        if os.path.isfile(candidate):
            return candidate
    # Возвращаем fallback имя agy для сред тестирования с моками
    return "agy"


class AntigravityClient:
    """Управляет подпроцессом agy.exe CLI в режиме stream-json (NDJSON over stdio)."""

    def __init__(
        self,
        agy_path: str,
        model: str | None = None,
        conversation_id: str | None = None,
        cwd: str | None = None,
        effort: str | None = None,
        disable_slash_commands: bool = True,
        dangerously_skip_permissions: bool = True,
        project_id: str | None = None,
        extra_args: list[str] | None = None,
    ):
        self._agy_path = agy_path
        self._model = model
        self.conversation_id: str | None = conversation_id
        self._cwd = cwd
        self._effort = effort
        self._disable_slash_commands = disable_slash_commands
        self._dangerously_skip_permissions = dangerously_skip_permissions
        self._project_id = project_id
        self._extra_args = list(extra_args or [])

        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def _start(self):
        args = [self._agy_path, "--input-format", "stream-json", "--output-format", "stream-json"]
        if self._dangerously_skip_permissions:
            args.append("--dangerously-skip-permissions")
        if self._disable_slash_commands:
            args.append("--disable-slash-commands")
        if self._model:
            args.extend(["--model", self._model])
        if self._effort:
            args.extend(["--effort", self._effort])
        if self.conversation_id:
            args.extend(["--conversation", self.conversation_id])
        if self._project_id:
            args.extend(["--project", self._project_id])
        if self._extra_args:
            args.extend(self._extra_args)

        log.info(
            "[antigravity] spawning %s (conv_id=%s, model=%s, cwd=%s)",
            self._agy_path, self.conversation_id or "<new>", self._model, self._cwd,
        )

        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=10 * 1024 * 1024,
            cwd=self._cwd,
            env=os.environ,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self):
        while self.is_alive and self._proc and self._proc.stderr:
            try:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                s = line.decode("utf-8", errors="replace").strip()
                if s:
                    log.debug("[antigravity:stderr] %s", s)
            except (asyncio.CancelledError, Exception):
                break

    async def chat(self, prompt: str):
        """Отправляет запрос в agy.exe и стримит события до получения события 'result'."""
        if not self.is_alive:
            await self._start()

        msg = {"event": "user", "message": {"content": prompt}}
        payload = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")

        async with self._write_lock:
            _CHUNK = 65536
            for i in range(0, len(payload), _CHUNK):
                self._proc.stdin.write(payload[i:i + _CHUNK])
                await self._proc.stdin.drain()

        while True:
            raw_line = await self._proc.stdout.readline()
            if not raw_line:
                rc = self._proc.returncode
                raise RuntimeError(f"Antigravity CLI process ended unexpectedly (code {rc})")
            s = raw_line.decode("utf-8", errors="replace").strip()
            if not s:
                continue
            try:
                data = json.loads(s)
            except json.JSONDecodeError:
                log.warning("[antigravity] non-JSON line from agy: %r", s[:200])
                continue

            event = data.get("event")
            if event == "init":
                cid = data.get("conversation_id")
                if cid:
                    self.conversation_id = cid
            elif event == "result":
                cid = data.get("result", {}).get("conversation_id")
                if cid:
                    self.conversation_id = cid

            yield data

            if event == "result":
                break

    async def close(self):
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._stderr_task
        self._stderr_task = None

        if self._proc is not None:
            with suppress(Exception):
                self._proc.stdin.close()
                await self._proc.stdin.wait_closed()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                with suppress(Exception):
                    self._proc.kill()
                    await self._proc.wait()
            self._proc = None


class AntigravityAgentSkill(Skill):
    @bypass("agy_info", "Информация о сессии Antigravity", standalone=True)
    async def agy_info_command(self, args: str) -> str:
        backend = getattr(self, "backend", None)
        if not backend:
            return "Antigravity бэкенд не инициализирован."
        state = backend._load_state()
        cid = state.get("conversation_id", "отсутствует")
        live = "активен" if backend._client is not None and backend._client.is_alive else "не запущен"
        return f"🚀 Antigravity Backend (сессия: {cid}, процесс: {live})"


class AntigravityBackend(BaseBackend):
    def __init__(self, agent, sdk_options: dict | None = None):
        """sdk_options — словарь параметров бэкенда:
          - antigravity_bin: кастомный путь к agy.exe
          - tools / allowed_tools: список разрешённых нативных инструментов (по умолчанию [])
          - forbidden_tools: кортеж запрещённых нативных тулов (если не задан allowed_tools)
          - strip_native_instructions: вырезать нативные инструкции через override-промпт (True)
          - disable_slash_commands: передавать ли флаг --disable-slash-commands (True)
          - dangerously_skip_permissions: передавать ли --dangerously-skip-permissions (True)
          - effort: reasoning effort ('low' | 'medium' | 'high' | None)
          - model: имя модели для agy CLI
          - app_data_dir: каталог данных (~/.gemini/antigravity-cli)
        """
        super().__init__(agent)
        self._sdk_options = sdk_options or {}
        self._agy_path = _find_antigravity(self._sdk_options.get("antigravity_bin"))

        # Поддержка избирательного разрешения тулов (tools / allowed_tools)
        # по аналогии с claude backend (options_kwargs["tools"])
        allowed = self._sdk_options.get("allowed_tools")
        if allowed is None:
            allowed = self._sdk_options.get("tools")

        if allowed is not None:
            self._allowed_tools = tuple(allowed)
            allowed_set = set(allowed)
            self._forbidden_tools = tuple(t for t in _FORBIDDEN_NATIVE_TOOLS if t not in allowed_set)
        else:
            self._allowed_tools = ()
            self._forbidden_tools = tuple(self._sdk_options.get("forbidden_tools", _FORBIDDEN_NATIVE_TOOLS))

        self._strip_native_instructions = self._sdk_options.get("strip_native_instructions", True)

        # Регистрация сервисного скилла с командой /agy_info
        skill = AntigravityAgentSkill()
        skill.backend = self
        skill.register(self)
        agent.skills.insert(0, skill)

        if agent.memory.memory_dir:
            self._cwd = os.path.join(agent.memory.memory_dir, "workspace")
            os.makedirs(self._cwd, exist_ok=True)
        else:
            self._cwd = os.getcwd()

        self._client: AntigravityClient | None = None
        self._client_append: str | None = None
        self._client_skills_fp: str | None = None
        self._memory_state: dict = {}

        self._mcp_runner = None
        self._mcp_port: int | None = None
        self._mcp_server_name: str | None = None
        self._project_id: str = f"slonagent_{self.agent.id}"
        self._mcp_tools: list[dict] = []

        atexit.register(self._unregister_mcp_config)
        atexit.register(self._cleanup_project_config)

    def __del__(self):
        # Очищаем MCP и проект
        with suppress(BaseException):
            self._unregister_mcp_config()
            self._cleanup_project_config()

        # Эфемерный агент (без memory_dir) — чистим созданные файлы сессии в
        # ~/.gemini/antigravity-cli/, чтобы не захламлять диск между запусками
        with suppress(BaseException):
            if self._state_file is None and self._memory_state.get("conversation_id"):
                cid = self._memory_state["conversation_id"]
                self._cleanup_session_files(cid)

    def _cleanup_session_files(self, cid: str):
        """Удаляет sqlite db и brain logs для сессии cid."""
        app_data_dir = (
            self._sdk_options.get("app_data_dir")
            or os.environ.get("ANTIGRAVITY_APP_DATA_DIR")
            or os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-cli")
        )
        if not app_data_dir or not os.path.isdir(app_data_dir):
            return

        db_path = os.path.join(app_data_dir, "conversations", f"{cid}.db")
        for suffix in ("", "-shm", "-wal"):
            f = db_path + suffix
            if os.path.isfile(f):
                with suppress(OSError):
                    os.remove(f)

        brain_dir = os.path.join(app_data_dir, "brain", cid)
        if os.path.isdir(brain_dir):
            with suppress(OSError):
                shutil.rmtree(brain_dir)
        log.info("[antigravity] очищены файлы сессии %s", cid)

    def _mcp_config_path(self) -> str:
        return os.path.join(os.path.expanduser("~"), ".gemini", "config", "mcp_config.json")

    def _register_mcp_config(self):
        if not self._mcp_server_name or not self._mcp_port:
            return
        p = self._mcp_config_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        data = {}
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        servers = data.setdefault("mcpServers", {})
        bridge_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "antigravity_mcp.py")
        servers[self._mcp_server_name] = {
            "command": sys.executable,
            "args": [bridge_script, "--port", str(self._mcp_port)],
        }
        tmp = f"{p}.tmp.{os.getpid()}.{uuid.uuid4().hex[:6]}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        shutil.move(tmp, p)

    def _unregister_mcp_config(self):
        if not self._mcp_server_name:
            return
        p = self._mcp_config_path()
        if os.path.isfile(p):
            try:
                open_func = _builtin_open if "_builtin_open" in globals() and _builtin_open is not None else open
                with open_func(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                servers = data.get("mcpServers", {})
                if self._mcp_server_name in servers:
                    del servers[self._mcp_server_name]
                    tmp = f"{p}.tmp.{os.getpid()}.{uuid.uuid4().hex[:6]}"
                    with open_func(tmp, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)
                    shutil.move(tmp, p)
            except (NameError, TypeError):
                pass
            except Exception as e:
                log.warning("[antigravity] не удалось удалить MCP конфигурацию: %s", e)

        schema_dir = os.path.join(
            os.path.expanduser("~"), ".gemini", "antigravity-cli", "mcp", self._mcp_server_name
        )
        shutil.rmtree(schema_dir, ignore_errors=True)

    def _project_config_path(self) -> str:
        proj_id = self._project_id or f"slonagent_{self.agent.id}"
        return os.path.join(os.path.expanduser("~"), ".gemini", "config", "projects", f"{proj_id}.json")

    def _ensure_project_config(self):
        self._project_id = self._project_id or f"slonagent_{self.agent.id}"
        p = self._project_config_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        data = {
            "id": self._project_id,
            "name": f"SlonAgent Project {self._project_id}",
            "projectResources": {},
        }
        tmp = f"{p}.tmp.{os.getpid()}.{uuid.uuid4().hex[:6]}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        shutil.move(tmp, p)

    def _cleanup_project_config(self):
        if self._project_id:
            p = self._project_config_path()
            with suppress(OSError):
                if os.path.isfile(p):
                    os.remove(p)

    async def _ensure_mcp_bridge(self):
        """Поднимает локальный HTTP loopback сервер для скиллов Слона и регистрирует MCP сервер."""
        tools_decl = []
        for skill in self.agent.skills:
            for decl in skill.get_tools():
                fn = decl.get("function") or decl
                tools_decl.append({
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "inputSchema": fn.get("parameters") or {"type": "object", "properties": {}},
                })

        self._mcp_tools = tools_decl

        # Если тулов нет — не регистрируем MCP bridge
        if not tools_decl:
            if self._mcp_runner is not None:
                with suppress(Exception):
                    await self._mcp_runner.cleanup()
                self._mcp_runner = None
                self._mcp_port = None
                self._unregister_mcp_config()
            self._ensure_project_config()
            return

        # Если раннер уже работает — обеспечиваем конфиг проекта и выходим
        if self._mcp_runner is not None:
            self._ensure_project_config()
            return

        from aiohttp import web

        app = web.Application()

        async def get_tools(request):
            return web.json_response({"tools": self._mcp_tools})

        async def call_tool(request):
            data = await request.json()
            name = data.get("name")
            args = data.get("arguments") or {}
            fake_turn = {
                "tool_calls": [{
                    "id": f"mcp_{name}_{uuid.uuid4().hex[:8]}",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args),
                    },
                }],
            }
            task = asyncio.create_task(
                self.agent.dispatch_tool_calls(fake_turn, emit_transport_events=False)
            )
            try:
                tool_turns = await asyncio.shield(task)
            except asyncio.CancelledError:
                task.cancel()
                raise

            result_text = ""
            for t in tool_turns:
                c = t.get("content")
                if isinstance(c, str):
                    result_text += c
                elif isinstance(c, list):
                    for p in c:
                        if isinstance(p, dict) and p.get("text"):
                            result_text += p["text"]
                elif c is not None:
                    result_text += json.dumps(c, ensure_ascii=False)
            return web.json_response({"result": result_text, "turns": tool_turns})

        app.router.add_get("/tools", get_tools)
        app.router.add_post("/call", call_tool)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self._mcp_runner = runner
        self._mcp_port = site._server.sockets[0].getsockname()[1]
        self._mcp_server_name = f"slon_{self.agent.id}"

        self._register_mcp_config()
        self._ensure_project_config()

    @property
    def _state_file(self) -> str | None:
        if not self.agent.memory.memory_dir:
            return None
        tid = self.agent.thread_id
        fname = f"ANTIGRAVITY_{tid}.json" if tid else "ANTIGRAVITY.json"
        return os.path.join(self.agent.memory.memory_dir, fname)

    def _load_state(self) -> dict:
        if self._state_file is None:
            return dict(self._memory_state)
        try:
            with open(self._state_file, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_state(self, state: dict):
        if self._state_file is None:
            self._memory_state = dict(state)
            return
        with open(self._state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    def _skills_fingerprint(self) -> str:
        """Хеш набора тулов скиллов Слона.
        Используется для перезапуска клиента при изменении списка скиллов.
        """
        items = []
        for skill in self.agent.skills:
            for decl in skill.get_tools():
                fn = decl.get("function") or decl
                items.append((
                    fn["name"],
                    fn.get("description", ""),
                    json.dumps(fn.get("parameters") or {}, sort_keys=True),
                ))
        items.sort()
        return hashlib.sha256(repr(items).encode()).hexdigest()

    def _build_tools(self) -> list:
        """Оборачивает тулы скиллов Слона в callable объекты со схемой (для тестов и выполнения)."""
        tools = []
        for skill in self.agent.skills:
            for decl in skill.get_tools():
                fn = decl.get("function") or decl
                name = fn["name"]
                schema = fn.get("parameters") or {"type": "object", "properties": {}}
                desc = fn.get("description", "")

                async def handler(_name=name, **args):
                    fake_turn = {
                        "tool_calls": [{
                            "id": f"call_{_name}_{uuid.uuid4().hex[:8]}",
                            "function": {
                                "name": _name,
                                "arguments": json.dumps(args, ensure_ascii=False),
                            },
                        }],
                    }
                    task = asyncio.create_task(
                        self.agent.dispatch_tool_calls(fake_turn, emit_transport_events=False)
                    )
                    try:
                        tool_turns = await asyncio.shield(task)
                    except asyncio.CancelledError:
                        task.cancel()
                        raise

                    results = []
                    for t in tool_turns:
                        c = t.get("content")
                        if c is not None:
                            results.append(c)
                    if len(results) == 1:
                        res = results[0]
                        return res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
                    return json.dumps(results, ensure_ascii=False) if results else ""

                handler.__name__ = name
                handler.__doc__ = desc
                handler.input_schema = schema
                tools.append(handler)
        return tools

    def _format_skills_for_prompt(self) -> str:
        """Описывает тулы скиллов Слона для системного промпта."""
        tools = []
        for skill in self.agent.skills:
            for decl in skill.get_tools():
                fn = decl.get("function") or decl
                tools.append(fn)
        if not tools:
            return ""
        server_name = self._mcp_server_name or "slon"
        lines = [f"Available SlonAgent Tools: (accessible via call_mcp_tool with ServerName='{server_name}')"]
        for fn in tools:
            name = fn["name"]
            desc = fn.get("description", "")
            params = json.dumps(fn.get("parameters") or {}, ensure_ascii=False)
            lines.append(f"- Tool `{name}`: {desc} (parameters: {params})")
        lines.append(f"CRITICAL: When the user requests a tool or calculation, you MUST call call_mcp_tool(ServerName='{server_name}', ToolName=..., Arguments=...). Never calculate or answer in your head without calling the tool.")
        return "\n".join(lines)

    @staticmethod
    def _extract_user_text(pending: list) -> str:
        """Извлекает текст из списка user-турнов."""
        parts = []
        for t in pending:
            content = t.get("content")
            if isinstance(content, str):
                if content:
                    parts.append(content)
                continue
            for b in content or ():
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text"):
                    parts.append(b["text"])
        if not parts:
            raise RuntimeError("AntigravityBackend: нет текста в user-турнах")
        return "\n\n".join(parts)

    def _build_prompt_payload(self, append_text: str, user_text: str) -> str:
        """Формирует итоговый prompt envelope для agy.exe."""
        parts = []
        if self._strip_native_instructions:
            parts.append(_build_base_instructions(self._forbidden_tools, self._allowed_tools))
        if append_text:
            parts.append(f"[System Instructions]\n{append_text}")
        skills_prompt = self._format_skills_for_prompt()
        if skills_prompt:
            parts.append(skills_prompt)
        parts.append(f"[User Request]\n{user_text}")
        return "\n\n".join(parts)

    # — Context Synchronization & Divergence Detection —————————————

    @staticmethod
    def _stable_memory_turns(turns: list) -> list:
        """Хвост user-турнов в конце памяти — это «pending», ещё не ушедший в модель.
        Их при сверке с историей диалога исключаем.
        """
        end = len(turns)
        for i in range(len(turns) - 1, -1, -1):
            t = turns[i]
            if isinstance(t, dict) and t.get("role") == "user":
                end = i
            else:
                break
        return turns[:end]

    @classmethod
    def _clean_user_text(cls, text: str) -> str:
        """Очищает user-текст от временного штампа [YYYY-MM-DDTHH:MM:SS] и лишних пробелов."""
        return re.sub(
            r"^\[\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?\]\s*",
            "",
            text.strip(),
        ).strip()

    @classmethod
    def _memory_signatures(cls, turns: list) -> set[tuple]:
        """Сигнатуры ходов, ожидаемые в истории диалога на основе памяти Слона."""
        sigs = set()
        for t in turns or ():
            if not isinstance(t, dict):
                continue
            role = t.get("role")
            if role == "assistant" and t.get("tool_calls"):
                for tc in t["tool_calls"]:
                    fn = tc.get("function") or {}
                    name = fn.get("name") or tc.get("name")
                    if name:
                        sigs.add(("tool_call", name))
                continue
            content = t.get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        text += p.get("text", "")
            if role == "user":
                text = cls._clean_user_text(text)
            else:
                text = text.strip()
            text = text[:150].strip()
            if role == "user" and text:
                sigs.add(("message", "user", text))
            elif role == "assistant" and text:
                if text.startswith("[ответ прерван") or "[прервано пользователем]" in text:
                    continue
                sigs.add(("message", "assistant", text))
            elif role == "tool":
                if t.get("content") == "[прервано пользователем]":
                    continue
                cid = t.get("tool_call_id") or t.get("name") or "tool"
                sigs.add(("tool_output", cid))
        return sigs

    def _transcript_path(self, conversation_id: str) -> str | None:
        """Находит путь к transcript.jsonl сессии."""
        app_data_dir = (
            self._sdk_options.get("app_data_dir")
            or os.environ.get("ANTIGRAVITY_APP_DATA_DIR")
            or os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-cli")
        )
        p1 = os.path.join(app_data_dir, "brain", conversation_id, ".system_generated", "logs", "transcript.jsonl")
        if os.path.isfile(p1):
            return p1
        save_dir = self._sdk_options.get("save_dir")
        if save_dir:
            p2 = os.path.join(save_dir, "brain", conversation_id, ".system_generated", "logs", "transcript.jsonl")
            if os.path.isfile(p2):
                return p2
            p3 = os.path.join(save_dir, conversation_id, ".system_generated", "logs", "transcript.jsonl")
            if os.path.isfile(p3):
                return p3
        return None

    @classmethod
    def _transcript_signatures(cls, path: str, ignored_tools: tuple[str, ...] | set[str] | None = None) -> set[tuple]:
        """Извлекает сигнатуры ходов из transcript.jsonl.
        ignored_tools — тулы, намеренно подавленные бэкендом (не вызывают расхождение памяти).
        """
        sigs = set()
        ignored = set(ignored_tools or ())
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    stype = entry.get("type")
                    source = entry.get("source")
                    if stype == "USER_INPUT" or source in ("USER_EXPLICIT", "USER"):
                        content = entry.get("content") or ""
                        m = re.search(r"\[User Request\]\s*(.*?)(?:</USER_REQUEST>|\Z)", content, re.DOTALL)
                        if m:
                            user_text = m.group(1).strip()
                        elif "<USER_REQUEST>" in content:
                            m2 = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", content, re.DOTALL)
                            user_text = m2.group(1).strip() if m2 else content
                        else:
                            user_text = content
                        cleaned = cls._clean_user_text(user_text)[:150].strip()
                        if cleaned:
                            sigs.add(("message", "user", cleaned))
                    elif stype == "PLANNER_RESPONSE":
                        for tc in entry.get("tool_calls") or ():
                            name = tc.get("name")
                            if name == "call_mcp_tool":
                                args = _parse_mcp_args(tc.get("args") or {})
                                tool_name = _clean_mcp_param(args.get("ToolName") or "")
                                if tool_name:
                                    sigs.add(("tool_call", tool_name))
                            elif name:
                                # Игнорируем внутренний просмотр MCP схем
                                if name == "view_file":
                                    args = tc.get("args") or {}
                                    fpath = str(args.get("AbsolutePath", "")).lower()
                                    if "mcp" in fpath:
                                        continue
                                # Игнорируем намеренно подавленные бэкендом инструменты
                                if name in ignored:
                                    continue
                                sigs.add(("tool_call", name))
                        content = entry.get("content") or ""
                        text = content[:150].strip()
                        if text:
                            sigs.add(("message", "assistant", text))
                    elif stype == "TOOL_CALL":
                        step_id = entry.get("id") or "tool"
                        sigs.add(("tool_output", step_id))
        except Exception as e:
            log.warning("[antigravity] ошибка чтения transcript %s: %s", path, e)
        return sigs

    def _agy_signatures(self, conversation_id: str | None = None) -> set[tuple]:
        """Сигнатуры из transcript.jsonl сохранённой сессии."""
        sigs = set()
        cid = (
            conversation_id
            or (self._client.conversation_id if self._client else None)
            or (self._load_state().get("conversation_id"))
        )
        if cid:
            transcript_path = self._transcript_path(cid)
            if transcript_path and os.path.isfile(transcript_path):
                return self._transcript_signatures(transcript_path, ignored_tools=self._forbidden_tools)
        return sigs

    async def _conversation_has_diverged(self, conversation_id: str | None = None) -> bool:
        """True если стабильная часть памяти Слона и история диалога Antigravity разошлись."""
        actual = self._agy_signatures(conversation_id)
        if not actual:
            return False

        stable = self._stable_memory_turns(self.agent.memory._turns)
        formatted = self.agent.strip_contents_private(stable)
        expected = self._memory_signatures(formatted)

        if not expected and not actual:
            return False

        if expected == actual:
            return False

        only_in_agy = actual - expected
        only_in_memory = expected - actual

        if not only_in_agy and not only_in_memory:
            return False

        # Если в истории Antigravity есть ходы, которых больше нет в памяти (LogCompressor сжал старые ходы):
        if only_in_agy:
            log.warning(
                "[antigravity] обнаружено расхождение контекста: %d ходов в Antigravity отсутствуют в памяти (сжатие памяти)",
                len(only_in_agy),
            )
            for sig in list(only_in_agy)[:3]:
                log.warning("[antigravity]   только в Antigravity: %r", sig)
            return True

        # Если в памяти есть ходы, но ни один не совпадает с Antigravity (внешняя замена памяти / форк):
        if only_in_memory and len(actual) > 0 and not (expected & actual):
            log.warning(
                "[antigravity] обнаружено расхождение: память полностью не совпадает с историей Antigravity",
            )
            return True

        return False

    @staticmethod
    def _format_turns_for_context(turns: list) -> str:
        """Форматирует недавние турны памяти в текстовый контекст для новой сессии."""
        lines = []
        for t in turns:
            if not isinstance(t, dict):
                continue
            role = t.get("role")
            content = t.get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        text += p.get("text", "")
            if role == "user" and text:
                lines.append(f"User: {text.strip()}")
            elif role == "assistant":
                if text:
                    lines.append(f"Assistant: {text.strip()}")
                for tc in t.get("tool_calls") or ():
                    fn = tc.get("function") or {}
                    name = fn.get("name") or tc.get("name")
                    args = fn.get("arguments") or ""
                    lines.append(f"Assistant [tool call {name}]: {args}")
            elif role == "tool":
                name = t.get("name") or "tool"
                lines.append(f"Tool Result [{name}]: {text.strip()}")
        return "\n".join(lines)

    async def _create_client(
        self,
        conversation_id: str | None,
        append_text: str,
        skills_fp: str,
        prompt_fp: str,
        resume: bool = False,
    ):
        """Создаёт инстанс AntigravityClient."""
        model_name = self.agent.model_name or self._sdk_options.get("model") or "gemini-3.8-flash-high"
        effort = self._sdk_options.get("effort")
        if not effort:
            for suf in ("-high", "-medium", "-low"):
                if model_name.endswith(suf):
                    break
            else:
                if any(k in model_name for k in ("gemini", "flash", "pro")):
                    effort = "high"
        disable_slash = self._sdk_options.get("disable_slash_commands", True)
        danger_perms = self._sdk_options.get("dangerously_skip_permissions", True)
        extra_args = self._sdk_options.get("extra_args")

        self._client = AntigravityClient(
            agy_path=self._agy_path,
            model=model_name,
            conversation_id=conversation_id if resume else None,
            cwd=self._cwd,
            effort=effort,
            disable_slash_commands=disable_slash,
            dangerously_skip_permissions=danger_perms,
            project_id=self._project_id,
            extra_args=extra_args,
        )
        self._client_append = append_text
        self._client_skills_fp = skills_fp

        actual_cid = getattr(self._client, "conversation_id", None) or conversation_id or str(uuid.uuid4())
        self._save_state({
            "conversation_id": actual_cid,
            "skills_fp": skills_fp,
            "prompt_fp": prompt_fp,
            "created": True,
        })

    async def _ensure_session(self, append_text: str, skills_fp: str, stable_turns: list) -> str:
        """Гарантирует валидную сессию Antigravity, синхронизированную с памятью Слона."""
        prompt_fp = hashlib.sha256(append_text.encode("utf-8")).hexdigest()
        state = self._load_state()
        saved_cid = state.get("conversation_id")
        saved_skills_fp = state.get("skills_fp")

        # 1. Если клиент активен и жив:
        if self._client is not None and self._client.is_alive:
            # Проверяем, изменился ли набор тулов скиллов
            if self._client_skills_fp != skills_fp:
                log.info("[antigravity] набор скиллов изменился, перезапускаем клиента с сессией %s", saved_cid)
                with suppress(Exception, asyncio.CancelledError):
                    await self._client.close()
                self._client = None
                self._client_append = None
                self._client_skills_fp = None
                await self._create_client(
                    conversation_id=saved_cid,
                    append_text=append_text,
                    skills_fp=skills_fp,
                    prompt_fp=prompt_fp,
                    resume=True,
                )
                return saved_cid or ""

            # Проверяем реальное расхождение памяти (например, LogCompressor только что сжал старые ходы)
            if await self._conversation_has_diverged(saved_cid):
                log.info("[antigravity] память разошлась в активной сессии, пересоздаём сессию")
                with suppress(Exception, asyncio.CancelledError):
                    await self._client.close()
                self._client = None
                self._client_append = None
                self._client_skills_fp = None
            else:
                # Нормальный multi-turn: продолжаем в том же живом процессе
                self._client_append = append_text
                return self._client.conversation_id or saved_cid or ""

        # 2. Клиент не запущен (первый запуск, рестарт процесса или память разошлась)
        need_fresh = False
        diverged = False
        if saved_cid:
            diverged = await self._conversation_has_diverged(saved_cid)
            if diverged:
                need_fresh = True
            else:
                # Пытаемся возобновить сохранённую сессию
                try:
                    await self._create_client(
                        conversation_id=saved_cid,
                        append_text=append_text,
                        skills_fp=skills_fp,
                        prompt_fp=prompt_fp,
                        resume=True,
                    )
                    log.info("[antigravity] успешно возобновлена сессия %s", saved_cid)
                    return saved_cid
                except Exception as e:
                    log.warning(
                        "[antigravity] возобновление сессии %s не удалось (%s: %s), начинаем свежую",
                        saved_cid, type(e).__name__, e,
                    )
                    need_fresh = True
        else:
            need_fresh = True

        # 3. Запуск свежей сессии (первый запуск или компрессия памяти)
        reasons = []
        if diverged:
            reasons.append("память разошлась (сжатие/правка истории)")
        elif not saved_cid:
            reasons.append("первый запуск")
        else:
            reasons.append("перезапуск сессии")
        reason = ", ".join(reasons)

        log.info("[antigravity] запуск свежей сессии (причина: %s)", reason)

        if self._state_file is None and saved_cid:
            self._cleanup_session_files(saved_cid)

        effective_append = append_text
        if stable_turns:
            history_text = self._format_turns_for_context(self.agent.strip_contents_private(stable_turns))
            if history_text:
                effective_append += (
                    f"\n\nContext of preceding turns in this conversation:\n"
                    f"<recent_conversation>\n{history_text}\n</recent_conversation>"
                )

        new_cid = str(uuid.uuid4())
        await self._create_client(
            conversation_id=new_cid,
            append_text=effective_append,
            skills_fp=skills_fp,
            prompt_fp=prompt_fp,
            resume=False,
        )

        # Оповещаем транспорт только если сессия пересоздана из-за сжатия истории
        if diverged:
            with suppress(Exception):
                await self.agent.transport.send_memory_info(
                    f"Синхронизировал antigravity-сессию: {reason}, создана новая сессия"
                )

        return self._client.conversation_id or new_cid

    async def close(self):
        """Закрывает клиент/подпроцесс Antigravity и освобождает ресурсы MCP и проекта."""
        if self._client:
            with suppress(Exception, asyncio.CancelledError):
                await self._client.close()
            self._client = None
            self._client_append = None
            self._client_skills_fp = None

        if self._mcp_runner:
            with suppress(Exception):
                await self._mcp_runner.cleanup()
            self._mcp_runner = None
            self._mcp_port = None

        self._unregister_mcp_config()
        self._cleanup_project_config()

    async def llm(self, tool_choice: str = None, parallel_tool_calls: bool = None,
                  temperature: float = 1.0, max_tokens: int | None = None,
                  system_prompt: str | None = None):
        """Запускает шаг генерации через Google Antigravity CLI.
        Стримит токены, мысли и вызовы инструментов в transport Слона.
        Возвращает список ходов (list[dict]) для сохранения в памяти.
        """
        agent = self.agent
        user_text = agent.memory.last_user_query()

        # 1. Запуск компрессии памяти
        contents = await agent.memory.get_contents()

        # 2. Выделяем pending user-турны в хвосте памяти
        pending = []
        for t in reversed(contents):
            if not isinstance(t, dict) or t.get("role") != "user":
                break
            pending.insert(0, t)
        pending = self.agent.strip_contents_private(pending)
        if not pending:
            raise RuntimeError("AntigravityBackend.llm(): нет user-турнов в хвосте памяти — нечего слать")

        # 3. Собираем системный промпт из скиллов + system_prompt
        parts = []
        for skill in agent.skills:
            ctx = await skill.get_context_prompt(user_text)
            if ctx:
                parts.append(ctx)
        if system_prompt:
            parts.append(system_prompt)
        append_text = "\n\n".join(p for p in parts if p) or "You are a helpful assistant."

        skills_fp = self._skills_fingerprint()
        stable_turns = self._stable_memory_turns(self.agent.memory._turns)

        # Обеспечиваем MCP bridge для скиллов с тулами
        await self._ensure_mcp_bridge()

        # 4. Проверка и синхронизация сессии Antigravity
        await self._ensure_session(append_text, skills_fp, stable_turns)

        # 5. Формирование пользовательского запроса и промпта
        query_text = self._extract_user_text(pending)
        prompt_payload = self._build_prompt_payload(append_text, query_text)
        log.info("[antigravity] запрос: %r (%d pending turns)", query_text[:80], len(pending))

        text_buf = ""
        text_stream_id = None
        thinking_buf = ""
        thinking_stream_id = None
        turns: list[dict] = []
        tool_use_names: dict[str, str] = {}

        try:
            async for data in self._client.chat(prompt_payload):
                event = data.get("event")

                if event == "step_update":
                    su = data.get("step_update", {})
                    stype = su.get("step_type")

                    # А. Рассуждения модели (thinking)
                    if "thinking_delta" in su:
                        if thinking_stream_id is None:
                            thinking_stream_id = _next_stream_id()
                        thinking_buf += su["thinking_delta"]
                        await agent.transport.send_thinking(
                            thinking_buf, stream_id=thinking_stream_id, final=False
                        )

                    # Б. Текст ответа модели
                    if "text_delta" in su:
                        if text_stream_id is None:
                            text_stream_id = _next_stream_id()
                        text_buf += su["text_delta"]
                        await agent.transport.send_message(
                            text_buf, stream_id=text_stream_id, final=False
                        )

                    # В. Вызовы инструментов
                    if stype == "tool":
                        tool_name = su.get("tool_name", "")
                        tool_info = su.get("tool_info", {})
                        state = su.get("state")
                        call_id = str(su.get("step_index") or f"call_{tool_name}_{uuid.uuid4().hex[:8]}")

                        # Обработка вызова инструмента через MCP
                        if tool_name == "call_mcp_tool":
                            params = tool_info.get("parameters") or {}
                            actual_tool = _clean_mcp_param(params.get("ToolName") or "tool")
                            actual_args = _parse_mcp_args(params.get("Arguments"))

                            if state == "ACTIVE":
                                tool_use_names[call_id] = actual_tool
                                await agent.transport.on_tool_call(actual_tool, actual_args)
                                turns.append({
                                    "role": "assistant",
                                    "tool_calls": [{
                                        "id": call_id,
                                        "type": "function",
                                        "function": {
                                            "name": actual_tool,
                                            "arguments": json.dumps(actual_args, ensure_ascii=False),
                                        },
                                    }],
                                })
                            elif state == "DONE":
                                resolved_name = tool_use_names.get(call_id, actual_tool)
                                out = tool_info.get("output", "")
                                content_str = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
                                await agent.transport.on_tool_result(resolved_name, out)
                                turns.append({
                                    "role": "tool",
                                    "tool_call_id": call_id,
                                    "name": resolved_name,
                                    "content": content_str,
                                })
                            continue

                        # Подавляем только внутренний просмотр схем MCP
                        if tool_name == "view_file":
                            fpath = str((tool_info.get("parameters") or {}).get("AbsolutePath", ""))
                            if "mcp" in fpath.lower():
                                log.debug("[antigravity] пропущен внутренний просмотр MCP схемы: %s", fpath)
                                continue

                        # Подавляем нативные тулы, которые запрещены политикой (не входят в allowed_tools)
                        if tool_name in self._forbidden_tools:
                            log.debug("[antigravity] подавлен запрещённый нативный тул: %s", tool_name)
                            continue

                        if state == "ACTIVE":
                            params = tool_info.get("parameters") or {}
                            tool_use_names[call_id] = tool_name
                            await agent.transport.on_tool_call(tool_name, params)
                            turns.append({
                                "role": "assistant",
                                "tool_calls": [{
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": tool_name,
                                        "arguments": json.dumps(params, ensure_ascii=False) if isinstance(params, dict) else str(params),
                                    },
                                }],
                            })
                        elif state == "DONE":
                            resolved_name = tool_use_names.get(call_id, tool_name)
                            out = tool_info.get("output", "")
                            content_str = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
                            await agent.transport.on_tool_result(resolved_name, out)
                            turns.append({
                                "role": "tool",
                                "tool_call_id": call_id,
                                "name": resolved_name,
                                "content": content_str,
                            })

                elif event == "result":
                    res = data.get("result", {})
                    resp_text = res.get("response", "")
                    if not text_buf and resp_text:
                        text_buf = resp_text

                    # Финализируем стримы в транспорт
                    if text_stream_id is not None or text_buf:
                        await agent.transport.send_message(
                            text_buf,
                            stream_id=text_stream_id or _next_stream_id(),
                            final=True,
                        )
                    if thinking_stream_id is not None:
                        await agent.transport.send_thinking(
                            thinking_buf,
                            stream_id=thinking_stream_id,
                            final=True,
                        )

                    if text_buf:
                        turns.append({
                            "role": "assistant",
                            "content": text_buf,
                        })

                    # Обновляем сохранённый conversation_id
                    active_cid = res.get("conversation_id") or getattr(self._client, "conversation_id", None)
                    if active_cid:
                        state = self._load_state()
                        state["conversation_id"] = active_cid
                        self._save_state(state)

            log.info("[antigravity] готово: %d turns сформировано", len(turns))
            return turns

        except asyncio.CancelledError:
            log.warning("[antigravity] выполнение прервано пользователем (stop)")
            cur_task = asyncio.current_task()
            if cur_task is not None and hasattr(cur_task, "uncancel"):
                with suppress(Exception):
                    cur_task.uncancel()

            # 1. Немедленно завершаем подпроцесс agy.exe
            await self.close()

            # 2. Сбрасываем статус обработки и отправляем уведомление в транспорт
            with suppress(Exception):
                await agent.transport.send_processing(False)
            with suppress(Exception):
                await agent.transport.send_message("⚠️ Ответ прерван пользователем.")

            # 3. Закрываем незавершённые tool_calls синтетическими ответами
            seen = {t.get("tool_call_id") for t in turns if t.get("role") == "tool"}
            for t in list(turns):
                for tc in t.get("tool_calls") or ():
                    if tc["id"] not in seen:
                        turns.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "name": tc["function"]["name"],
                            "content": "[прервано пользователем]",
                        })
                        seen.add(tc["id"])
            turns.append({"role": "assistant", "content": "[ответ прерван пользователем]"})
            return turns
