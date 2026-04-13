import json
import logging
import os
import uuid
from typing import Annotated

from agent import Skill, tool, bypass
from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    ToolResultBlock,
    UserMessage,
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

    def _load_session_id(self) -> str:
        try:
            with open(self._state_file) as f:
                return json.load(f).get("session_id", "")
        except (FileNotFoundError, json.JSONDecodeError):
            return ""

    def _save_session_id(self, session_id: str):
        with open(self._state_file, "w") as f:
            json.dump({"session_id": session_id}, f)

    def _new_session_id(self) -> str:
        sid = str(uuid.uuid4())
        self._save_session_id(sid)
        return sid

    async def _send_query(self, client, transport, text, session_id="default"):
        await client.query(text, session_id=session_id)
        await transport.send_processing(True)

        text_buf = ""
        text_stream_id = None
        thinking_buf = ""
        thinking_stream_id = None
        tool_input_buf = ""
        tool_name = ""
        block_type = None

        async for message in client.receive_response():
            if isinstance(message, StreamEvent):
                event = message.event
                etype = event.get("type", "")

                if etype == "content_block_start":
                    block = event.get("content_block", {})
                    block_type = block.get("type")

                    if block_type == "tool_use":
                        # Flush text before tool call
                        if text_buf:
                            await transport.send_message(text_buf, stream_id=text_stream_id)
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
                        tool_name = ""
                        tool_input_buf = ""

                    elif block_type == "thinking" and thinking_buf:
                        await transport.send_thinking(thinking_buf, stream_id=thinking_stream_id, final=True)
                        thinking_buf = ""
                        thinking_stream_id = None

                    block_type = None

            elif isinstance(message, UserMessage):
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        result = block.content or ""
                        await transport.on_tool_result(block.tool_use_id, result)

            elif isinstance(message, AssistantMessage):
                if text_buf:
                    await transport.send_message(text_buf, stream_id=text_stream_id)
                    text_buf = ""
                    text_stream_id = None

            elif isinstance(message, ResultMessage):
                await transport.send_processing(False)
                cost = f"${message.total_cost_usd:.4f}" if message.total_cost_usd else "n/a"
                await transport.send_message(f"✅ Готово ({message.num_turns} turns, {cost})")
                return

        await transport.send_processing(False)

    @bypass("claude", "Запустить Claude Code", standalone=True)
    async def launch_command(self, args: str):
        self.agent.call_before_next_message(self.launch(task=args))

    @bypass("session", "Сбросить сессию Claude Code", standalone=True)
    async def session_command(self, args: str):
        sid = self._new_session_id()
        await self.agent.transport.send_message(f"Новая сессия: {sid[:8]}...")

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

        original_transport = self.agent.transport
        multi = MultiTransport([original_transport, coding_transport])
        multi.set_agent(self.agent)
        self.agent.transport = multi
        coding_transport.start_watcher()

        url = await coding_transport.get_url('/')
        await original_transport.send_message(
            f"\U0001f4bb Claude Code: {url}\nДля выхода: /stop"
        )

        options = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            cwd=cwd,
            cli_path=self._cli_path,
            model=self._model,
            include_partial_messages=True,
            setting_sources=["user"],
        )

        session_id = self._load_session_id() or self._new_session_id()

        async with ClaudeSDKClient(options=options) as client:
            if task:
                await self._send_query(client, multi, task, session_id)

            while True:
                content_parts, _, _ = await self.agent.next_message()
                text = " ".join(
                    p.get("text", "") for p in content_parts if isinstance(p, dict)
                ).strip()
                if not text:
                    continue
                if text.lower() in ("/stop", "/exit", "стоп", "выход"):
                    break
                try:
                    await self._send_query(client, multi, text, session_id)
                except Exception as e:
                    log.warning("[claude_code] query failed: %s", e, exc_info=True)
                    await multi.send_message(f"Ошибка: {e}")

        coding_transport.cleanup()
        return {"status": "finished"}
