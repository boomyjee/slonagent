"""
Загружает ключи и env из .config.json в переменные окружения
до запуска тестов, чтобы не нужно было передавать их вручную.
"""

collect_ignore = ["test_recall_pipeline.py"]
import io
import json
import os
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_PATH = os.path.join(_ROOT, ".config.json")

if os.path.exists(_CONFIG_PATH):
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        _cfg = json.load(f)

    for k, v in _cfg.get("env", {}).items():
        os.environ.setdefault(k, v)

    keys = _cfg.get("keys", {})
    if keys.get("llm") and not os.environ.get("LLM_KEY"):
        os.environ["LLM_KEY"] = keys["llm"]
    if keys.get("llm_url") and not os.environ.get("LLM_URL"):
        os.environ["LLM_URL"] = keys["llm_url"]
    if keys.get("openrouter") and not os.environ.get("OPENROUTER_KEY"):
        os.environ["OPENROUTER_KEY"] = keys["openrouter"]
