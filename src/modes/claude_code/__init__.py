import json
import logging
from typing import Annotated

from agent import Skill, tool
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
    def __init__(self, cli_path: str = "", model: str = ""):
        super().__init__()
        self._cli_path = cli_path or None
        self._model = model or None
        self._client: ClaudeSDKClient | None = None

    async def _send_query(self, client, transport, text):
        await client.query(text)
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
                        await transport.send_thinking(thinking_buf, stream_id=thinking_msgs, final=True)
                        thinking_buf = ""
                        thinking_msgs = []

                    block_type = None

            elif isinstance(message, UserMessage):
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        result = block.content or ""
                        await transport.on_tool_result(block.tool_use_id, result)

            elif isinstance(message, AssistantMessage):
                # Flush remaining text
                if text_buf:
                    await transport.send_message(text_buf, stream_id=text_msgs)
                    text_buf = ""
                    text_msgs = []

            elif isinstance(message, ResultMessage):
                await transport.send_processing(False)
                cost = f"${message.total_cost_usd:.4f}" if message.total_cost_usd else "n/a"
                await transport.send_message(f"✅ Готово ({message.num_turns} turns, {cost})")
                return

        await transport.send_processing(False)

    @tool("Запустить Claude Code для работы с кодом в указанной папке")
    async def start_claude_code(
        self,
        task: Annotated[str, "Задача для Claude Code"] = "",
        project_path: Annotated[str, "Путь к проекту"] = "",
    ) -> dict:
        transport = self.agent.transport

        from src.skills.sandbox import SandboxSkill
        sandbox = next((s for s in self.agent.skills if isinstance(s, SandboxSkill)), None)
        if not sandbox:
            return {"error": "Требуется SandboxSkill с Docker-контейнером"}
        cwd = sandbox.resolve_path(project_path or "/workspace")
        log.info("[claude_code] cwd=%s", cwd)

        options = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            cwd=cwd,
            cli_path=self._cli_path,
            model=self._model,
            include_partial_messages=True,
            setting_sources=["user"],
        )

        async with ClaudeSDKClient(options=options) as client:
            self._client = client

            if task:
                await self._send_query(client, transport, task)

            while True:
                content_parts, _ = await self.agent.next_message()
                text = " ".join(
                    p.get("text", "") for p in content_parts if isinstance(p, dict)
                ).strip()
                if not text:
                    continue

                if text.lower() in ("/stop", "/exit", "стоп", "выход"):
                    break

                await self._send_query(client, transport, text)

            self._client = None

        return {"status": "finished"}
