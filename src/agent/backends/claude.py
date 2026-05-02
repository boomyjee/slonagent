"""ClaudeAgent — Agent с llm() через claude_agent_sdk.ClaudeSDKClient.

Вместо OpenAI-совместимого chat.completions использует Claude Code CLI
как backend. Каждый content_block ответа пишется в self.memory отдельным
turn'ом (как клод хранит в своей сессионной jsonl).
"""
import asyncio
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
        """sdk_options — словарь, который переопределяет дефолтные ключи
        ClaudeAgentOptions перед запросом. Например, для голой модели:
        {'system_prompt': None, 'setting_sources': None, 'tools': []}."""
        super().__init__(agent)
        self._sdk_options = sdk_options or {}
        skill = ClaudeAgentSkill()
        skill.register(self)
        agent.skills.insert(0, skill)
        # cwd живёт под memory_dir (там же claude-сессии и LOG.md). У эфемерного
        # агента (memory_dir пустой) cwd нет — claude запустится в дефолтной
        # директории, persistence нам всё равно не нужен.
        if agent.memory.memory_dir:
            self._cwd = os.path.join(agent.memory.memory_dir, "workspace")
            os.makedirs(self._cwd, exist_ok=True)
        else:
            self._cwd = None
        self._client: ClaudeSDKClient | None = None
        self._client_append: str | None = None  # текст системки в живом клиенте
        self._mcp_server = None  # построится лениво из agent.skills
        # Эфемерный агент: session_id живёт в памяти инстанса, на диск ничего не пишем.
        self._memory_state: dict = {}

    def _build_mcp_server(self):
        """Оборачивает тулы своих скиллов в SDK MCP-сервер. Клод увидит как mcp__slon__*."""
        sdk_tools: list[SdkMcpTool] = []
        for skill in self.agent.skills:
            for decl in skill.get_tools():
                fn = decl["function"]
                name = fn["name"]
                schema = fn.get("parameters") or {"type": "object", "properties": {}}

                async def handler(args, _skill=skill, _name=name):
                    result = await _skill.dispatch_tool_call({
                        "function": {
                            "name": _name,
                            "arguments": json.dumps(args, ensure_ascii=False),
                        }
                    })
                    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
                    return {"content": [{"type": "text", "text": text}]}

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
        # uses that as the project dir under ~/.claude/projects/.
        if not self._cwd:
            return None
        sanitized = re.sub(r'[^a-zA-Z0-9]', '-', self._cwd)
        path = os.path.join(os.path.expanduser("~"), ".claude", "projects", sanitized, f"{session_id}.jsonl")
        return path if os.path.isfile(path) else None

    async def _sync_claude_session(self, session_id: str) -> int:
        """Если у claude'а в jsonl сильно больше старых turn'ов чем у нас в памяти —
        вычищаем лишние. Возвращает кол-во удалённых строк (0 если не трогали)."""
        jsonl_path = self._claude_session_jsonl(session_id)
        if not jsonl_path:
            return 0

        our_uuids = {t["_uuid"] for t in self.agent.memory._turns
                     if isinstance(t, dict) and t.get("_uuid")}
        if not our_uuids:
            return 0
        our_oldest = next(
            (t["_uuid"] for t in self.agent.memory._turns
             if isinstance(t, dict) and t.get("_uuid")), None,
        )

        with open(jsonl_path, encoding="utf-8") as f:
            lines = f.readlines()

        oldest_idx, pre_count = None, 0
        for i, line in enumerate(lines):
            try: entry = json.loads(line)
            except json.JSONDecodeError: continue
            if entry.get("uuid") == our_oldest:
                oldest_idx = i
                break
            if entry.get("type") in ("user", "assistant"):
                pre_count += 1

        if oldest_idx is None or pre_count <= 20:
            return 0

        new_lines = []
        for line in lines:
            try: entry = json.loads(line)
            except json.JSONDecodeError:
                new_lines.append(line)
                continue
            uid = entry.get("uuid")
            if uid is None or uid in our_uuids:
                new_lines.append(line)

        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        pruned = len(lines) - len(new_lines)
        log.info("[claude_agent] sync: pruned %d → %d lines from %s",
                 len(lines), len(new_lines), session_id)
        return pruned

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

        # Триггер компрессии — обновит OM_turn в _turns
        await agent.memory.get_contents()

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
                f"Синхронизировал claude-сессию: вычистил {pruned} старых записей", final=True,
            )

        # Динамический контекст: OM_turn + context_prompts всех скиллов
        parts = []
        for t in agent.memory._turns:
            if isinstance(t, dict) and t.get("_observation_message"):
                parts.append(t.get("content", ""))
                break
        for skill in agent.skills:
            ctx = await skill.get_context_prompt(user_text)
            if ctx:
                parts.append(ctx)
        if system_prompt:
            parts.append(system_prompt)
        append_text = "\n\n".join(p for p in parts if p)

        # Переподнимаем клиент если системка изменилась (claude читает
        # --append-system-prompt-file только при старте процесса).
        if self._client and self._client_append != append_text:
            log.info("[claude_agent] system prompt changed, recreating client")
            with suppress(Exception, asyncio.CancelledError):
                await self._client.disconnect()
            self._client = None
            self._client_append = None

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

            # sdk_options перекрывает дефолтные ключи (например, чтобы выключить
            # claude_code preset для голой модели). dict(**a, **b) на пересечении
            # ключей бросает TypeError, поэтому делаем явный update.
            options_kwargs = {
                "permission_mode": "bypassPermissions",
                "cwd": self._cwd,
                "model": agent.model_name,
                "include_partial_messages": True,
                "setting_sources": ["user"],
                "system_prompt": {"type": "preset", "preset": "claude_code"},
                "extra_args": extra_args,
                "mcp_servers": {"slon": self._mcp_server} if self._mcp_server else {},
                "stderr": _on_stderr,
            }
            options_kwargs.update(self._sdk_options)

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
            self._save_state({"session_id": session_id, "created": True})
        else:
            log.info("[claude_agent] reusing live claude")

        pending = []
        for t in reversed(agent.memory._turns):
            if not isinstance(t, dict) or t.get("role") != "user" or t.get("_observation_message"): break
            pending.insert(0, t)

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
                            await agent.transport.send_message(text_buf, stream_id=text_stream_id)
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
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            turns.append({
                                "role": "assistant",
                                "content": block.text,
                                "_uuid": message.uuid,
                            })
                        elif isinstance(block, ToolUseBlock):
                            await agent.transport.on_tool_call(block.name, block.input)
                            tool_use_names[block.id] = block.name
                            turns.append({
                                "role": "assistant",
                                "tool_calls": [{
                                    "id": block.id,
                                    "type": "function",
                                    "function": {
                                        "name": block.name,
                                        "arguments": json.dumps(block.input, ensure_ascii=False),
                                    },
                                }],
                                "_uuid": message.uuid,
                            })
                        # ThinkingBlock — пропускаем

                elif isinstance(message, UserMessage):
                    for block in message.content:
                        if isinstance(block, ToolResultBlock):
                            raw = block.content or ""
                            if isinstance(raw, list):
                                result = "\n".join(b.get("text", "") for b in raw if isinstance(b, dict) and b.get("type") == "text")
                            else:
                                result = raw
                            await agent.transport.on_tool_result(tool_use_names.get(block.tool_use_id, block.tool_use_id), result)
                            turns.append({
                                "role": "tool",
                                "tool_call_id": block.tool_use_id,
                                "name": tool_use_names.get(block.tool_use_id, block.tool_use_id),
                                "content": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False),
                                "_uuid": message.uuid,
                            })

                elif isinstance(message, ResultMessage):
                    cost = f"${message.total_cost_usd:.4f}" if message.total_cost_usd else "n/a"
                    log.info("[claude_agent] done: %d turns, %s", message.num_turns, cost)
                    await agent.transport.send_message(f"✅ Готово ({message.num_turns} turns, {cost})")
                    return turns
        except asyncio.CancelledError:
            asyncio.current_task().uncancel()
            with suppress(Exception):
                await self._client.interrupt()
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
