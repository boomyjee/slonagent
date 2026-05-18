"""CodexBackend — Agent с llm() через `codex app-server` (OpenAI Codex CLI).

JSON-RPC поверх stdio: handshake (initialize/initialized) → thread/start с
dynamicTools из всех скиллов агента → turn/start → стримим item-нотификации.
Когда codex зовёт тул — приходит server-initiated request `item/tool/call`,
мы диспатчим через agent.dispatch_tool_calls и отвечаем contentItems.

Авторизация через подписку ChatGPT: юзер один раз делает `codex login`,
backend требует наличия `~/.codex/auth.json` — иначе ругается.

ARCHITECTURE NOTE: codex app-server multi-thread by design (один процесс
обслуживает много thread'ов параллельно). Поэтому процесс — это
process-singleton `CodexClient`, а `CodexBackend` per-Agent держит только
thread-specific state и регистрируется в client'е по своему thread_id.
"""
import asyncio
import hashlib
import json
import logging
import os
import time
from contextlib import suppress

from src.agent.backends.base import BaseBackend

log = logging.getLogger(__name__)


# Базовый stream_id для transport.send_message — миллисекунды от запуска
# процесса. Гарантирует что после restart'а slonagent'а новые stream_id'ы
# всегда строго больше старых (UI хранит сообщения с прошлой сессии под их
# stream_id'ами, и если мы стартанём с 0 — будем редактировать ИХ вместо
# создания новых). Per-process монотонный счётчик поверх.
_STREAM_ID_BASE: int = int(time.time() * 1000)
_stream_id_counter: int = 0


def _next_stream_id() -> int:
    """Process-wide монотонный stream_id. После рестарта база сдвигается
    вперёд (time.time() растёт), коллизий с прошлой сессией не будет."""
    global _stream_id_counter
    _stream_id_counter += 1
    return _STREAM_ID_BASE + _stream_id_counter


# Нативные codex-тулы, которые модель видит поверх наших dynamicTools.
# Их нужно явно отрубить — иначе модель может выйти за наш SandboxSkill
# (запустить shell на хосте, открыть URL, генерить картинки помимо
# NanoBananaSkill и т.п.). Список взят из `codex features list`.
# Нативные codex-тулы, которые отключаются через `--disable <feature>` CLI флаг.
# Список взят из `codex features list` (только stable=true + actually-tool-эмиттящие).
_DISABLED_NATIVE_FEATURES = (
    "shell_tool",                    # exec на хосте — минуя SandboxSkill
    "apply_patch_freeform",          # правка файлов (под-флаг apply_patch)
    "apply_patch_streaming_events",  # стрим-правка файлов
    "browser_use",                   # автоматизация браузера
    "browser_use_external",          # внешний браузер
    "in_app_browser",                # встроенный браузер
    "computer_use",                  # управление OS
    "image_generation",              # генерация картинок (у нас NanoBananaSkill)
    "multi_agent",                   # codex'овый внутренний multi-agent
    "tool_search",                   # автопоиск тулов
    "tool_suggest",                  # автоподсказка тулов
    "builtin_mcp",                   # codex'овая встроенная MCP
    "enable_mcp_apps",               # MCP apps
)

# Top-level config keys которые передаём через `-c key=value`.
# Имена взяты из исходников codex (lib/codex/codex-rs/config/src/
# config_toml.rs + core/src/session/mod.rs):
#   - web_search="disabled" → убирает web.run И multi_tool_use.parallel
#   - include_permissions_instructions=false → нет <permissions instructions>
#     блока ("sandbox_mode is..." болтовня которая пугает модель)
#   - include_apps_instructions=false → нет <apps_instructions> блока
#   - include_environment_context=false → нет <environment_context> envelope
#
# В 0.130.0 НЕ поддерживаются (появятся в alpha):
#   - include_skill_instructions → <skills_instructions> блок про codex'овские
#     встроенные skills останется (не страшный)
#   - include_collaboration_mode_instructions
_DISABLED_CONFIG_KEYS: tuple[tuple[str, str], ...] = (
    ('web_search', '"disabled"'),
    ('include_permissions_instructions', 'false'),
    ('include_apps_instructions', 'false'),
    ('include_environment_context', 'false'),
)

# Default sandbox = "danger-full-access" + approval_policy = "untrusted".
# Эта комбинация даёт лучшее поведение:
#   1. Модель в системках видит "No filesystem sandboxing" (не страшное
#      "read-only" которое пугает её отказываться даже от наших
#      dynamicTools).
#   2. apply_patch — единственный baked-in writer без --disable флага —
#      триггерит approval-request под `untrusted` режимом. Мы declining →
#      patch блокируется.
# С combo `workspace-write + on-request` codex автоматически downgrade'ит
# до effective read-only (safety fallback) и снова показывает "read-only"
# текст в permissions блоке.
_THREAD_SANDBOX_DEFAULT = "danger-full-access"
_THREAD_APPROVAL_POLICY_DEFAULT = "untrusted"

# Если baseInstructions не задавать вообще — codex вкидывает свою длинную
# системку про "You are Codex, a coding agent based on GPT-5...". Если
# задать любой непустой текст — codex полностью заменяет на наш и
# дополнительно НЕ добавляет блоки <permissions instructions>,
# <skills_instructions>, <environment_context>. Минимальный непустой
# placeholder ниже даёт чистого агента, которым управляет developerInstructions
# (skill-контексты + system_prompt).
_BASE_INSTRUCTIONS_DEFAULT = (
    "You are an AI assistant. Use the provided tools to help the user."
)

# Reasoning settings — codex эмитит мысли модели через item/reasoning/*
# нотификации только при заданных effort/summary в turn/start. По probe'у
# на free ChatGPT (gpt-5.5):
#   - effort=None / summary=None → 0 reasoning событий вообще
#   - effort=high + summary=auto/detailed → приходит summaryTextDelta (summary
#     мыслей); raw textDelta НЕ приходит на этом плане
# Поэтому default'им на effort=high + summary=auto → юзер видит мысли как
# минимум в summary-форме. Можно override через backend_params.
_REASONING_EFFORT_DEFAULT = "high"
_REASONING_SUMMARY_DEFAULT = "auto"


def _find_codex() -> str:
    """Найти codex-бинарь. На Windows npm ставит как .cmd shim, на posix —
    обычный исполняемый. Override через env var CODEX_PATH.
    """
    override = os.environ.get("CODEX_PATH")
    if override:
        return override
    if os.name == "nt":
        candidate = os.path.expandvars(r"%APPDATA%\npm\codex.cmd")
        if os.path.exists(candidate):
            return candidate
    return "codex"


# ═══════════════════════════════════════════════════════════════════════════════
# CodexClient — process-singleton поверх одного `codex app-server` subprocess'а
# ═══════════════════════════════════════════════════════════════════════════════

class CodexClient:
    """Shared JSON-RPC клиент к `codex app-server` процессу.

    Pool keyed by enable_features tuple — backend'ы с одинаковым набором
    включённых нативных тулов шарят процесс (типичный случай: все backend'ы
    с дефолтным «всё отрублено» → один client), backend'ы с разными
    наборами получают каждый свой client/subprocess.

    Сам codex server multi-thread by design — каждый CodexBackend
    регистрирует свой thread_id в `_handlers`, роутер кидает нотификации/
    server-request'ы тому backend'у, чей threadId указан в `params`.
    """

    _instances: dict[tuple, "CodexClient"] = {}
    _pool_lock: asyncio.Lock | None = None

    @classmethod
    async def get(cls, enable_features: tuple | None = None) -> "CodexClient":
        """Возвращает готовый к работе client (subprocess поднят, handshake
        и account/read сделаны). Если client с таким enable_features уже
        запущен — переиспользует; иначе создаёт новый.

        enable_features — какие нативные тулы codex'а оставить включёнными
        (по умолчанию `_DISABLED_NATIVE_FEATURES` всё опасное гасит через
        --disable). Tuple для стабильного hash-ключа.
        """
        if cls._pool_lock is None:
            cls._pool_lock = asyncio.Lock()
        key = tuple(sorted(set(enable_features or ())))
        async with cls._pool_lock:
            if key not in cls._instances:
                inst = cls(enable_features=key)
                try:
                    await inst._start()
                except Exception:
                    await inst._shutdown()
                    raise
                cls._instances[key] = inst
            return cls._instances[key]

    @classmethod
    async def reset(cls):
        """Только для тестов: грохнуть все clients в pool'е."""
        instances = list(cls._instances.values())
        cls._instances.clear()
        for inst in instances:
            await inst._shutdown()

    def __init__(self, enable_features: tuple = ()):
        self._codex_path = _find_codex()
        self._enable_features = enable_features
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._router_task: asyncio.Task | None = None

        self._next_id = 1000
        self._pending: dict[int, asyncio.Future] = {}
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._write_lock = asyncio.Lock()

        # thread_id → backend instance. Backend регистрируется в _ensure_thread()
        # и снимается в close(). Роутер по нему диспатчит нотификации.
        self._handlers: dict[str, "CodexBackend"] = {}

    # — lifecycle ————————————————————————————————————————————————

    async def _start(self):
        # Default disable list минус то что юзер явно вернул через enable_features
        effective_disable = tuple(
            f for f in _DISABLED_NATIVE_FEATURES if f not in self._enable_features
        )
        args = [self._codex_path, "app-server"]
        for feat in effective_disable:
            args.extend(["--disable", feat])
        # Config keys (отдельно от feature flags). enable_features может убирать
        # из конфиг-default'ов тоже — если юзер кладёт "web_search=cached" или
        # "web_search=live" в enable_features, не пишем дефолтный disable.
        for key, default_val in _DISABLED_CONFIG_KEYS:
            user_override = next(
                (f for f in self._enable_features if f.startswith(f"{key}=")), None,
            )
            if user_override:
                args.extend(["-c", user_override])
            else:
                args.extend(["-c", f"{key}={default_val}"])
        log.info("[codex] spawning %s app-server (disabled: %s; configs: %s; enabled-back: %s)",
                 self._codex_path,
                 ", ".join(effective_disable) or "<none>",
                 ", ".join(f"{k}={v}" for k, v in _DISABLED_CONFIG_KEYS) or "<none>",
                 ", ".join(self._enable_features) or "<none>")
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ,  # наследуем HTTPS_PROXY и прочее
        )
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())
        self._router_task = asyncio.create_task(self._router_loop())

        # Handshake — обязательная часть запуска, без него сервер откажет на
        # любых thread/turn запросах. Делается ровно один раз на singleton.
        info = await self.request("initialize", {
            "clientInfo": {"name": "slon", "title": "Slonagent", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True},
        }, timeout=10)
        log.info("[codex] initialized: %s/%s",
                 info.get("platformFamily"), info.get("platformOs"))
        await self.notify("initialized", {})
        acct = await self.request("account/read", {"refreshToken": False}, timeout=10)
        account = acct.get("account") if isinstance(acct, dict) else None
        if not account:
            raise RuntimeError("codex: не авторизован. Запусти `codex login` и повтори.")
        log.info("[codex] account: type=%s plan=%s",
                 account.get("type"), account.get("planType"))

    async def _shutdown(self):
        if self._proc is None:
            return
        with suppress(Exception):
            self._proc.stdin.close()
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            with suppress(Exception):
                self._proc.kill()
                await self._proc.wait()
        for t in (self._reader_task, self._stderr_task, self._router_task):
            if t and not t.done():
                t.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await t
        self._proc = None
        self._handlers.clear()

    # — I/O loops ————————————————————————————————————————————————

    async def _reader_loop(self):
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                return
            try:
                msg = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                log.warning("[codex] non-JSON line: %r", line[:200])
                continue
            await self._inbox.put(msg)

    async def _stderr_loop(self):
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                return
            s = line.decode("utf-8", errors="replace").rstrip()
            if s:
                log.warning("[codex:stderr] %s", s)

    async def _router_loop(self):
        while True:
            msg = await self._inbox.get()
            try:
                await self._route_msg(msg)
            except Exception:
                log.exception("[codex] router error on msg=%r", msg)

    async def _route_msg(self, msg: dict):
        # Response на наш request
        if "id" in msg and ("result" in msg or "error" in msg):
            rid = msg["id"]
            fut = self._pending.pop(rid, None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(RuntimeError(json.dumps(msg["error"])))
                else:
                    fut.set_result(msg.get("result"))
            return

        method = msg.get("method")
        if method is None:
            log.warning("[codex] unknown message shape: %r", msg)
            return

        # Server-initiated request (id + method)
        if "id" in msg and "result" not in msg and "error" not in msg:
            params = msg.get("params") or {}
            tid = params.get("threadId")
            handler = self._handlers.get(tid) if tid else None
            if handler:
                await handler._handle_server_request(msg)
            else:
                # Не знаем чей это thread — отклоняем чтоб сервер не висел.
                log.warning("[codex] server-request %s for unknown thread=%r — declining",
                            method, tid)
                with suppress(Exception):
                    await self.reply_to(msg["id"], "decline")
            return

        # Notification — роутим по threadId если он есть, иначе log only
        params = msg.get("params") or {}
        tid = params.get("threadId")
        if tid:
            handler = self._handlers.get(tid)
            if handler:
                await handler._handle_notification(msg)
        # else: глобальные нотификации (account/updated, model/* и пр.) — игнорим

    # — wire-level primitives ————————————————————————————————————

    async def _send_raw(self, msg: dict):
        line = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            self._proc.stdin.write(line)
            await self._proc.stdin.drain()

    async def notify(self, method: str, params: dict | None = None):
        msg = {"method": method}
        if params is not None:
            msg["params"] = params
        await self._send_raw(msg)

    async def request(self, method: str, params: dict, timeout: float = 120.0):
        rid = self._next_id
        self._next_id += 1
        fut = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        await self._send_raw({"method": method, "params": params, "id": rid})
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise

    async def reply_to(self, rid, result):
        await self._send_raw({"id": rid, "result": result})

    # — handler registration —————————————————————————————————————

    def register_thread(self, thread_id: str, backend: "CodexBackend"):
        self._handlers[thread_id] = backend

    def unregister_thread(self, thread_id: str):
        self._handlers.pop(thread_id, None)


# ═══════════════════════════════════════════════════════════════════════════════
# CodexBackend — per-Agent thread state поверх shared CodexClient
# ═══════════════════════════════════════════════════════════════════════════════

class CodexBackend(BaseBackend):
    def __init__(self, agent, codex_options: dict | None = None,
                 enable_features: list | tuple | None = None,
                 sandbox: str | None = None,
                 approval_policy: str | None = None,
                 reasoning_effort: str | None = None,
                 reasoning_summary: str | None = None):
        """codex_options — словарь, который переопределяет/добавляет ключи
        thread/start перед запросом.

        enable_features — какие нативные тулы codex'а вернуть в работу
        (по умолчанию опасные отключены — shell_tool, browser_use и т.п.).
        Также можно класть `key=value` для config-overrides — например
        "web_search=live". См. `codex features list`. Специальное значение
        "apply_patch" разрешает встроенный apply_patch (мы auto-accept'им
        соответствующие approval-запросы).

        sandbox — codex'овский sandbox-режим (`read-only` / `workspace-write` /
        `danger-full-access`). Default `danger-full-access` — модель в своих
        системках видит "all permitted", не пугается; apply_patch блочится
        через approval-hook.

        approval_policy — `never` / `on-request` / `on-failure` / `untrusted`.
        Default `untrusted`: codex шлёт нам approval-request перед write
        через встроенные тулы, мы declining → блок без kernel sandbox'а.
        Если поставить `never` — apply_patch будет работать (юзер берёт
        ответственность).

        Наши dynamicTools НЕ зависят ни от sandbox, ни от approval —
        выполняются нашим Python-кодом через server-request `item/tool/call`.

        Pool-нюанс: backend'ы с одинаковым enable_features шарят процесс
        codex app-server; с разными — отдельный subprocess каждый. Остальные
        параметры (sandbox/approval) — thread-level.

        Путь к codex-бинарю — через env CODEX_PATH или автодетект, не per-backend.
        """
        super().__init__(agent)
        self._codex_options = codex_options or {}
        self._enable_features = tuple(enable_features or ())
        self._sandbox = sandbox or _THREAD_SANDBOX_DEFAULT
        self._approval_policy = approval_policy or _THREAD_APPROVAL_POLICY_DEFAULT
        self._reasoning_effort = reasoning_effort or _REASONING_EFFORT_DEFAULT
        self._reasoning_summary = reasoning_summary or _REASONING_SUMMARY_DEFAULT

        # cwd живёт под memory_dir (как у клода). Для эфемерного агента — process cwd.
        if agent.memory.memory_dir:
            self._cwd = os.path.join(agent.memory.memory_dir, "workspace")
            os.makedirs(self._cwd, exist_ok=True)
        else:
            self._cwd = os.getcwd()

        self._client: CodexClient | None = None  # singleton, ленивая инициализация
        self._thread_id: str | None = None
        self._thread_path: str | None = None  # путь к rollout-*.jsonl
        self._client_tools_fp: str | None = None
        self._client_dev_fp: str | None = None  # fingerprint developerInstructions
        self._memory_state: dict = {}  # ephemeral fallback

        # In-flight dynamic-tool tasks (для cancel при interrupt)
        self._active_tool_tasks: set[asyncio.Task] = set()

        # Стрим-стейт текущего llm() — заполняется обработчиками notification'ов
        # и сбрасывается на выходе из llm(). Пока пуст — нотификации игнорируются.
        # stream_id для каждого нового item берётся из process-wide `_next_stream_id()`
        # — гарантирует уникальность и в пределах llm(), и между llm()'ами, и
        # между рестартами процесса (UI хранит сообщения старой сессии и при
        # collision'е редактировал бы их вместо новых).
        self._stream_state: dict = {}

    @property
    def _state_file(self) -> str | None:
        if not self.agent.memory.memory_dir:
            return None
        tid = self.agent.thread_id
        fname = f"CODEX_{tid}.json" if tid else "CODEX.json"
        return os.path.join(self.agent.memory.memory_dir, fname)

    def _load_state(self) -> dict:
        if self._state_file is None:
            return dict(self._memory_state)
        try:
            with open(self._state_file) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_state(self, state: dict):
        if self._state_file is None:
            self._memory_state = dict(state)
            return
        with open(self._state_file, "w") as f:
            json.dump(state, f)

    def _skills_fingerprint(self) -> str:
        items = []
        for skill in self.agent.skills:
            for decl in skill.get_tools():
                fn = decl["function"]
                items.append((
                    fn["name"],
                    fn.get("description", ""),
                    json.dumps(fn.get("parameters") or {}, sort_keys=True),
                ))
        items.sort()
        return hashlib.sha256(repr(items).encode()).hexdigest()

    def _build_dynamic_tools(self) -> list[dict]:
        tools = []
        for skill in self.agent.skills:
            for decl in skill.get_tools():
                fn = decl["function"]
                tools.append({
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "inputSchema": fn.get("parameters") or {"type": "object", "properties": {}},
                })
        return tools

    # — wire delegators (тестируемые точки patch'а) ————————————————

    async def _request(self, method: str, params: dict, timeout: float = 120.0):
        return await self._client.request(method, params, timeout=timeout)

    async def _send(self, method: str, params: dict | None = None, id_: int | None = None):
        if id_ is not None:
            await self._client.request(method, params or {}, timeout=120)
        else:
            await self._client.notify(method, params)

    async def _reply(self, rid, result):
        await self._client.reply_to(rid, result)

    # — память → Responses API items (для thread/inject_items) ———————

    @staticmethod
    def _memory_to_response_items(turns: list) -> list[dict]:
        """Конвертит наши OpenAI-style turns в Raw Responses API items
        (формат который codex принимает в `thread/inject_items` и сам пишет
        в rollout-jsonl как response_item.payload).
        """
        items: list[dict] = []
        for t in turns or ():
            if not isinstance(t, dict):
                continue
            role = t.get("role")
            if role == "user":
                content = t.get("content")
                parts: list[dict] = []
                if isinstance(content, str):
                    if content:
                        parts.append({"type": "input_text", "text": content})
                elif isinstance(content, list):
                    for p in content:
                        if not isinstance(p, dict):
                            continue
                        if p.get("type") == "text" and p.get("text"):
                            parts.append({"type": "input_text", "text": p["text"]})
                        elif p.get("type") == "image_url":
                            url = (p.get("image_url") or {}).get("url", "")
                            if url:
                                parts.append({"type": "input_image", "image_url": url})
                if parts:
                    items.append({"type": "message", "role": "user", "content": parts})
            elif role == "assistant":
                tool_calls = t.get("tool_calls") or ()
                if tool_calls:
                    for tc in tool_calls:
                        items.append({
                            "type": "function_call",
                            "name": tc["function"]["name"],
                            "arguments": tc["function"].get("arguments", "{}"),
                            "call_id": tc["id"],
                        })
                    continue
                content = t.get("content")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = "".join(
                        p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                if text:
                    items.append({
                        "type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    })
            elif role == "tool":
                output = t.get("content", "")
                if not isinstance(output, str):
                    output = json.dumps(output, ensure_ascii=False)
                items.append({
                    "type": "function_call_output",
                    "call_id": t.get("tool_call_id"),
                    "output": output,
                })
        return items

    # — divergence detection (нужен ли свежий thread с inject) —————————

    @staticmethod
    def _memory_signatures(turns: list) -> set:
        """Сигнатуры response_item'ов которые мы ожидаем в jsonl исходя из памяти.

        Tool-calls/results — call_id. user/assistant messages — первые 200
        символов текста.
        """
        expected: set = set()
        for t in turns or ():
            if not isinstance(t, dict):
                continue
            role = t.get("role")
            if role == "assistant" and t.get("tool_calls"):
                for tc in t["tool_calls"]:
                    expected.add(("function_call", tc.get("id")))
                continue
            content = t.get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        text += p.get("text", "")
            text = text[:200]
            if role == "user" and text:
                expected.add(("message", "user", text))
            elif role == "assistant" and text:
                expected.add(("message", "assistant", text))
            elif role == "tool" and t.get("tool_call_id"):
                expected.add(("function_call_output", t["tool_call_id"]))
        return expected

    @staticmethod
    def _is_codex_internal(payload: dict) -> bool:
        """Codex кладёт в jsonl свои service-сообщения как response_item:
          - role=developer (permissions / skills instructions)
          - role=user с XML-обёрткой `<environment_context>...</environment_context>`
        Их игнорируем при сверке памяти с тредом.
        """
        if payload.get("type") != "message":
            return False
        role = payload.get("role")
        if role == "developer":
            return True
        if role == "user":
            for c in payload.get("content") or ():
                t = (c.get("text") or "") if isinstance(c, dict) else ""
                if t.lstrip().startswith("<") and ">" in t:
                    return True
        return False

    @staticmethod
    def _stable_memory_turns(turns: list) -> list:
        """Хвост user-турнов в конце памяти — это «pending», ещё не ушло в codex
        (uplive отправится в следующем turn/start). Их при сверке с jsonl
        исключаем — иначе нормальный flow «append user → llm()» постоянно
        будет давать ложный divergence.
        """
        end = len(turns)
        for i in range(len(turns) - 1, -1, -1):
            t = turns[i]
            if isinstance(t, dict) and t.get("role") == "user":
                end = i
            else:
                break
        return turns[:end]

    def _jsonl_signatures(self) -> set:
        """Сигнатуры response_item'ов из rollout-jsonl (без codex-internal)."""
        sigs: set = set()
        if not self._thread_path or not os.path.exists(self._thread_path):
            return sigs
        with open(self._thread_path, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "response_item":
                    continue
                payload = entry.get("payload") or {}
                if self._is_codex_internal(payload):
                    continue
                ptype = payload.get("type")
                if ptype == "function_call":
                    sigs.add(("function_call", payload.get("call_id")))
                elif ptype == "function_call_output":
                    sigs.add(("function_call_output", payload.get("call_id")))
                elif ptype == "message":
                    text = "".join(
                        c.get("text", "") for c in (payload.get("content") or [])
                        if isinstance(c, dict) and c.get("type") in ("input_text", "output_text")
                    )
                    sigs.add(("message", payload.get("role"), text[:200]))
        return sigs

    async def _thread_has_diverged(self) -> bool:
        """True если стабильная часть памяти и rollout-jsonl расходятся —
        в любую сторону. Триггеры:

          - jsonl содержит turn'ы, которых нет в памяти (наша компрессия
            их выкинула; codex продолжит видеть старое)
          - память содержит turn'ы, которых нет в jsonl (внешнее
            вмешательство в файл; crash при стриме когда мы успели
            записать в память, а codex не успел в jsonl)

        Pending хвост user-турнов исключаем — он по определению ещё не дошёл
        до codex'а (отправится в следующем turn/start). Сравниваем имея в
        виду `strip_contents_private` — agent добавляет `[timestamp]` префикс
        к user-турнам ПЕРЕД отправкой LLM (чтобы модель знала время), и
        именно эта формат-копия лежит в codex jsonl. Сравниваем как ушло.
        """
        if not self._thread_path or not os.path.exists(self._thread_path):
            return False
        stable = self._stable_memory_turns(self.agent.memory._turns)
        # Прогоняем через ту же трансформацию что и при отправке в codex,
        # иначе timestamp-префикс в jsonl не совпадает с raw memory text.
        formatted = self.agent.strip_contents_private(stable)
        expected = self._memory_signatures(formatted)
        actual = self._jsonl_signatures()
        if not expected and not actual:
            return False
        if expected == actual:
            return False
        # Логируем что именно разошлось — без этого ловить регрессии в
        # normal flow невозможно (пользователь видит только «started fresh
        # thread reason=memory diverged» и не понимает причину).
        only_in_memory = expected - actual
        only_in_jsonl = actual - expected
        log.warning("[codex] divergence detected: "
                    "%d only in memory, %d only in jsonl",
                    len(only_in_memory), len(only_in_jsonl))
        for sig in list(only_in_memory)[:5]:
            log.warning("[codex]   only in memory: %r", sig)
        for sig in list(only_in_jsonl)[:5]:
            log.warning("[codex]   only in jsonl:  %r", sig)
        return True

    # — thread lifecycle —————————————————————————————————————————

    async def _build_developer_instructions(self, user_text: str, system_prompt: str | None) -> str:
        """Собирает thread-level instructions из skill-context'ов + explicit
        system_prompt. Идёт в developerInstructions thread/start — НЕ в каждый
        turn'овый input, чтоб не дублироваться в истории на каждом шаге.
        """
        parts: list[str] = []
        for skill in self.agent.skills:
            ctx = await skill.get_context_prompt(user_text)
            if ctx:
                parts.append(ctx)
        if system_prompt:
            parts.append(system_prompt)
        return "\n\n".join(parts)

    @staticmethod
    def _fp(text: str) -> str:
        return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

    @property
    def _is_ephemeral(self) -> bool:
        """Эфемерный backend — без agent_dir/memory_dir. Сюда попадают
        one-shot агенты типа компрессии памяти / fact-recall — им не нужна
        persistent история. Включаем codex'овый ephemeral=true чтоб он
        не писал rollout на диск (быстрее + ничего чистить не надо).
        """
        return self._state_file is None

    async def _start_fresh_thread(self, reason: str, dev_instructions: str):
        """Создаёт thread/start с актуальными dynamicTools + developerInstructions,
        инжектит туда нашу память через thread/inject_items, удаляет старый
        rollout-jsonl с диска. Используется и при первом запуске, и при
        компрессии-расхождении, и при смене скиллов / dev-instructions.
        """
        old_thread_path = self._thread_path

        params = {
            "cwd": self._cwd,
            # approvalPolicy=on-request → codex шлёт нам approval-request
            # на write-операции встроенных тулов (apply_patch и пр.). Наш
            # _handle_server_request их declining → блок без kernel-sandbox'а.
            "approvalPolicy": self._approval_policy,
            # workspace-write вместо read-only — модель в системках видит
            # "ты можешь писать в workspace", не пугается; реальная защита
            # от apply_patch через approval-flow (см. выше).
            "sandbox": self._sandbox,
            # baseInstructions со своим минимальным текстом → codex НЕ
            # вкидывает свою длинную системку про "You are Codex" + блоки
            # <permissions instructions> / <skills_instructions> /
            # <environment_context>. Наши skill-контексты и system_prompt
            # уходят в developerInstructions отдельно.
            "baseInstructions": _BASE_INSTRUCTIONS_DEFAULT,
            "personality": "none",
            "serviceName": "slon",
            "dynamicTools": self._build_dynamic_tools(),
        }
        # Если model_name не задан / пуст — не пишем поле вообще, codex
        # подберёт свой default (на free ChatGPT это gpt-5.5). Иначе он
        # принимает '' как валидную строку и падает с
        # "'' model is not supported when using Codex with a ChatGPT account".
        if self.agent.model_name:
            params["model"] = self.agent.model_name
        if dev_instructions:
            params["developerInstructions"] = dev_instructions
        if self._is_ephemeral:
            # Codex не будет писать rollout-*.jsonl на диск; thread живёт
            # только в памяти процесса до своего close/expire.
            params["ephemeral"] = True
        params.update(self._codex_options)

        resp = await self._request("thread/start", params, timeout=30)
        new_thread_id = resp["thread"]["id"]
        new_thread_path = resp["thread"].get("path")
        log.info("[codex] started fresh thread=%s (reason: %s)", new_thread_id, reason)

        # Регистрируем НОВЫЙ thread у клиента ДО инжекта — иначе нотификации
        # от inject не дойдут (мы пока ещё на старом).
        if self._thread_id:
            self._client.unregister_thread(self._thread_id)
        self._thread_id = new_thread_id
        self._thread_path = new_thread_path
        self._client.register_thread(self._thread_id, self)
        self._client_tools_fp = self._skills_fingerprint()
        self._client_dev_fp = self._fp(dev_instructions)
        # Сохраняем fingerprints в state — без этого после рестарта
        # slonagent'а in-memory fp'ы сбрасываются в None, resume-условие
        # фейлит ("skills changed") и мы сворачиваемся в fresh thread
        # каждый старт, теряя историю в codex'е.
        self._save_state({
            "thread_id": self._thread_id,
            "tools_fp": self._client_tools_fp,
            "dev_fp": self._client_dev_fp,
        })

        # Только stable-часть памяти. Trailing pending user'ов НЕ инжектим —
        # они уйдут в codex отдельно через input первого turn/start. Иначе
        # в jsonl получаем дубль user-сообщения (один из inject, один из
        # turn/start), что ломает наш divergence-check.
        # ВАЖНО: прогоняем через strip_contents_private — это та же
        # трансформация, что и в _build_user_input. Иначе injected items
        # будут без [timestamp] префикса, а turn-входы с — divergence на
        # следующем llm().
        items = self._memory_to_response_items(
            self.agent.strip_contents_private(
                self._stable_memory_turns(self.agent.memory._turns)
            )
        )
        if items:
            await self._request("thread/inject_items", {
                "threadId": self._thread_id,
                "items": items,
            }, timeout=30)
            log.info("[codex] injected %d items into fresh thread", len(items))

        # Сносим старый rollout (он теперь orphan, история перелита)
        if old_thread_path and old_thread_path != new_thread_path and os.path.exists(old_thread_path):
            with suppress(OSError):
                os.remove(old_thread_path)
                log.info("[codex] removed old rollout %s", old_thread_path)

    async def _ensure_thread(self, dev_instructions: str):
        """На каждом llm()-вызове проверяет/создаёт thread. Триггеры на fresh
        (thread/start + inject_items):
          - первый запуск (нет saved thread_id)
          - набор скиллов изменился (skills_fp diff)
          - developerInstructions изменились (dev_fp diff) — переехать на
            новый thread с обновлёнными instructions
          - в jsonl есть ответы которых нет в нашей памяти (компрессия)
        """
        skills_fp = self._skills_fingerprint()
        dev_fp = self._fp(dev_instructions)
        state = self._load_state()

        # Берём fingerprints из state (после рестарта slonagent'а in-memory
        # _client_tools_fp = None и условие resume фейлит без fallback'а).
        saved_thread_id = state.get("thread_id")
        saved_tools_fp = state.get("tools_fp") or self._client_tools_fp
        saved_dev_fp = state.get("dev_fp") or self._client_dev_fp

        if (saved_thread_id
                and saved_tools_fp == skills_fp
                and saved_dev_fp == dev_fp):
            try:
                # 30s — у codex resume иногда занимает >10s (cold thread,
                # медленный сетевой round-trip к OpenAI). На 10s бывал
                # ложный timeout → fresh thread без причины.
                resp = await self._request("thread/resume", {
                    "threadId": saved_thread_id,
                }, timeout=30)
                self._thread_id = resp["thread"]["id"]
                self._thread_path = resp["thread"].get("path")
                # Восстанавливаем in-memory fp на случай если процесс свежий.
                self._client_tools_fp = skills_fp
                self._client_dev_fp = dev_fp
                self._client.register_thread(self._thread_id, self)
                if await self._thread_has_diverged():
                    self._client.unregister_thread(self._thread_id)
                    await self._start_fresh_thread(
                        "memory diverged from rollout jsonl", dev_instructions,
                    )
                    return
                log.info("[codex] resumed thread=%s", self._thread_id)
                return
            except Exception as e:
                # type().__name__ обязательно — у asyncio.TimeoutError
                # пустой str() и в логе остаётся только "() — starting fresh".
                log.warning("[codex] thread/resume failed (%s: %s) — starting fresh",
                            type(e).__name__, e)

        if saved_thread_id is None:
            reason = "first run / no state"
        elif saved_tools_fp != skills_fp:
            reason = "skills changed"
        elif saved_dev_fp != dev_fp:
            reason = "developer instructions changed"
        else:
            reason = "resume failed"
        await self._start_fresh_thread(reason, dev_instructions)

    # — incoming JSON-RPC handlers (вызываются роутером client'а) ————

    # apply_patch — единственный baked-in write-тул codex'а: --disable флага
    # для него нет (issue #8161 closed-not-planned). Единственный способ
    # блокировать — approval-hook: с approval_policy="on-request" codex шлёт
    # нам approval перед применением patch'а, мы decline'им → patch не
    # применяется. Прочие нативные write-тулы (shell_tool, browser_use,
    # apply_patch_freeform/streaming) отрублены через --disable, approval'ы
    # для них в норме не приходят вообще.
    _APPLY_PATCH_APPROVAL_METHODS = frozenset({
        "item/fileChange/requestApproval",
        "applyPatchApproval",
    })
    # Если юзер кладёт это в enable_features — разрешает apply_patch
    # (psevdo-feature, реального --enable для baked-in apply_patch нет).
    _APPLY_PATCH_ALLOW_KEY = "apply_patch"

    async def _handle_server_request(self, msg):
        method = msg["method"]
        rid = msg["id"]
        params = msg.get("params") or {}

        if method == "item/tool/call":
            task = asyncio.create_task(self._handle_tool_call(rid, params))
            self._active_tool_tasks.add(task)
            task.add_done_callback(self._active_tool_tasks.discard)
            return

        if method in self._APPLY_PATCH_APPROVAL_METHODS:
            # Юзер может явно разрешить apply_patch через
            # backend_params.enable_features=["apply_patch"]. По дефолту —
            # decline (модель должна писать через наши functions.sandbox_*).
            allow = self._APPLY_PATCH_ALLOW_KEY in self._enable_features
            decision = "accept" if allow else "decline"
            log.info("[codex] apply_patch approval (%s) → %s", method, decision)
            with suppress(Exception):
                await self._reply(rid, {"decision": decision})
            return

        log.warning("[codex] unexpected server request %s — declining", method)
        with suppress(Exception):
            await self._reply(rid, {"decision": "decline"})

    async def _handle_tool_call(self, rid: int, params: dict):
        tool = params.get("tool")
        args = params.get("arguments") or {}
        call_id = params.get("callId")
        log.info("[codex] tool call: %s args=%s", tool, json.dumps(args, ensure_ascii=False)[:200])

        try:
            fake_turn = {
                "tool_calls": [{
                    "id": call_id,
                    "function": {
                        "name": tool,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                }],
            }
            tool_turns = await self.agent.dispatch_tool_calls(fake_turn, emit_transport_events=False)
        except asyncio.CancelledError:
            with suppress(Exception):
                await self._reply(rid, {"success": False, "contentItems": [
                    {"type": "inputText", "text": "[прервано пользователем]"},
                ]})
            raise
        except Exception as e:
            log.exception("[codex] tool %s failed", tool)
            with suppress(Exception):
                await self._reply(rid, {"success": False, "contentItems": [
                    {"type": "inputText", "text": f"[error: {e}]"},
                ]})
            return

        content_items: list[dict] = []
        for t in tool_turns:
            c = t.get("content")
            if isinstance(c, str):
                content_items.append({"type": "inputText", "text": c})
            elif isinstance(c, list):
                for p in c:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "text":
                        content_items.append({"type": "inputText", "text": p.get("text", "")})
                    elif p.get("type") == "image_url":
                        url = (p.get("image_url") or {}).get("url", "")
                        if url:
                            content_items.append({"type": "inputImage", "imageUrl": url})
        if not content_items:
            content_items = [{"type": "inputText", "text": ""}]
        with suppress(Exception):
            await self._reply(rid, {"success": True, "contentItems": content_items})

    async def _handle_notification(self, msg):
        s = self._stream_state
        if not s:
            return
        method = msg.get("method")
        params = msg.get("params") or {}

        if method == "turn/started":
            return
        if method == "turn/completed":
            s["result_params"] = params
            evt: asyncio.Event = s.get("wakeup")
            if evt:
                evt.set()
            return

        if method == "item/agentMessage/delta":
            item_id = params.get("itemId")
            bufs = s.setdefault("text_bufs", {})
            sids = s.setdefault("text_stream_ids", {})
            bufs[item_id] = bufs.get(item_id, "") + params.get("delta", "")
            if item_id not in sids:
                sids[item_id] = _next_stream_id()
            await self.agent.transport.send_message(bufs[item_id], stream_id=sids[item_id], final=False)
            return

        if method == "item/reasoning/textDelta":
            item_id = params.get("itemId")
            bufs = s.setdefault("thinking_bufs", {})
            sids = s.setdefault("thinking_stream_ids", {})
            bufs[item_id] = bufs.get(item_id, "") + params.get("delta", "")
            if item_id not in sids:
                sids[item_id] = _next_stream_id()
            await self.agent.transport.send_thinking(bufs[item_id], stream_id=sids[item_id])
            return

        if method == "item/reasoning/summaryTextDelta":
            # На free ChatGPT (gpt-5.5) raw textDelta никогда не приходит,
            # только summary. Обрабатываем — иначе мысли модели не видны
            # вовсе. Если в будущем модель/план начнёт давать textDelta —
            # они уйдут отдельным stream'ом (другой item_id) выше.
            item_id = params.get("itemId")
            # summary может состоять из нескольких partов (`summaryIndex`),
            # все ложим в один stream — иначе UI получит фрагментированный
            # thinking блоками.
            bufs = s.setdefault("thinking_bufs", {})
            sids = s.setdefault("thinking_stream_ids", {})
            bufs[item_id] = bufs.get(item_id, "") + params.get("delta", "")
            if item_id not in sids:
                sids[item_id] = _next_stream_id()
            await self.agent.transport.send_thinking(bufs[item_id], stream_id=sids[item_id])
            return

        # Стрим вывода native shell_tool (commandExecution): аккумулируем
        # текст в буфер чтоб на item/completed получить полный output.
        if method == "item/commandExecution/outputDelta":
            item_id = params.get("itemId")
            bufs = s.setdefault("cmd_output_bufs", {})
            bufs[item_id] = bufs.get(item_id, "") + params.get("delta", "")
            return

        if method == "item/started":
            item = params.get("item") or {}
            t = item.get("type")
            if t == "dynamicToolCall":
                await self.agent.transport.on_tool_call(item.get("tool"), item.get("arguments") or {})
            elif t == "commandExecution":
                await self.agent.transport.on_tool_call(
                    "shell_command",
                    {"command": item.get("command", ""), "cwd": item.get("cwd", "")},
                )
            elif t == "fileChange":
                await self.agent.transport.on_tool_call(
                    "file_change", {"changes": item.get("changes", [])},
                )
            elif t == "webSearch":
                await self.agent.transport.on_tool_call(
                    "web_search", {"query": item.get("query", "")},
                )
            elif t == "mcpToolCall":
                # Полное имя как codex его регистрирует — server__tool
                name = f"{item.get('server')}__{item.get('tool')}"
                await self.agent.transport.on_tool_call(name, item.get("arguments") or {})
            return

        if method == "item/completed":
            item = params.get("item") or {}
            t = item.get("type")
            item_id = item.get("id")

            if t == "agentMessage":
                text = item.get("text") or ""
                stream_id = s.get("text_stream_ids", {}).get(item_id)
                if text:
                    await self.agent.transport.send_message(text, stream_id=stream_id, final=True)
                    s.setdefault("turns", []).append({
                        "role": "assistant",
                        "content": text,
                        "_codex_item_id": item_id,
                    })
                return

            if t == "reasoning":
                content = item.get("content") or ""
                if content:
                    stream_id = s.get("thinking_stream_ids", {}).get(item_id)
                    await self.agent.transport.send_thinking(content, stream_id=stream_id, final=True)
                return

            if t == "dynamicToolCall":
                tool_name = item.get("tool")
                call_id = item_id
                args = item.get("arguments") or {}
                content_items = item.get("contentItems") or []
                turns = s.setdefault("turns", [])
                turns.append({
                    "role": "assistant",
                    "tool_calls": [{
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(args, ensure_ascii=False),
                        },
                    }],
                    "_codex_item_id": call_id,
                })
                result_text = "\n".join(
                    ci.get("text", "") for ci in content_items
                    if isinstance(ci, dict) and ci.get("type") == "inputText"
                )
                turns.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": tool_name,
                    "content": result_text,
                    "_codex_item_id": call_id,
                })
                await self.agent.transport.on_tool_result(tool_name, result_text)
                return

            # Нативные тулы codex'а (если включены через enable_features) —
            # пишем их в turns так же как dynamicToolCall. Имя совпадает с
            # тем что codex кладёт в jsonl как function_call.name, поэтому
            # divergence-check автоматически даёт match (без хаков и
            # whitelist'ов).
            if t == "commandExecution":
                call_id = item_id
                cmd = item.get("command", "")
                cwd = item.get("cwd", "")
                exit_code = item.get("exitCode")
                output_text = s.get("cmd_output_bufs", {}).pop(call_id, "")
                result_text = f"Exit code: {exit_code}\nOutput:\n{output_text}".rstrip()
                turns = s.setdefault("turns", [])
                turns.append({
                    "role": "assistant",
                    "tool_calls": [{
                        "id": call_id, "type": "function",
                        "function": {
                            "name": "shell_command",
                            "arguments": json.dumps({"command": cmd, "cwd": cwd}, ensure_ascii=False),
                        },
                    }],
                    "_codex_item_id": call_id,
                })
                turns.append({
                    "role": "tool", "tool_call_id": call_id, "name": "shell_command",
                    "content": result_text, "_codex_item_id": call_id,
                })
                await self.agent.transport.on_tool_result("shell_command", result_text)
                return

            if t == "fileChange":
                call_id = item_id
                changes = item.get("changes") or []
                status = item.get("status", "")
                result_text = f"status: {status}\nchanges:\n" + "\n".join(
                    f"  {c.get('kind','?')} {c.get('path','?')}" for c in changes if isinstance(c, dict)
                )
                turns = s.setdefault("turns", [])
                turns.append({
                    "role": "assistant",
                    "tool_calls": [{
                        "id": call_id, "type": "function",
                        "function": {
                            "name": "file_change",
                            "arguments": json.dumps({"changes": changes}, ensure_ascii=False),
                        },
                    }],
                    "_codex_item_id": call_id,
                })
                turns.append({
                    "role": "tool", "tool_call_id": call_id, "name": "file_change",
                    "content": result_text, "_codex_item_id": call_id,
                })
                await self.agent.transport.on_tool_result("file_change", result_text)
                return

            if t == "webSearch":
                call_id = item_id
                query = item.get("query", "")
                action = item.get("action") or {}
                turns = s.setdefault("turns", [])
                turns.append({
                    "role": "assistant",
                    "tool_calls": [{
                        "id": call_id, "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": json.dumps({"query": query}, ensure_ascii=False),
                        },
                    }],
                    "_codex_item_id": call_id,
                })
                turns.append({
                    "role": "tool", "tool_call_id": call_id, "name": "web_search",
                    "content": json.dumps(action, ensure_ascii=False),
                    "_codex_item_id": call_id,
                })
                await self.agent.transport.on_tool_result("web_search", action)
                return

            if t == "mcpToolCall":
                call_id = item_id
                server = item.get("server", "")
                tool = item.get("tool", "")
                name = f"{server}__{tool}"
                args = item.get("arguments") or {}
                result = item.get("result")
                if not isinstance(result, str):
                    result = json.dumps(result, ensure_ascii=False) if result is not None else ""
                turns = s.setdefault("turns", [])
                turns.append({
                    "role": "assistant",
                    "tool_calls": [{
                        "id": call_id, "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(args, ensure_ascii=False),
                        },
                    }],
                    "_codex_item_id": call_id,
                })
                turns.append({
                    "role": "tool", "tool_call_id": call_id, "name": name,
                    "content": result, "_codex_item_id": call_id,
                })
                await self.agent.transport.on_tool_result(name, result)
                return

    # — user input construction —————————————————————————————————

    @staticmethod
    async def _build_user_input(pending: list) -> list[dict]:
        items: list[dict] = []
        for t in pending:
            c = t.get("content")
            if isinstance(c, str):
                if c:
                    items.append({"type": "text", "text": c})
                continue
            for b in c or ():
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text"):
                    items.append({"type": "text", "text": b["text"]})
                elif b.get("type") == "image_url":
                    url = (b.get("image_url") or {}).get("url", "")
                    if url:
                        items.append({"type": "image", "url": url})
        return items

    # — public API —————————————————————————————————————————————

    async def close(self):
        # Снимаем регистрацию thread'а с shared client'а, но сам client НЕ
        # трогаем — на нём может висеть другой backend (subagent / параллельный
        # инстанс). Client живёт до конца процесса.
        if self._thread_id and self._client:
            self._client.unregister_thread(self._thread_id)
        self._thread_id = None

    def __del__(self):
        # Эфемерный backend (нет memory_dir → state_file == None) → удаляем
        # rollout-jsonl чтобы между запусками не копился orphan history.
        try:
            if self._state_file is None and self._thread_path:
                with suppress(OSError):
                    os.remove(self._thread_path)
                    log.info("[codex] cleaned up ephemeral session %s", self._thread_path)
        except Exception:
            pass

    async def llm(self, tool_choice: str = None, parallel_tool_calls: bool = None,
                  temperature: float = 1.0, max_tokens: int | None = None,
                  system_prompt: str | None = None):
        agent = self.agent

        if self._client is None:
            self._client = await CodexClient.get(enable_features=self._enable_features)

        # Собираем pending user-турны (хвост памяти всё подряд role=user).
        pending = []
        contents = await agent.memory.get_contents()
        for t in reversed(contents):
            if not isinstance(t, dict) or t.get("role") != "user":
                break
            pending.insert(0, t)
        pending = agent.strip_contents_private(pending)
        if not pending:
            raise RuntimeError("CodexBackend.llm(): нет user-турнов в хвосте памяти — нечего слать")

        input_items = await self._build_user_input(pending)
        if not input_items:
            raise RuntimeError("CodexBackend.llm(): pending user turns не содержат текста/картинок")

        # Skill context'ы + system_prompt → thread.developerInstructions
        # (НЕ в per-turn input — иначе каждый turn в codex'овой истории
        # дублирует системку, контекст распухает в N раз). Изменился набор —
        # _ensure_thread поднимет fresh thread с inject.
        user_text = agent.memory.last_user_query()
        dev_instructions = await self._build_developer_instructions(user_text, system_prompt)
        await self._ensure_thread(dev_instructions)

        wakeup = asyncio.Event()
        self._stream_state = {
            "wakeup": wakeup,
            "turns": [],
            "text_bufs": {},
            "thinking_bufs": {},
            "text_stream_ids": {},
            "thinking_stream_ids": {},
            "result_params": {},
        }

        turn_params = {
            "threadId": self._thread_id,
            "input": input_items,
            # НЕ override'им sandboxPolicy per-turn — иначе read-only из
            # thread/start (который блокирует apply_patch) перестанет
            # действовать. Раньше тут было externalSandbox+network=enabled
            # — это open'нуло бы apply_patch обратно.
        }
        if self.agent.model_name:
            turn_params["model"] = self.agent.model_name
        # Reasoning settings — без них codex не эмитит мысли модели.
        # "none" значит "не передавать поле" (= использовать model default).
        if self._reasoning_effort and self._reasoning_effort != "none":
            turn_params["effort"] = self._reasoning_effort
        if self._reasoning_summary and self._reasoning_summary != "none":
            turn_params["summary"] = self._reasoning_summary
        # codex_options может явно перебить выше — последняя истина.
        for k in ("effort", "summary", "personality", "approvalPolicy"):
            if k in self._codex_options:
                turn_params[k] = self._codex_options[k]

        log.info("[codex] turn/start (%d input items) → thread=%s", len(input_items), self._thread_id)
        try:
            await self._request("turn/start", turn_params, timeout=30)
        except Exception:
            self._stream_state = {}
            raise

        try:
            await asyncio.wait_for(wakeup.wait(), timeout=600)
        except asyncio.CancelledError:
            asyncio.current_task().uncancel()
            log.warning("[codex] llm() cancelled — sending turn/interrupt")
            for t in list(self._active_tool_tasks):
                t.cancel()
            with suppress(Exception):
                await self._request("turn/interrupt", {"threadId": self._thread_id}, timeout=5)
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wakeup.wait(), timeout=10)
            turns = self._stream_state.get("turns") or []
            self._stream_state = {}
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
            await agent.transport.send_message("⚠️ Прервано")
            return turns

        result_params = self._stream_state.get("result_params", {})
        turn_info = result_params.get("turn") or {}
        status = turn_info.get("status")
        turns = self._stream_state.get("turns", [])
        self._stream_state = {}

        if status == "failed":
            err = (turn_info.get("error") or {}).get("message") or "unknown error"
            raise RuntimeError(f"codex turn failed: {err}")

        duration = turn_info.get("durationMs")
        log.info("[codex] done: %d turns, status=%s, duration=%sms", len(turns), status, duration)
        await agent.transport.send_message(f"✅ Готово ({len(turns)} turns, {duration}ms)")
        return turns
