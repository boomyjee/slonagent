"""ClaudeAgent — Agent с llm() через claude_agent_sdk.ClaudeSDKClient.

Вместо OpenAI-совместимого chat.completions использует Claude Code CLI
как backend. Каждый content_block ответа пишется в self.memory отдельным
turn'ом (как клод хранит в своей сессионной jsonl).
"""
import asyncio
import json
import logging
import os
import uuid
from contextlib import suppress

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SdkMcpTool,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
)

from src.agent.agent import Agent

log = logging.getLogger(__name__)


class ClaudeAgent(Agent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._cwd = os.path.join(self.memory.memory_dir, "workspace")
        os.makedirs(self._cwd, exist_ok=True)
        self._client: ClaudeSDKClient | None = None
        self._client_append: str | None = None  # текст системки в живом клиенте
        self._mcp_server = None  # построится лениво из self.skills

    def _build_mcp_server(self):
        """Оборачивает тулы своих скиллов в SDK MCP-сервер. Клод увидит как mcp__slon__*."""
        sdk_tools: list[SdkMcpTool] = []
        for skill in self.skills:
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

    @property
    def _state_file(self):
        return os.path.join(self.memory.memory_dir, "claude_code.json")

    def _load_state(self) -> dict:
        try:
            with open(self._state_file) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_state(self, state: dict):
        with open(self._state_file, "w") as f:
            json.dump(state, f)

    async def close(self):
        # SDK на disconnect утекает CancelledError из своего anyio cancel_scope —
        # поглощаем локально.
        if self._client:
            with suppress(Exception, asyncio.CancelledError):
                await self._client.disconnect()
            self._client = None
            self._client_append = None
        await super().close()

    async def llm(self, tool_choice: str = None, parallel_tool_calls: bool = None):
        """Запускает claude (resume или fresh) с текущим OM_turn + skill-context'ами
        как append-system-prompt. Стримит ответ, на каждый content_block пишет turn
        в self.memory с _uuid из claude jsonl. Возвращает финальный turn для loop().
        """
        # Извлекаем последний user-текст из current_content_parts (Agent.next_message)
        user_text = " ".join(
            p.get("text", "") for p in (self._current_content_parts or [])
            if isinstance(p, dict) and "text" in p
        ).strip()

        state = self._load_state()
        session_id = state.get("session_id") or str(uuid.uuid4())

        # Триггер компрессии — обновит OM_turn в _turns
        await self.memory.get_contents()

        # Динамический контекст: OM_turn + context_prompts всех скиллов
        parts = []
        for t in self.memory._turns:
            if isinstance(t, dict) and t.get("_observation_message"):
                parts.append(t.get("content", ""))
                break
        for skill in self.skills:
            ctx = await skill.get_context_prompt(user_text)
            if ctx:
                parts.append(ctx)
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
            append_path = os.path.join(self.memory.memory_dir, "claude_append.md")
            extra_args = {}
            if append_text:
                with open(append_path, "w", encoding="utf-8") as f:
                    f.write(append_text)
                extra_args["append-system-prompt-file"] = append_path

            def _on_stderr(line: str):
                line = line.rstrip()
                if line:
                    log.warning("[claude_agent:stderr] %s", line)

            if self._mcp_server is None:
                self._mcp_server = self._build_mcp_server()

            options_kwargs = dict(
                permission_mode="bypassPermissions",
                cwd=self._cwd,
                model=self.model_name,
                include_partial_messages=True,
                setting_sources=["user"],
                system_prompt={"type": "preset", "preset": "claude_code"},
                extra_args=extra_args,
                mcp_servers={"slon": self._mcp_server} if self._mcp_server else {},
                stderr=_on_stderr,
            )

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

        log.info("[claude_agent] query: %r", user_text[:80])
        await self._client.query(user_text, session_id=session_id)

        # Стримим. StreamEvent → UI. AssistantMessage / UserMessage — собираем
        # turn'ы в list. agent.loop запишет всё в memory и решит по tool_calls
        # последнего turn'а нужен ли внешний tool-dispatch (нам не нужен — клод
        # отрабатывает тулы сам через MCP).
        text_stream_id = None
        thinking_stream_id = None
        tool_use_names: dict[str, str] = {}
        block_type = None
        turns: list[dict] = []

        async for message in self._client.receive_response():
            if isinstance(message, StreamEvent):
                event = message.event
                etype = event.get("type", "")
                if etype == "content_block_start":
                    block_type = event.get("content_block", {}).get("type")
                    if block_type == "thinking":
                        thinking_stream_id = id(event)
                elif etype == "content_block_delta":
                    delta = event.get("delta", {})
                    dtype = delta.get("type")
                    if dtype == "text_delta":
                        if text_stream_id is None:
                            text_stream_id = id(delta)
                        await self.transport.send_message(delta.get("text", ""), stream_id=text_stream_id)
                    elif dtype == "thinking_delta":
                        await self.transport.send_thinking(delta.get("thinking", ""), stream_id=thinking_stream_id)
                elif etype == "content_block_stop":
                    if block_type == "text":
                        text_stream_id = None
                    elif block_type == "thinking":
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
                        await self.transport.on_tool_call(block.name, block.input)
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
                        result = block.content or ""
                        await self.transport.on_tool_result(block.tool_use_id, result)
                        turns.append({
                            "role": "tool",
                            "tool_call_id": block.tool_use_id,
                            "name": tool_use_names.get(block.tool_use_id, ""),
                            "content": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False),
                            "_uuid": message.uuid,
                        })

            elif isinstance(message, ResultMessage):
                cost = f"${message.total_cost_usd:.4f}" if message.total_cost_usd else "n/a"
                log.info("[claude_agent] done: %d turns, %s", message.num_turns, cost)
                return turns

        return turns
