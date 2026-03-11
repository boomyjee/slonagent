import asyncio


class CliTransport:
    def __init__(self):
        self.agent = None

    def set_agent(self, agent):
        self.agent = agent

    def _stream_print(self, text: str, stream_id, prefix: str):
        if stream_id is None:
            print(f"\n{prefix}", end="", flush=True)
            stream_id = [0]
        new_text = text[stream_id[0]:]
        if new_text:
            print(new_text, end="", flush=True)
            stream_id[0] = len(text)
        return stream_id

    async def send_message(self, text: str, stream_id=None):
        return self._stream_print(text, stream_id, "Агент: ")

    async def send_system_prompt(self, text: str):
        print(f"[system]\n{text}\n")

    async def send_thinking(self, text: str, stream_id=None, final: bool = False):
        return self._stream_print(text, stream_id, "[думает...]\n")

    async def on_tool_call(self, name: str, args: dict):
        print(f"[{name}] {args}")

    async def on_tool_result(self, name: str, result):
        print(f"[{name}] -> {result}")

    async def inject_message(self, text: str):
        print(f"\n[→] {text}")
        await self.agent.process_message(
            message_parts=[{"text": text}],
            user_query=text,
        )

    async def start(self):
        print("CLI режим. Введите сообщение (Ctrl+C для выхода).")
        while True:
            try:
                text = await asyncio.get_event_loop().run_in_executor(None, input, "Вы: ")
            except (EOFError, KeyboardInterrupt):
                break
            if text.strip():
                await self.agent.process_message(
                    message_parts=[{"text": text}],
                )
                print()
