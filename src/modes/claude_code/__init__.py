import asyncio
import json
import logging
import os
import uuid
from contextlib import suppress
from typing import Annotated

from agent import Skill, tool, bypass
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

log = logging.getLogger(__name__)


class ClaudeCodeSkill(Skill):
    def __init__(self, cli_path: str = "", model: str = "", expose_tool: bool = True):
        super().__init__()
        self._cli_path = cli_path or None
        self._model = model or None
        self._expose_tool = expose_tool
        self._client: ClaudeSDKClient | None = None
        self._client_append: str | None = None  # текст системки в живом клиенте

    def get_tools(self):
        return super().get_tools() if self._expose_tool else []

    @property
    def _state_file(self):
        return os.path.join(self.agent.memory.memory_dir, "claude_code.json")

    def _load_state(self) -> dict:
        try:
            with open(self._state_file) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_state(self, state: dict):
        with open(self._state_file, "w") as f:
            json.dump(state, f)

    def _build_mcp_server(self, shadow):
        """Прокидывает тулы shadow-скиллов в Claude Code как SDK MCP-сервер.

        Каждый OpenAI-format декларатор оборачивается в SdkMcpTool, handler
        делегирует skill.dispatch_tool_call. Имена тулов остаются как есть —
        Claude Code увидит их как mcp__slon__<name>.
        """
        sdk_tools: list[SdkMcpTool] = []
        for skill in shadow.skills:
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

    async def _send_query(self, shadow, text: str, cwd: str, mcp_server=None):
        transport = shadow.transport
        state = self._load_state()
        session_id = state.get("session_id") or str(uuid.uuid4())

        # 1. user turn в shadow.memory (до get_contents — чтобы новый ход попал в наблюдения)
        await shadow.memory.add_turn({"role": "user", "content": text})

        # 2. триггер компрессии — обновит OM_turn в _turns
        await shadow.memory.get_contents()

        # 3. собираем дин. контекст: OM_turn компрессора + context_prompts
        #    от всех shadow-skill'ов (FactProvider, PersonalityProvider и т.п.).
        parts = []
        for t in shadow.memory._turns:
            if isinstance(t, dict) and t.get("_observation_message"):
                parts.append(t.get("content", ""))
                break
        for skill in shadow.skills:
            ctx = await skill.get_context_prompt(text)
            if ctx:
                parts.append(ctx)
        append_text = "\n\n".join(p for p in parts if p)

        # 4. ClaudeSDKClient переиспользуется пока append_text тот же. Если изменился —
        #    закрываем старый процесс и поднимаем новый (claude читает --append-system-
        #    prompt-file только при старте процесса, апдейтить файл бесполезно).
        if self._client and self._client_append != append_text:
            log.info("[claude_code] system prompt changed, recreating client")
            # SDK transport.close на disconnect утекает CancelledError в нашу таску.
            # Гасим локально чтоб не прервать обработку текущего юзерского сообщения.
            with suppress(Exception, asyncio.CancelledError):
                await self._client.disconnect()
            self._client = None
            self._client_append = None

        if self._client is None:
            append_path = os.path.join(shadow.memory.memory_dir, "claude_code_append.md")
            extra_args = {}
            if append_text:
                with open(append_path, "w", encoding="utf-8") as f:
                    f.write(append_text)
                extra_args["append-system-prompt-file"] = append_path

            def _on_stderr(line: str):
                line = line.rstrip()
                if line:
                    log.warning("[claude_code:stderr] %s", line)

            # --session-id создаёт новую сессию (ошибка если уже есть),
            # --resume подключается к существующей. Первый запуск → session-id,
            # все последующие — resume.
            options_kwargs = dict(
                permission_mode="bypassPermissions",
                cwd=cwd,
                cli_path=self._cli_path,
                model=self._model,
                include_partial_messages=True,
                setting_sources=["user"],
                system_prompt={"type": "preset", "preset": "claude_code"},
                extra_args=extra_args,
                mcp_servers={"slon": mcp_server} if mcp_server else {},
                stderr=_on_stderr,
            )
            async def _connect(use_resume: bool):
                opts = dict(options_kwargs)
                opts["resume" if use_resume else "session_id"] = session_id
                log.info(
                    "[claude_code] %s claude (session=%s)",
                    "resuming" if use_resume else "spawning fresh", session_id,
                )
                self._client = ClaudeSDKClient(options=ClaudeAgentOptions(**opts))
                await self._client.connect()

            try:
                await _connect(use_resume=bool(state.get("created")))
            except Exception:
                # Резервный путь: state не совпал с реальностью claude (потеря/расход
                # state-файла). Переключаемся на противоположный режим и пробуем ещё раз.
                self._client = None
                await _connect(use_resume=not state.get("created"))

            self._client_append = append_text
            self._save_state({"session_id": session_id, "created": True})
        else:
            log.info("[claude_code] reusing live claude")

        log.info("[claude_code] query: %r", text[:80])
        await self._client.query(text, session_id=session_id)

        # 5. стримим ответ.
        #    StreamEvent → только UI (deltas через transport).
        #    AssistantMessage / UserMessage → запись в shadow.memory вместе с uuid
        #    из claude jsonl (по одному блоку на turn — как клод хранит у себя).
        #    Формат памяти — OpenAI: text → assistant.content, tool_use → tool_calls,
        #    tool_result → role:tool. thinking в memory не пишем (нет в OpenAI).
        text_stream_id = None
        thinking_stream_id = None
        tool_use_names: dict[str, str] = {}  # id → name, для tool_result.name
        block_type = None

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
                        await transport.send_message(delta.get("text", ""), stream_id=text_stream_id)
                    elif dtype == "thinking_delta":
                        await transport.send_thinking(delta.get("thinking", ""), stream_id=thinking_stream_id)
                elif etype == "content_block_stop":
                    if block_type == "text":
                        text_stream_id = None
                    elif block_type == "thinking":
                        thinking_stream_id = None
                    block_type = None

            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        await shadow.memory.add_turn({
                            "role": "assistant",
                            "content": block.text,
                            "_uuid": message.uuid,
                        })
                    elif isinstance(block, ToolUseBlock):
                        await transport.on_tool_call(block.name, block.input)
                        tool_use_names[block.id] = block.name
                        await shadow.memory.add_turn({
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
                    # ThinkingBlock — пропускаем, нет в OpenAI-формате

            elif isinstance(message, UserMessage):
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        result = block.content or ""
                        await transport.on_tool_result(block.tool_use_id, result)
                        await shadow.memory.add_turn({
                            "role": "tool",
                            "tool_call_id": block.tool_use_id,
                            "name": tool_use_names.get(block.tool_use_id, ""),
                            "content": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False),
                            "_uuid": message.uuid,
                        })

            elif isinstance(message, ResultMessage):
                cost = f"${message.total_cost_usd:.4f}" if message.total_cost_usd else "n/a"
                log.info("[claude_code] done: %d turns, %s", message.num_turns, cost)
                await transport.send_message(f"✅ Готово ({message.num_turns} turns, {cost})")
                return

    @bypass("claude", "Запустить Claude Code", standalone=True)
    async def launch_command(self, args: str):
        self.agent.call_before_next_message(self.launch(task=args))

    @bypass("session", "Сбросить Claude Code сессию (shadow.memory не трогается)", standalone=True)
    async def session_command(self, _args: str):
        if os.path.exists(self._state_file):
            os.remove(self._state_file)
        await self.agent.transport.send_message(
            "Session reset — следующий /claude стартует свежую Claude Code-сессию"
        )

    @tool("Запустить Claude Code для работы с кодом в указанной папке")
    async def launch(
        self,
        task: Annotated[str, "Задача для Claude Code"] = "",
        project_path: Annotated[str, "Путь к проекту"] = "",
    ) -> dict:
        from src.modes.coding import CodingTransport
        from src.skills.sandbox import SandboxSkill
        from src.transport.multi import MultiTransport

        sandbox = next((s for s in self.agent.skills if isinstance(s, SandboxSkill)), None)
        if not sandbox:
            return {"error": "Требуется SandboxSkill с Docker-контейнером"}
        cwd = sandbox.resolve_path(project_path or "/workspace")
        log.info("[claude_code] cwd=%s", cwd)

        coding_transport = CodingTransport(project_path or "/workspace")
        coding_transport.resolve_path = sandbox.resolve_path
        coding_transport.workspace_host_dir = sandbox.workspace_dir
        coding_transport.start_watcher()

        # Свежий shadow на каждый launch. Наследуем compressor + providers из главного
        # config'а; skills=[] — выкидываем SandboxSkill/WebSkill/CronSkill/ConfigSkill,
        # они тут только мешали бы (контекст_промптами и тулзами).
        # transport — MultiTransport: shadow'овые tool-events / messages уезжают
        # и в основной transport (Telegram/CLI), и в Coding Web UI.
        shadow = await self.agent.spawn_subagent(
            "claude_code",
            skills=[],
            transport=MultiTransport([self.agent.transport, coding_transport]),
        )
        # Тащим скиллы транспорта (TelegramSkill etc) — обычно их собирает
        # agent.loop(), но shadow с run_loop=False. Чтобы клод мог отправлять
        # файлы/кнопки в TG — делаем это вручную после spawn.
        await shadow.add_transport_skills()

        url = await coding_transport.get_url('/')
        await self.agent.transport.send_message(
            f"\U0001f4bb Claude Code: {url}\nДля выхода: /stop"
        )

        mcp_server = self._build_mcp_server(shadow)

        try:
            text = task
            while True:
                if text:
                    if text.lower() in ("/stop", "/exit", "стоп", "выход"):
                        break
                    await shadow.transport.send_processing(True)
                    try:
                        await self._send_query(shadow, text, cwd, mcp_server=mcp_server)
                    except Exception as e:
                        log.warning("[claude_code] query failed: %s", e, exc_info=True)
                        await shadow.transport.send_message(f"Ошибка: {e}")
                    finally:
                        await shadow.transport.send_processing(False)

                content_parts, _, _ = await shadow.next_message()
                text = " ".join(
                    p.get("text", "") for p in content_parts if isinstance(p, dict)
                ).strip()
        finally:
            if self._client:
                with suppress(Exception, asyncio.CancelledError):
                    await self._client.disconnect()
                self._client = None
                self._client_append = None
            coding_transport.cleanup()

        return {"status": "finished"}
