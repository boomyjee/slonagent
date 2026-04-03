"""Universal mode launcher.

Usage:
    .venv\\Scripts\\python -m scripts.test_mode <start_method>

Examples:
    .venv\\Scripts\\python -m scripts.test_mode start_checkers
    .venv\\Scripts\\python -m scripts.test_mode start_enrichment
    .venv\\Scripts\\python -m scripts.test_mode start_anecdote_loop
"""
import asyncio
import json
import logging
import os
import sys
import tempfile

os.chdir(os.path.join(os.path.dirname(__file__), "..", ".."))

with open("scripts/test_mode/.config.json", encoding="utf-8") as f:
    config = json.load(f)
os.environ.update(config.get("env", {}))

from aiogram import Bot, Dispatcher
from aiogram.types import Message
from aiogram.client.session.aiohttp import AiohttpSession

from agent import Agent
from src.transport.telegram import TelegramTransport

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


async def main():
    if len(sys.argv) < 2:
        print("Usage: python -m scripts.test_mode <start_method>")
        sys.exit(1)

    start_method = sys.argv[1]

    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    bot = Bot(token=config["telegram"]["bot_token"], session=AiohttpSession(proxy=proxy) if proxy else None)
    dp = Dispatcher()

    allowed = config["telegram"]["allowed_user_ids"]
    transport = TelegramTransport(bot=bot, chat_id=allowed[0], thread_id=None, agent_id="mode", verbose=False)
    agent = Agent.from_config(config["agent"], agent_dir=tempfile.mkdtemp(), transport=transport)
    await agent.start(run_loop=False)

    async def on_message(message: Message):
        if not message.from_user or message.from_user.id not in allowed: return
        await agent.transport.handle_message(message)

    async def run_mode():
        skill = next((s for s in agent.skills if hasattr(s, start_method)), None)
        if skill:
            await getattr(skill, start_method)()
        else:
            print(f"Method '{start_method}' not found.")
        dp.stop_polling()

    dp.message()(on_message)
    await bot.get_updates(offset=-1)
    asyncio.create_task(run_mode())
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
