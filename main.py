import warnings
_original_warn = warnings.warn
def _warn(msg, category=UserWarning, stacklevel=1, source=None, **kw):
    if issubclass(category, (DeprecationWarning, ResourceWarning, FutureWarning)):
        return
    if isinstance(msg, (DeprecationWarning, ResourceWarning, FutureWarning)):
        return
    if getattr(category, "__module__", "").startswith("requests"):
        return
    return _original_warn(msg, category, stacklevel, source, **kw)
warnings.warn = _warn

import asyncio, importlib, json, logging, os, shutil, sys

_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

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
_PID_PATH = ".agent.pid"

def acquire_pid_lock():
    import time, psutil
    try:
        pid = int(open(_PID_PATH).read().strip())
        if all(time.sleep(0.5) or psutil.pid_exists(pid) for _ in range(4)):
            logging.error("Агент уже запущен (PID %d). Завершаю.", pid)
            sys.exit(1)
    except Exception:
        pass
    open(_PID_PATH, "w").write(str(os.getpid()))

def release_pid_lock():
    try: os.unlink(_PID_PATH)
    except FileNotFoundError: pass

async def run():
    from agent import Agent
    from src.transport.cli import CliTransport
    from src.transport.telegram import TelegramTransport
    from src.ui.wrapper import UITransportWrapper


    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f: config = json.load(f)
        os.environ.update(config.get("env", {}))

        if  "--cli" in sys.argv:
            logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
            transport = CliTransport()
        else:
            tg = config["telegram_transport"]
            transport = UITransportWrapper(TelegramTransport)(
                bot_token=tg["bot_token"],
                allowed_user_ids=tg["allowed_user_ids"],
            )

        agent = Agent(**resolve(config["agent"]),transport=transport)
            
    except Exception as e:
        logging.error("Ошибка при запуске: %s", e)
        if os.path.exists(_LAST_GOOD_PATH):
            shutil.move(_LAST_GOOD_PATH, _CONFIG_PATH)
            logging.info("Конфиг откатился до последнего рабочего, перезапуск...")
            os.execv(sys.executable, [sys.executable] + sys.argv)
        raise

    # shutil.copy(_CONFIG_PATH, _LAST_GOOD_PATH)
    try:
        await agent.start()
    finally:
        release_pid_lock()

acquire_pid_lock()
asyncio.run(run())
