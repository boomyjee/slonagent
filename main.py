import warnings
warnings.filterwarnings("ignore", category=Warning, module="requests")

import asyncio, importlib, json, logging, os, shutil, sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

def resolve(v):
    if isinstance(v, str) and v.startswith("$"):
        key = v[1:]
        if key not in os.environ:
            raise KeyError(f"Переменная окружения ${key} не задана (проверь .config.json → env)")
        return os.environ[key]
    if isinstance(v, dict):
        if '__class__' in v: return instantiate(v)
        return {k: resolve(val) for k, val in v.items()}
    if isinstance(v, list):
        return [resolve(i) for i in v]
    return v


def instantiate(cfg: dict, cls=None):
    if cls is None:
        module_path, cls_name = cfg["__class__"].rsplit(".", 1)
        cls = getattr(importlib.import_module(module_path), cls_name)
    return cls(**{k: resolve(v) for k, v in cfg.items() if k != "__class__"})

_CONFIG_PATH = ".config.json"
_LAST_GOOD_PATH = ".config.last_good.json"

async def run():
    from agent import Agent
    from src.transport.cli import CliTransport
    from src.transport.telegram import TelegramTransport

    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            config = json.load(f)
        os.environ.update(config.get("env", {}))
        agent = Agent(**resolve(config["agent"]))
        if "--cli" in sys.argv:
            transport = CliTransport(agent)
        else:
            tg = config["telegram_transport"]
            transport = TelegramTransport(bot_token=tg["bot_token"], allowed_user_ids=tg["allowed_user_ids"], agent=agent)
        await agent.start()
    except Exception as e:
        logging.error("Ошибка при запуске: %s", e)
        if os.path.exists(_LAST_GOOD_PATH):
            shutil.move(_LAST_GOOD_PATH, _CONFIG_PATH)
            logging.info("Конфиг откатился до последнего рабочего, перезапуск...")
            os.execv(sys.executable, [sys.executable] + sys.argv)
        raise

    shutil.copy(_CONFIG_PATH, _LAST_GOOD_PATH)
    await transport.start()

asyncio.run(run())
