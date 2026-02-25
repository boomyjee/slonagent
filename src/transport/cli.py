import asyncio


class CliTransport:
    def __init__(self, agent):
        self.agent = agent

    async def send_message(self, text: str):
        print(f"\nАгент: {text}\n")

    async def send_thinking(self, text: str):
        print(f"[думает...]\n")

    async def on_tool_call(self, name: str, args: dict):
        print(f"[{name}] {args}")

    async def on_tool_result(self, name: str, result):
        print(f"[{name}] -> {result}")

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
                    transport=self,
                )
