"""Universal mode launcher. Usage: start_test_mode.bat <tool_name>"""
import asyncio, json, logging, os, sys
from agent import Agent
from src.transport.telegram import TelegramTransport
from src.transport.multi import MultiTransport
from src.transport.dashboard import DashboardTransport

with open("scripts/test_mode/.config.json", encoding="utf-8") as f: config = json.load(f)
os.environ.update(config.get("env", {}))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

async def main():
    if len(sys.argv) < 2: print("Usage: python -m scripts.test_mode <tool_name>"); sys.exit(1)
    tool_name = sys.argv[1]
    DashboardTransport.set_server_config(**config.get("web", {}))

    async def make_agent(agent_id, tg, **_):
        if agent_id != "main": return None
        transport = MultiTransport([tg, DashboardTransport()])
        agent = Agent.from_config(config["agent"], id="main", transport=transport, agent_dir=os.path.dirname(__file__))
        await agent.start(run_loop=False)
        async def run_tool():
            await agent.dispatch_tool_calls({"tool_calls": [{
                "id": "cli", "function": {"name": tool_name, "arguments": "{}"},
            }]})
            asyncio.get_running_loop().stop()
        asyncio.create_task(run_tool())
        return agent
    await TelegramTransport.listen(config["telegram"], make_agent)


if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError): pass
