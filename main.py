import asyncio, importlib, json, logging, os, sys

from agent import Agent
from memory import Memory
from src.transport.cli import CliTransport
from src.transport.telegram import TelegramTransport

with open(".config.json", encoding="utf-8") as f:
    config = json.load(f)

os.environ.update(config.get("env", {}))
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


def resolve(v):
    if isinstance(v, str) and v.startswith("$"):
        return os.environ[v[1:]]
    if isinstance(v, dict):
        return {k: resolve(val) for k, val in v.items()}
    if isinstance(v, list):
        return [resolve(i) for i in v]
    return v


def instantiate(cfg: dict, cls=None):
    if cls is None:
        module_path, cls_name = cfg["__class__"].rsplit(".", 1)
        cls = getattr(importlib.import_module(module_path), cls_name)
    return cls(**{k: resolve(v) for k, v in cfg.items() if k != "__class__"})


agent_cfg = resolve(config["agent"])
agent = Agent(
    model_name=agent_cfg["model_name"],
    api_key=agent_cfg["api_key"],
    include_thoughts=agent_cfg.get("include_thoughts", False),
    memory=Memory(providers=[instantiate(p) for p in agent_cfg["memory_providers"]]),
    skills=[instantiate(s) for s in agent_cfg["skills"]],
)

if "--cli" in sys.argv:
    transport = CliTransport(agent)
else:
    tg = config["telegram_transport"]
    transport = TelegramTransport(bot_token=tg["bot_token"], allowed_user_ids=tg["allowed_user_ids"], agent=agent)

asyncio.run(transport.start())
