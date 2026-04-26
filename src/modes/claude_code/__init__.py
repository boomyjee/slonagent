import asyncio
import json
import logging
import os
import uuid
from typing import Annotated

from agent import Skill, tool, bypass
from claude_agent_sdk import (
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    SdkMcpTool,
    StreamEvent,
    ToolResultBlock,
    UserMessage,
    create_sdk_mcp_server,
    query,
)

log = logging.getLogger(__name__)


class ClaudeCodeSkill(Skill):
    def __init__(self, cli_path: str = "", model: str = "", expose_tool: bool = True):
        super().__init__()
        self._cli_path = cli_path or None
        self._model = model or None
        self._expose_tool = expose_tool

    def get_tools(self):
        return super().get_tools() if self._expose_tool else []

    @property
    def _state_file(self):
        return os.path.join(self.agent.memory.memory_dir, "claude_code.json")

    def _get_session_id(self) -> str:
        try:
            with open(self._state_file) as f:
                sid = json.load(f).get("session_id", "")
                if sid:
                    return sid
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        sid = str(uuid.uuid4())
        with open(self._state_file, "w") as f:
            json.dump({"session_id": sid}, f)
        return sid

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
        session_id = self._get_session_id()
        log.info("[claude_code] query start: %r (cwd=%s, session=%s)", text[:80], cwd, session_id)

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

        # Пишем в файл и передаём --append-system-prompt-file: inline-аргументом
        # переполняется Windows cmdline (CreateProcess лимит 32K → WinError 206).
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

        # 3. options с фиксированным session_id (наш UUID, переживает рестарты)
        options = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            cwd=cwd,
            cli_path=self._cli_path,
            model=self._model,
            include_partial_messages=True,
            setting_sources=["user"],
            session_id=session_id,
            system_prompt={"type": "preset", "preset": "claude_code"},
            extra_args=extra_args,
            mcp_servers={"slon": mcp_server} if mcp_server else {},
            stderr=_on_stderr,
        )

        # 4. стримим query() — каждый user-message = свежий процесс claude с актуальным system_prompt
        text_buf = ""
        text_stream_id = None
        thinking_buf = ""
        thinking_stream_id = None
        tool_input_buf = ""
        tool_name = ""
        block_type = None
        assistant_parts: list[str] = []   # text + tool_use, для сохранения как assistant turn

        async for message in query(prompt=text, options=options):
            if isinstance(message, StreamEvent):
                event = message.event
                etype = event.get("type", "")

                if etype == "content_block_start":
                    block = event.get("content_block", {})
                    block_type = block.get("type")
                    if block_type == "tool_use":
                        if text_buf:
                            await transport.send_message(text_buf, stream_id=text_stream_id)
                            assistant_parts.append(text_buf)
                            text_buf = ""
                            text_stream_id = None
                        tool_name = block.get("name", "?")
                        tool_input_buf = ""
                    elif block_type == "thinking":
                        thinking_buf = ""
                        thinking_stream_id = id(event)

                elif etype == "content_block_delta":
                    delta = event.get("delta", {})
                    dtype = delta.get("type")
                    if dtype == "text_delta":
                        text_buf += delta.get("text", "")
                        if text_stream_id is None:
                            text_stream_id = id(delta)
                        await transport.send_message(text_buf, stream_id=text_stream_id)
                    elif dtype == "thinking_delta":
                        thinking_buf += delta.get("thinking", "")
                        await transport.send_thinking(thinking_buf, stream_id=thinking_stream_id)
                    elif dtype == "input_json_delta":
                        tool_input_buf += delta.get("partial_json", "")

                elif etype == "content_block_stop":
                    if block_type == "tool_use":
                        try:
                            args = json.loads(tool_input_buf) if tool_input_buf else {}
                        except json.JSONDecodeError:
                            args = {"raw": tool_input_buf}
                        await transport.on_tool_call(tool_name, args)
                        assistant_parts.append(
                            f"[tool_use {tool_name}] {json.dumps(args, ensure_ascii=False)}"
                        )
                        tool_name = ""
                        tool_input_buf = ""
                    elif block_type == "thinking" and thinking_buf:
                        await transport.send_thinking(thinking_buf, stream_id=thinking_stream_id, final=True)
                        # thinking в shadow.memory не сохраняем (внутренний монолог)
                        thinking_buf = ""
                        thinking_stream_id = None
                    block_type = None

            elif isinstance(message, UserMessage):
                tr_parts: list[str] = []
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        result = block.content or ""
                        await transport.on_tool_result(block.tool_use_id, result)
                        tr_parts.append(f"[tool_result] {result}")
                if tr_parts:
                    await shadow.memory.add_turn({
                        "role": "user",
                        "content": "\n".join(tr_parts),
                    })

            elif isinstance(message, AssistantMessage):
                if text_buf:
                    await transport.send_message(text_buf, stream_id=text_stream_id)
                    assistant_parts.append(text_buf)
                    text_buf = ""
                    text_stream_id = None

            elif isinstance(message, ResultMessage):
                cost = f"${message.total_cost_usd:.4f}" if message.total_cost_usd else "n/a"
                log.info("[claude_code] done: %d turns, %s", message.num_turns, cost)
                await transport.send_message(f"✅ Готово ({message.num_turns} turns, {cost})")
                if assistant_parts:
                    await shadow.memory.add_turn({
                        "role": "assistant",
                        "content": "\n\n".join(assistant_parts),
                    })
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
                        # _send_query крутится в отдельной таске чтобы SDK-шный
                        # cancel_scope (anyio task-group в transport.close) не утекал
                        # в наш launch-цикл при aclose async-генератора query().
                        await asyncio.create_task(
                            self._send_query(shadow, text, cwd, mcp_server=mcp_server)
                        )
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
            coding_transport.cleanup()

        return {"status": "finished"}
