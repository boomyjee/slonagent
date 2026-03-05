import json
import os
import sys
from agent import Skill, tool, bypass


def _format_json(obj, indent=4, level=0) -> str:
    def is_simple(v) -> bool:
        return isinstance(v, (str, int, float, bool)) or v is None

    pad = " " * indent * level
    child_pad = " " * indent * (level + 1)
    if isinstance(obj, dict):
        if len(obj) < 3 and all(is_simple(v) for v in obj.values()):
            return json.dumps(obj, ensure_ascii=False)
        items = [f"{child_pad}{json.dumps(k)}: {_format_json(v, indent, level + 1)}" for k, v in obj.items()]
        return "{\n" + ",\n".join(items) + "\n" + pad + "}"
    if isinstance(obj, list):
        if len(obj) <= 1 and all(is_simple(v) for v in obj):
            return json.dumps(obj, ensure_ascii=False)
        items = [f"{child_pad}{_format_json(v, indent, level + 1)}" for v in obj]
        return "[\n" + ",\n".join(items) + "\n" + pad + "]"
    return json.dumps(obj, ensure_ascii=False)

HELP = (
    "config read                 — показать весь конфиг\n"
    "config read <key>           — показать значение по ключу\n"
    "config write <key> <value>  — установить значение\n"
    "config write <key>          — удалить ключ\n"
    "config write <key>[] <value>— добавить/удалить значение в массиве (toggle)"
)

class ConfigSkill(Skill):
    def __init__(self, config_path: str = None):
        super().__init__()
        if config_path is None:
            root = os.path.dirname(os.path.abspath(sys.modules["__main__"].__file__))
            config_path = os.path.join(root, ".config.json")
        self.config_path = config_path
        if not os.path.exists(config_path):
            self._save({})

    @bypass("config", "Конфиг read/write", standalone=True)
    def config_command(self, args: str) -> str:
        parts = args.strip().split(None, 1)
        if not parts:
            return f"```json\n{json.dumps(self._load(), ensure_ascii=False, indent=2)}\n```"

        subcommand = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        if subcommand == "read":
            cfg = self._load()
            if rest:
                value = self._get(cfg, rest.strip())
                if value is None:
                    return f"Ключ не найден: {rest.strip()}"
                return f"{rest.strip()} = {json.dumps(value, ensure_ascii=False, indent=2)}"
            return f"```json\n{json.dumps(cfg, ensure_ascii=False, indent=2)}\n```"

        if subcommand == "write":
            if not rest:
                return f"Использование:\n{HELP}"
            kv = rest.split(None, 1)
            key_expr = kv[0]
            value_str = kv[1] if len(kv) > 1 else None

            cfg = self._load()
            if key_expr.endswith("[]"):
                key = key_expr[:-2]
                if value_str is None:
                    return "Для toggle-операции нужно значение: /config write key[] <value>"
                value = self._parse_value(value_str)
                current = self._get(cfg, key) or []
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

            if value_str is None:
                if not self._delete(cfg, key_expr):
                    return f"Ключ не найден: {key_expr}"
                self._save(cfg)
                return f"✓ Удалён: {key_expr}"

            self._set(cfg, key_expr, self._parse_value(value_str))
            self._save(cfg)
            return f"✓ {key_expr} = {json.dumps(self._parse_value(value_str), ensure_ascii=False)}"

        return f"Неизвестная подкоманда.\n{HELP}"

    def get(self, key: str, default=None):
        return self._get(self._load(), key, default)

    def _load(self) -> dict:
        try:
            with open(self.config_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self, cfg: dict):
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(_format_json(cfg) + "\n")

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
