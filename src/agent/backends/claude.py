"""ClaudeAgent — Agent с llm() через claude_agent_sdk.ClaudeSDKClient.

Вместо OpenAI-совместимого chat.completions использует Claude Code CLI
как backend. Каждый content_block ответа пишется в self.memory отдельным
turn'ом (как клод хранит в своей сессионной jsonl).
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import uuid
from contextlib import suppress

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    SdkMcpTool,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
)

from src.agent.skill import Skill, bypass
from src.agent.backends.base import BaseBackend

log = logging.getLogger(__name__)


class ClaudeAgentSkill(Skill):
    @bypass("context", "Заполнение контекста claude'а", standalone=True)
    async def context_command(self, args: str) -> str:
        client = self.agent._client
        if client is None:
            return "Клод ещё не запускался в этой сессии."
        u = await client.get_context_usage()
        return f"📊 Контекст: {u['totalTokens']:,} / {u['maxTokens']:,} ({u['percentage']:.1f}%)"


class ClaudeBackend(BaseBackend):
    def __init__(self, agent, sdk_options: dict | None = None):
        """sdk_options — словарь, который переопределяет/добавляет ключи
        ClaudeAgentOptions перед запросом. По умолчанию backend голый — без
        claude_code preset, без user settings, без встроенных тулов. Для режима
        Claude Code передавай:
            sdk_options={
                'system_prompt': {'type': 'preset', 'preset': 'claude_code'},
                'setting_sources': ['user'],
                ...
            }
        """
        super().__init__(agent)
        self._sdk_options = sdk_options or {}
        skill = ClaudeAgentSkill()
        skill.register(self)
        agent.skills.insert(0, skill)
        # cwd живёт под memory_dir (там же claude-сессии и LOG.md). Для
        # эфемерного агента (memory_dir пустой) явно берём process cwd —
        # это то же что claude CLI использовал бы по умолчанию, но раз
        # явно — helper'у не нужен fallback и __del__ корректно находит
        # session jsonl.
        if agent.memory.memory_dir:
            self._cwd = os.path.join(agent.memory.memory_dir, "workspace")
            os.makedirs(self._cwd, exist_ok=True)
        else:
            self._cwd = os.getcwd()
        self._client: ClaudeSDKClient | None = None
        self._client_append: str | None = None  # текст системки в живом клиенте
        self._client_skills_fp: str | None = None  # fingerprint скилов в живом клиенте
        self._mcp_server = None  # построится лениво из agent.skills
        self._active_tool_tasks: set[asyncio.Task] = set()  # in-flight MCP handlers
        # Эфемерный агент: session_id живёт в памяти инстанса, на диск ничего не пишем.
        self._memory_state: dict = {}

    def __del__(self):
        # Эфемерный агент (memory_dir пуст) — на shutdown'е чистим за собой
        # session jsonl в ~/.claude/projects/<cwd>/, чтоб не накапливалось
        # между запусками. Persistent main-агент имеет state_file и сюда не
        # попадает; его сессия живёт через рестарты.
        try:
            if self._state_file is None and self._memory_state.get("session_id"):
                jsonl = self._claude_session_jsonl(self._memory_state["session_id"])
                if jsonl:
                    os.remove(jsonl)
                    log.info("[claude_agent] cleaned up ephemeral session %s", jsonl)
        except Exception:
            pass  # __del__ не должен бросать

    def _skills_fingerprint(self) -> str:
        """Хеш набора тулов всех скиллов (name+description+schema). Используется
        чтобы пересобрать MCP-сервер и переподнять клиент когда скилы поменялись —
        агент мог добавить новый скилл, переключился режим и т.п."""
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

    def _build_mcp_server(self):
        """Оборачивает тулы своих скиллов в SDK MCP-сервер. Клод увидит как mcp__slon__*."""
        sdk_tools: list[SdkMcpTool] = []
        for skill in self.agent.skills:
            for decl in skill.get_tools():
                fn = decl["function"]
                name = fn["name"]
                schema = fn.get("parameters") or {"type": "object", "properties": {}}

                async def handler(args, _name=name):
                    # Регистрируемся в backend'е чтоб llm() мог отменить нас на
                    # CancelledError — SDK сам активные MCP-таски на interrupt()
                    # не отменяет, только на disconnect.
                    self._active_tool_tasks.add(asyncio.current_task())
                    try:
                        fake_turn = {
                            "tool_calls": [{
                                "id": f"mcp_{_name}",
                                "function": {
                                    "name": _name,
                                    "arguments": json.dumps(args, ensure_ascii=False),
                                },
                            }],
                        }
                        tool_turns = await self.agent.dispatch_tool_calls(fake_turn, emit_transport_events=False)
                    finally:
                        self._active_tool_tasks.discard(asyncio.current_task())
                    blocks = []
                    for t in tool_turns:
                        c = t.get("content")
                        if isinstance(c, str):
                            blocks.append({"type": "text", "text": c})
                        elif isinstance(c, list):
                            for p in c:
                                if not isinstance(p, dict): continue
                                if p.get("type") == "text":
                                    blocks.append({"type": "text", "text": p.get("text", "")})
                                elif p.get("type") == "image_url":
                                    url = (p.get("image_url") or {}).get("url", "")
                                    if url.startswith("data:") and "," in url:
                                        head, b64 = url.split(",", 1)
                                        mime = head.removeprefix("data:").split(";", 1)[0] or "image/jpeg"
                                        blocks.append({"type": "image", "data": b64, "mimeType": mime})
                    return {"content": blocks}

                sdk_tools.append(SdkMcpTool(
                    name=name,
                    description=fn.get("description", ""),
                    input_schema=schema,
                    handler=handler,
                ))
        if not sdk_tools:
            return None
        return create_sdk_mcp_server("slon", "1.0.0", sdk_tools)

    def _claude_session_jsonl(self, session_id: str) -> str | None:
        # claude CLI sanitizes cwd by replacing non-alphanumerics with dashes,
        # uses that as project dir under ~/.claude/projects/.
        sanitized = re.sub(r'[^a-zA-Z0-9]', '-', self._cwd)
        path = os.path.join(os.path.expanduser("~"), ".claude", "projects", sanitized, f"{session_id}.jsonl")
        return path if os.path.isfile(path) else None

    @staticmethod
    def _canon_order(uuids: list[str], result_uuids: set[str]) -> list[str]:
        """Канонизирует порядок: внутри каждого максимального подряд идущего прогона
        tool_result'ов UUID-ы сортируются. Порядок параллельных tool_result'ов не
        определён (claude отдаёт и флашит их в файл в произвольном порядке), поэтому
        обе стороны приводим к одному виду — иначе строгое сравнение даёт ложный
        desync. Не-результаты (tool_use, текст, user-сообщения) остаются на месте:
        их перестановка — реальный рассинхрон."""
        out: list[str] = []
        run: list[str] = []
        for u in uuids:
            if u in result_uuids:
                run.append(u)
            else:
                out.extend(sorted(run)); run.clear()
                out.append(u)
        out.extend(sorted(run))
        return out

    async def _sync_claude_session(self, session_id: str) -> int:
        """Синкает Claude jsonl с нашей памятью. Возвращает кол-во удалённых строк.

        Легитимных состояний всего два:
          - Наша память пуста (one-shot start) — ничего не делаем.
          - UUID-ы нашей памяти, которые есть в Claude jsonl, идут там тем же порядком
            до конца jsonl — с точностью до перестановки внутри кластера параллельных
            tool_result'ов (их порядок не определён, см. _canon_order). Перед ними у
            Claude могут быть старые extras (которые мы уже выкинули) — если их >20, режем.

        UUID-ы нашей памяти, которых НЕТ в Claude jsonl, считаем nested-sub-agent
        activity (например, Claude Code Agent-tool эмитит вложенные turn'ы — мы их
        пишем в память, а parent jsonl их не знает). Такие просто пропускаем
        при выравнивании.

        Любой другой расклад (UUID есть, но в неправильном порядке; наш UUID есть
        в jsonl, но и в jsonl что-то лишнее перед/после него) — баг pipeline.
        Громкий warn + notify через transport.
        """
        jsonl_path = self._claude_session_jsonl(session_id)

        our_uuids_all = [t["_uuid"] for t in self.agent.memory._turns
                         if isinstance(t, dict) and t.get("_uuid")]
        if not our_uuids_all:
            return 0  # one-shot start, легитимно

        if not jsonl_path:
            await self._notify_desync(
                f"в памяти {len(our_uuids_all)} турнов с UUID, но Claude jsonl отсутствует",
            )
            return 0

        with open(jsonl_path, encoding="utf-8") as f:
            lines = f.readlines()

        # Все uuid-bearing entries из jsonl в порядке файла, любого type
        # (user/assistant/system/...). /context-команда и подобное приходит как
        # type="system" с uuid — раньше мы их фильтровали и получали false-positive.
        #
        # Порядок — строго файловый, без сортировки по timestamp. У tool_result
        # timestamp — это время ЗАВЕРШЕНИЯ: долгий тул (sandbox_exec с таймаутом
        # 120с) рядом с мгновенным фейлом получает timestamp на минуты позже, хотя
        # запрошен раньше — сортировка по нему переставила бы пару относительно
        # памяти и дала ложный desync. Сам порядок параллельных tool_result'ов не
        # определён: claude и стримит, и флашит их в файл в произвольном порядке.
        # Поэтому ниже сравниваем кластеры результатов как множество (_canon_order),
        # а не как последовательность.
        claude_seq: list[tuple[int, str]] = []  # (line_idx, uuid)
        claude_result_uuids: set[str] = set()
        for i, line in enumerate(lines):
            try: entry = json.loads(line)
            except json.JSONDecodeError: continue
            uid = entry.get("uuid")
            if not uid: continue
            claude_seq.append((i, uid))
            if entry.get("type") == "user":
                content = (entry.get("message") or {}).get("content")
                if isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content
                ):
                    claude_result_uuids.add(uid)

        claude_uuids_set = {u for _, u in claude_seq}
        our_uuids_set = set(our_uuids_all)
        # UUID-ы tool_result'ов — порядок внутри их кластера не значим. Берём с обеих
        # сторон (role=="tool" в памяти, блок tool_result в jsonl); классификации
        # совпадают, объединение — на случай если одна сторона чего-то не распознала.
        result_uuids = claude_result_uuids | {
            t["_uuid"] for t in self.agent.memory._turns
            if isinstance(t, dict) and t.get("role") == "tool" and t.get("_uuid")
        }

        # Сводим обе стороны к пересечению UUID-ов:
        #   our_synced   — UUID-ы нашей памяти, известные Claude (в нашем порядке).
        #   claude_synced_seq — entries Claude'a, известные нашей памяти (в порядке jsonl).
        # Они должны быть равны как списки. Лишние entries Claude'а, которых у нас
        # нет с UUID (реальные user-сообщения, system-entries которые мы не пишем),
        # как и наши UUID-ы которых нет у Claude (nested sub-agent activity), просто
        # выпадают из выравнивания.
        our_synced = [u for u in our_uuids_all if u in claude_uuids_set]
        claude_synced_seq = [(idx, u) for idx, u in claude_seq if u in our_uuids_set]
        claude_synced = [u for _, u in claude_synced_seq]
        nested_count = len(our_uuids_all) - len(our_synced)

        if not our_synced:
            await self._notify_desync(
                f"ни один из {len(our_uuids_all)} UUID нашей памяти не найден "
                f"в Claude jsonl ({len(claude_seq)} uuid-entries)",
            )
            return 0

        # Канонизируем порядок внутри кластеров tool_result'ов (см. _canon_order),
        # чтобы их недетерминированная перестановка не считалась рассинхроном.
        our_canon = self._canon_order(our_synced, result_uuids)
        claude_canon = self._canon_order(claude_synced, result_uuids)
        for i in range(min(len(our_canon), len(claude_canon))):
            if our_canon[i] != claude_canon[i]:
                await self._notify_desync(
                    f"расхождение UUID на synced-позиции {i}: "
                    f"наш={our_canon[i][:8]}, claude={claude_canon[i][:8]}",
                )
                return 0

        if len(our_synced) != len(claude_synced):
            # При совпавшем prefix лишние с одной стороны = одна из «сторона ушла вперёд».
            ahead = "мы" if len(our_synced) > len(claude_synced) else "Claude"
            diff = abs(len(our_synced) - len(claude_synced))
            await self._notify_desync(
                f"{ahead} {('ушли' if ahead == 'мы' else 'ушёл')} вперёд на {diff} synced-турнов",
            )
            return 0

        # Норма: our_synced == claude_synced. Режем старые extras Claude-jsonl
        # ПЕРЕД первым общим UUID — это турны, которые мы давно выкинули из памяти.
        # claude_synced_seq в файловом порядке, так что первый общий UUID — [0].
        first_match_line = claude_synced_seq[0][0]
        # Считаем uuid-bearing entries до first_match_line, чьи UUID-ы НЕ в нашей
        # памяти (значит реально устаревшие, а не текущие user-сообщения посередине).
        pre_count = sum(
            1 for idx, u in claude_seq
            if idx < first_match_line and u not in our_uuids_set
        )
        if pre_count <= 20:
            return 0

        new_lines = []
        for line_idx, line in enumerate(lines):
            try: entry = json.loads(line)
            except json.JSONDecodeError:
                new_lines.append(line); continue
            uid = entry.get("uuid")
            # No-uuid строки (queue-op/last-prompt) и строки чьи UUID в нашей памяти
            # — оставляем всегда. Чужие UUID до first_match_line — режем (это старое,
            # что мы выкинули). Чужие UUID после — оставляем (реальные user-сообщения).
            if uid is None or uid in our_uuids_set or line_idx >= first_match_line:
                new_lines.append(line)

        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        pruned = len(lines) - len(new_lines)
        log.info(
            "[claude_agent] sync: pruned %d → %d lines from %s (nested=%d)",
            len(lines), len(new_lines), session_id, nested_count,
        )
        return pruned

    async def _notify_desync(self, reason: str):
        msg = (
            f"Claude session разошёлся с памятью: {reason}.\n"
            "Скорее всего сломан memory-pipeline (мутация турнов / обход PreCompact / "
            "crash при стриме). Состояние Claude может быть неконсистентным."
        )
        log.warning("[claude_agent] DESYNC: %s", reason)
        # Эфемерный агент (нет agent_dir) — transport мог быть заглушкой и сообщение
        # уйдёт в никуда. Не дадим тихо продолжать с поломанным состоянием — бросаем.
        if not self.agent.memory.memory_dir:
            raise RuntimeError(f"⚠️ {msg}")
        await self.agent.transport.send_memory_info(f"⚠️ {msg}")

    @property
    def _state_file(self) -> str | None:
        if not self.agent.memory.memory_dir:
            return None
        tid = self.agent.thread_id
        fname = f"CLAUDE_{tid}.json" if tid else "CLAUDE.json"
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

    @staticmethod
    async def _build_user_query(pending: list):
        """Собирает pending user-турны (OpenAI-формат памяти) в один структурированный
        SDK-сообщение в Anthropic-формате и отдаёт async-итератором — claude SDK
        принимает только str или AsyncIterable[dict]. Поддерживает text +
        image_url (`data:<media>;base64,...`)."""
        blocks: list[dict] = []
        for t in pending:
            content = t.get("content")
            if isinstance(content, str):
                if content:
                    blocks.append({"type": "text", "text": content})
                continue
            for b in content or ():
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text"):
                    blocks.append({"type": "text", "text": b["text"]})
                elif b.get("type") == "image_url":
                    url = (b.get("image_url") or {}).get("url", "")
                    if url.startswith("data:") and "," in url:
                        head, data = url.split(",", 1)
                        media_type = head[5:].split(";")[0] or "image/png"
                        blocks.append({"type": "image", "source": {
                            "type": "base64", "media_type": media_type, "data": data,
                        }})
        yield {
            "type": "user",
            "message": {"role": "user", "content": blocks},
            "parent_tool_use_id": None,
        }

    async def close(self):
        # SDK на disconnect утекает CancelledError из своего anyio cancel_scope —
        # поглощаем локально.
        if self._client:
            with suppress(Exception, asyncio.CancelledError):
                await self._client.disconnect()
            self._client = None
            self._client_append = None

    async def llm(self, tool_choice: str = None, parallel_tool_calls: bool = None,
                  temperature: float = 1.0, max_tokens: int | None = None,
                  system_prompt: str | None = None):
        """Запускает claude (resume или fresh) с текущим OM_turn + skill-context'ами
        как append-system-prompt. Стримит ответ, на каждый content_block пишет turn
        в self.agent.memory с _uuid из claude jsonl. Возвращает финальный turn."""
        agent = self.agent
        user_text = agent.memory.last_user_query()

        state = self._load_state()
        session_id = state.get("session_id") or str(uuid.uuid4())

        # Если у claude'а в jsonl есть старые turn'ы которых уже нет в нашей
        # памяти — вычищаем (наша компрессия должна резать клода тоже, иначе
        # его собственный autocompact пробьётся и стирает почти всё).
        pruned = await self._sync_claude_session(session_id) if state.get("created") else 0
        if pruned:
            if self._client:
                with suppress(Exception, asyncio.CancelledError):
                    await self._client.disconnect()
                self._client = None
                self._client_append = None
            await agent.transport.send_memory_info(
                f"Синхронизировал claude-сессию: вычистил {pruned} старых записей",
            )

        parts = []
        for skill in agent.skills:
            ctx = await skill.get_context_prompt(user_text)
            if ctx:
                parts.append(ctx)
        if system_prompt:
            parts.append(system_prompt)
        append_text = "\n\n".join(p for p in parts if p)

        skills_fp = self._skills_fingerprint()

        # Переподнимаем клиент если системка ИЛИ набор тулов изменились — claude
        # читает --append-system-prompt-file и mcp_servers только при старте процесса.
        prompt_changed = self._client and self._client_append != append_text
        skills_changed = self._client and self._client_skills_fp != skills_fp
        if prompt_changed or skills_changed:
            reason = "system prompt" if prompt_changed else "skills set"
            log.info("[claude_agent] %s changed, recreating client", reason)
            with suppress(Exception, asyncio.CancelledError):
                await self._client.disconnect()
            self._client = None
            self._client_append = None
            self._client_skills_fp = None
            if skills_changed:
                self._mcp_server = None  # пересобрать на новых тулах

        if self._client is None:
            extra_args = {}
            if append_text:
                # Временный файл — claude читает его только при старте процесса,
                # после connect() он не нужен. Не зависит от agent.memory.memory_dir,
                # поэтому работает и для эфемерных агентов.
                fd, append_path = tempfile.mkstemp(suffix=".md", prefix="claude_append_")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(append_text)
                extra_args["append-system-prompt-file"] = append_path

            def _on_stderr(line: str):
                line = line.rstrip()
                if line:
                    log.warning("[claude_agent:stderr] %s", line)

            if self._mcp_server is None:
                self._mcp_server = self._build_mcp_server()

            # Дефолт — голый клод (без claude_code preset, setting_sources,
            # встроенных тулов). claude_code mode добавляет всё это через
            # sdk_options в ClaudeCodeSkill.
            options_kwargs = {
                "permission_mode": "bypassPermissions",
                "cwd": self._cwd,
                "model": agent.model_name,
                "include_partial_messages": True,
                "system_prompt": None,
                "setting_sources": [],
                "settings": '{"attribution":{"commit":""}}',
                "tools": [],
                "max_turns": agent.max_iterations,
                "thinking": {"type": "adaptive", "display": "summarized"},
                "extra_args": extra_args,
                "mcp_servers": {"slon": self._mcp_server} if self._mcp_server else {},
                "stderr": _on_stderr,
                # SDK дефолт 1MB. С include_partial_messages клод эхает в stdout
                # накопленный контент (включая base64 картинок из tool-result'ов)
                # — на ~700KB изображении одна partial-строка переваливает 1MB.
                "max_buffer_size": 10 * 1024 * 1024,
            }
            options_kwargs.update(self._sdk_options)

            # Гасим non-essential traffic клода (генерация заголовка треда — лишний
            # haiku-вызов на каждый ход, плюс телеметрия/автоапдейт): headless-агенту
            # это не нужно.
            # Отключаем фоновые задачи: CLI сам уводит долгие Bash-команды в фон
            # (backgroundTaskId), а их завершение не доходит до slonagent-лупа (llm()
            # выходит на ResultMessage и стрим в простое не слушается), плюс процесс
            # гибнет при пересоздании клиента. Долгое — синхронно или через
            # sandbox_exec(background=true) в контейнере.
            # AUTO_MEMORY: у слона своя память (LOG.md/facts.db). Клодовская auto-memory
            # (~/.claude/projects/<cwd>/memory/MEMORY.md) иначе течёт в эфемерных
            # экстракт-агентов и порождает конфабуляции (несуществующий kling_audio.py).
            # Мёржим после user-overrides, юзер может переопределить.
            options_kwargs["env"] = {
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                **options_kwargs.get("env", {}),
            }

            # Слон управляет компрессией через LogCompressor — клодовский autocompact
            # писал бы свой dumb-summary поверх нашего OM. Блокируем после user overrides
            # и рядом с возможными user-хуками.
            async def block_compact(*_): return {"decision": "block", "systemMessage": "Compaction handled by slon"}

            existing_hooks = options_kwargs.get("hooks") or {}
            options_kwargs["hooks"] = {
                **existing_hooks,
                "PreCompact": [*(existing_hooks.get("PreCompact") or []), HookMatcher(hooks=[block_compact])],
            }

            async def _connect(use_resume: bool):
                opts = dict(options_kwargs)
                opts["resume" if use_resume else "session_id"] = session_id
                log.info(
                    "[claude_agent] %s claude (session=%s)",
                    "resuming" if use_resume else "spawning fresh", session_id,
                )
                self._client = ClaudeSDKClient(options=ClaudeAgentOptions(**opts))
                await self._client.connect()

            try:
                await _connect(use_resume=bool(state.get("created")))
            except Exception:
                self._client = None
                await _connect(use_resume=not state.get("created"))

            self._client_append = append_text
            self._client_skills_fp = skills_fp
            self._save_state({"session_id": session_id, "created": True})
        else:
            log.info("[claude_agent] reusing live claude")

        pending = []
        contents = await agent.memory.get_contents()
        for t in reversed(contents):
            if not isinstance(t, dict) or t.get("role") != "user": break
            pending.insert(0, t)
        pending = self.agent.strip_contents_private(pending)

        if not pending:
            raise RuntimeError("ClaudeBackend.llm(): нет user-турнов в хвосте памяти — нечего слать")

        log.info("[claude_agent] query: %r (%d pending turns)", user_text[:80], len(pending))
        await self._client.query(self._build_user_query(pending), session_id=session_id)

        # Стримим. StreamEvent → UI. AssistantMessage / UserMessage — собираем
        # turn'ы в list. agent.loop запишет всё в memory и решит по tool_calls
        # последнего turn'а нужен ли внешний tool-dispatch (нам не нужен — клод
        # отрабатывает тулы сам через MCP).
        text_buf = ""
        text_stream_id = None
        thinking_buf = ""
        thinking_stream_id = None
        tool_use_names: dict[str, str] = {}
        block_type = None
        turns: list[dict] = []

        try:
            async for message in self._client.receive_response():
                if isinstance(message, StreamEvent):
                    event = message.event
                    etype = event.get("type", "")
                    if etype == "content_block_start":
                        block_type = event.get("content_block", {}).get("type")
                        if block_type == "text":
                            text_buf = ""
                            text_stream_id = id(event)
                        elif block_type == "thinking":
                            thinking_buf = ""
                            thinking_stream_id = id(event)
                    elif etype == "content_block_delta":
                        delta = event.get("delta", {})
                        dtype = delta.get("type")
                        if dtype == "text_delta":
                            text_buf += delta.get("text", "")
                            await agent.transport.send_message(text_buf, stream_id=text_stream_id, final=False)
                        elif dtype == "thinking_delta":
                            chunk = delta.get("thinking", "")
                            if chunk:
                                thinking_buf += chunk
                                await agent.transport.send_thinking(thinking_buf, stream_id=thinking_stream_id)
                    elif etype == "content_block_stop":
                        if block_type == "text":
                            await agent.transport.send_message(text_buf, stream_id=text_stream_id, final=True)
                            text_stream_id = None
                        elif block_type == "thinking":
                            if thinking_buf:
                                await agent.transport.send_thinking(thinking_buf, stream_id=thinking_stream_id, final=True)
                            thinking_stream_id = None
                        block_type = None

                elif isinstance(message, AssistantMessage):
                    # Сообщения с parent_tool_use_id — изнутри встроенного Agent-тула
                    # (subagent). Их в parent's turns не пишем (иначе память засоряется
                    # действиями, которые делал не сам родитель), но в transport шлём
                    # — для UI. У субагента StreamEvent'ов нет, текст приходит цельным
                    # TextBlock'ом, так что send_message делаем здесь.
                    is_sub = message.parent_tool_use_id is not None
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            if is_sub:
                                await agent.transport.send_message(block.text)
                            else:
                                turns.append({
                                    "role": "assistant",
                                    "content": block.text,
                                    "_uuid": message.uuid,
                                })
                        elif isinstance(block, ToolUseBlock):
                            # MCP-обёртки наших скиллов прилетают как mcp__slon__<name>,
                            # внутренний учёт ведём по короткому имени.
                            name = block.name.removeprefix("mcp__slon__")
                            await agent.transport.on_tool_call(name, block.input)
                            tool_use_names[block.id] = name
                            if not is_sub:
                                turns.append({
                                    "role": "assistant",
                                    "tool_calls": [{
                                        "id": block.id,
                                        "type": "function",
                                        "function": {
                                            "name": name,
                                            "arguments": json.dumps(block.input, ensure_ascii=False),
                                        },
                                    }],
                                    "_uuid": message.uuid,
                                })
                        # ThinkingBlock — пропускаем

                elif isinstance(message, UserMessage):
                    is_sub = message.parent_tool_use_id is not None
                    for block in message.content:
                        if isinstance(block, ToolResultBlock):
                            raw = block.content or ""
                            if isinstance(raw, list):
                                result = "\n".join(b.get("text", "") for b in raw if isinstance(b, dict) and b.get("type") == "text")
                            else:
                                result = raw
                            await agent.transport.on_tool_result(tool_use_names.get(block.tool_use_id, block.tool_use_id), result)
                            if not is_sub:
                                turns.append({
                                    "role": "tool",
                                    "tool_call_id": block.tool_use_id,
                                    "name": tool_use_names.get(block.tool_use_id, block.tool_use_id),
                                    "content": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False),
                                    "_uuid": message.uuid,
                                })

                elif isinstance(message, SystemMessage):
                    # Fable рефузит кибербез/био-контент, Claude Code молча
                    # ретраит на fallback-модели (обычно opus). Без уведомления
                    # юзер не понимает, почему отвечает другая (дорогая) модель.
                    if message.subtype == "model_refusal_fallback":
                        orig = message.data.get("originalModel", agent.model_name)
                        fb = message.data.get("fallbackModel", "?")
                        log.warning("[claude_agent] %s refused (safety), fell back to %s", orig, fb)
                        await agent.transport.send_message(
                            f"⚠️ {orig} отказался отвечать (safety-фильтр) — Claude Code переключился на {fb}"
                        )

                elif isinstance(message, ResultMessage):
                    cost = f"${message.total_cost_usd:.4f}" if message.total_cost_usd else "n/a"
                    usage = message.model_usage or {}
                    # Ответившая модель — с наибольшим output (haiku-side-call мелкий).
                    # Видно сразу, ушёл ли ход на fable или на opus-fallback.
                    primary = max(usage, key=lambda m: usage[m].get("outputTokens", 0), default="?")
                    log.info("[claude_agent] done: %d turns, %s", message.num_turns, cost)
                    log.info("[claude_agent] model_usage: %s", message.model_usage)
                    await agent.transport.send_message(
                        f"✅ Готово ({message.num_turns} turns, {primary.removeprefix('claude-')}, {cost})"
                    )
                    return turns
        except asyncio.CancelledError:
            asyncio.current_task().uncancel()
            # SDK сам не отменяет активные MCP handler tasks на interrupt()
            # (только на disconnect) — отменяем сами, иначе sandbox_exec и
            # прочие тулы крутятся в фоне после stop'а.
            for t in list(self._active_tool_tasks):
                t.cancel()
            with suppress(Exception, asyncio.CancelledError):
                await self._client.interrupt()
            # SDK после interrupt оставляет финальный ResultMessage в своей очереди.
            # Не вычитаешь — он всплывёт в receive_response() следующего llm() и
            # терминирует новый запрос до того как тот получит ответ.
            cancelled_result: ResultMessage | None = None
            async def _drain():
                nonlocal cancelled_result
                async for _msg in self._client.receive_response():
                    if isinstance(_msg, ResultMessage):
                        cancelled_result = _msg
            with suppress(Exception, asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(_drain(), timeout=10.0)
            if cancelled_result is not None:
                cost = (f"${cancelled_result.total_cost_usd:.4f}"
                        if cancelled_result.total_cost_usd else "n/a")
                await agent.transport.send_message(
                    f"⚠️ Прервано ({cancelled_result.num_turns} turns, {cost})",
                )
            # Pair any dangling tool_calls with synthetic results — иначе
            # turns несбалансированы: assistant.tool_calls без matching
            # role=tool ломает компрессию и openai-style вызовы.
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

        return turns
