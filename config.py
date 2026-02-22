import json
import logging
import os
import re

HELP = (
    "config read                 — показать весь конфиг\n"
    "config read <key>           — показать значение по ключу\n"
    "config write <key> <value>  — установить значение\n"
    "config write <key>          — удалить ключ\n"
    "config write <key>[] <value>— добавить/удалить значение в массиве (toggle)"
)


class ConfigSkill:
    """
    Управляет JSON-конфигом агента.
    LLM не видит конфиг и не может его менять.
    Команды перехватываются в Agent.process_message до LLM.

    Синтаксис:
      config read [key]
      config write key [value]          — set; без value — delete
      config write key[] value          — toggle в массиве
    """

    tools = []

    def __init__(self, config_path: str):
        self.config_path = config_path
        if not os.path.exists(config_path):
            self._save({})

    # ── bypass interface ──────────────────────────────────────────────────────

    def is_bypass_command(self, text: str) -> bool:
        return bool(re.match(r"^config\s+(read|write)\b", text.strip(), re.IGNORECASE))

    def handle_bypass_command(self, text: str) -> str:
        parts = text.strip().split(None, 2)
        subcommand = parts[1].lower()

        if subcommand == "read":
            cfg = self._load()
            if len(parts) >= 3:
                key = parts[2].strip()
                value = self._get(cfg, key)
                if value is None:
                    return f"Ключ не найден: {key}"
                return f"{key} = {json.dumps(value, ensure_ascii=False, indent=2)}"
            return f"```json\n{json.dumps(cfg, ensure_ascii=False, indent=2)}\n```"

        if subcommand == "write":
            if len(parts) < 3:
                return f"Использование:\n{HELP}"

            rest = parts[2]
            # split key from optional value
            kv = rest.split(None, 1)
            key_expr = kv[0]
            value_str = kv[1] if len(kv) > 1 else None

            # toggle array: key ends with []
            if key_expr.endswith("[]"):
                key = key_expr[:-2]
                if value_str is None:
                    return "Для toggle-операции нужно значение: config write key[] <value>"
                value = self._parse_value(value_str)
                cfg = self._load()
                current = self._get(cfg, key)
                if current is None:
                    current = []
                if not isinstance(current, list):
                    return f"Ошибка: {key} не является массивом (тип: {type(current).__name__})"
                if value in current:
                    current.remove(value)
                    action = "Удалено из"
                else:
                    current.append(value)
                    action = "Добавлено в"
                self._set(cfg, key, current)
                self._save(cfg)
                return f"✓ {action} {key}: {json.dumps(value, ensure_ascii=False)}"

            # delete: no value
            if value_str is None:
                cfg = self._load()
                if not self._delete(cfg, key_expr):
                    return f"Ключ не найден: {key_expr}"
                self._save(cfg)
                return f"✓ Удалён: {key_expr}"

            # set value
            cfg = self._load()
            value = self._parse_value(value_str)
            self._set(cfg, key_expr, value)
            self._save(cfg)
            return f"✓ {key_expr} = {json.dumps(value, ensure_ascii=False)}"

        return f"Неизвестная подкоманда.\n{HELP}"

    # ── public read API for other skills ─────────────────────────────────────

    def get(self, key: str, default=None):
        return self._get(self._load(), key, default)

    # ── internals ─────────────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            with open(self.config_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self, cfg: dict):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _parse_value(s: str):
        try:
            return json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return s

    @staticmethod
    def _keys(key: str) -> list[str]:
        return key.split(".")

    def _get(self, cfg: dict, key: str, default=None):
        node = cfg
        for k in self._keys(key):
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    def _set(self, cfg: dict, key: str, value):
        keys = self._keys(key)
        node = cfg
        for k in keys[:-1]:
            if k not in node or not isinstance(node[k], dict):
                node[k] = {}
            node = node[k]
        node[keys[-1]] = value

    def _delete(self, cfg: dict, key: str) -> bool:
        keys = self._keys(key)
        node = cfg
        for k in keys[:-1]:
            if not isinstance(node, dict) or k not in node:
                return False
            node = node[k]
        if not isinstance(node, dict) or keys[-1] not in node:
            return False
        del node[keys[-1]]
        return True
