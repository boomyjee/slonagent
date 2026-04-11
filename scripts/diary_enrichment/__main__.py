"""Run diary enrichment as a standalone app with its own Telegram bot."""
import asyncio, json, logging, os, tempfile
from agent import Agent
from src.transport.telegram import TelegramTransport

with open("scripts/diary_enrichment/.config.json", encoding="utf-8") as f: config = json.load(f)
os.environ.update(config.get("env", {}))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

async def main():
    async def make_agent(agent_id, tg, **_):
        if not agent_id=="main": return None
        agent = Agent.from_config(config["agent"], id="main", transport=tg, agent_dir=tempfile.mkdtemp())
        await agent.start(run_loop=False)
        async def run_skill():
            skill = next(s for s in agent.skills if hasattr(s, "start_enrichment"))
            try: await skill.start_enrichment()
            except Exception: logging.exception("enrichment stop")
            finally: asyncio.get_running_loop().stop()
        asyncio.create_task(run_skill())
        return agent
    await TelegramTransport.listen(config["telegram"], make_agent)

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError): pass