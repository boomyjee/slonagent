import json, logging, os
from typing import Annotated
from agent import tool
from src.memory.providers.base import BaseProvider
from memory import Memory


class PersonalityProvider(BaseProvider):
    def __init__(self):
        super().__init__(consolidate_tokens=0)
        self._dir = os.path.join(Memory.memory_dir, "personalities")
        os.makedirs(self._dir, exist_ok=True)
        self._active_file = os.path.join(self._dir, ".active.json")
        try:
            self._active: list[str] = json.loads(open(self._active_file, encoding="utf-8").read())
        except (FileNotFoundError, json.JSONDecodeError):
            self._active: list[str] = []

    def _path(self, name: str) -> str:
        return os.path.join(self._dir, f"{name}.md")

    def _read(self, name: str) -> tuple[str, str]:
        path = self._path(name)
        if not os.path.exists(path):
            return "", ""
        with open(path, encoding="utf-8") as f:
            text = f.read()
        parts = text.split("---", 1)
        description = parts[0].strip()
        content = parts[1].strip() if len(parts) > 1 else ""
        return description, content

    def _write(self, name: str, description: str, content: str):
        with open(self._path(name), "w", encoding="utf-8") as f:
            f.write(f"{description}\n---\n{content}")

    def _list(self) -> list[str]:
        return sorted(f[:-3] for f in os.listdir(self._dir) if f.endswith(".md"))

    async def get_context_prompt(self, user_text: str = "") -> str:
        all_names = self._list()
        active = [n for n in self._active if n in all_names]

        if not all_names:
            index = "(субличностей пока нет)"
        else:
            lines = []
            for name in all_names:
                description, content = self._read(name)
                if name in active:
                    lines.append(f"- **{name}** — {description} [активна]")
                else:
                    lines.append(f"- **{name}** — {description} ({len(content)} симв.)")
            index = "\n".join(lines)

        instruction = (
            "Субличности — это грани тебя самого в разных контекстах. "
            "Каждая субличность хранит не просто факты, а целостный образ: "
            "что ты знаешь об этой теме, как к ней относишься, что важно учитывать.\n\n"
            "Активные субличности попадают в контекст целиком и влияют на то, как ты отвечаешь. "
            "Вызови personality_load с полным списком нужных субличностей — "
            "те, что не перечислены, автоматически деактивируются. "
            "Обновлять (personality_update) можно только активные субличности.\n\n"
            "Обновляй субличность сразу, как только в разговоре появляется что-то важное для неё. "
            "Если для темы разговора нет подходящей субличности — создай через personality_create. "
            "Имя должно отражать контекст, а не просто тему: "
            "не 'cooking', а 'home_chef' — не 'user', а 'friend_alex'."
        )

        parts = [f"## Субличности\n{index}\n\n{instruction}"]

        for name in active:
            description, content = self._read(name)
            parts.append(f"### [{name}] {description}\n{content}")

        return "\n\n".join(parts)

    @tool("Активировать субличности. Перечисленные загружаются в контекст, остальные автоматически деактивируются.")
    def load(self, names: Annotated[list[str], "Список имён субличностей для активации"]) -> dict:
        available = self._list()
        unknown = [n for n in names if n not in available]
        if unknown:
            return {"error": f"Не найдены: {', '.join(unknown)}. Доступные: {', '.join(available)}"}
        self._active = list(names)
        open(self._active_file, "w", encoding="utf-8").write(json.dumps(self._active, ensure_ascii=False))
        logging.info("[PersonalityProvider] активные: %s", self._active)
        return {"active": self._active}

    @tool("Обновить содержимое активной субличности.")
    def update(
        self,
        name: Annotated[str, "Имя субличности"],
        content: Annotated[str, "Полное новое содержимое субличности"],
    ) -> dict:
        if name not in self._active:
            return {"error": f"Субличность '{name}' не активна. Активные: {self._active}. Сначала вызови personality_load."}
        if not os.path.exists(self._path(name)):
            return {"error": f"Субличность '{name}' не найдена."}
        description, _ = self._read(name)
        self._write(name, description, content)
        logging.info("[PersonalityProvider] обновлена: %s", name)
        return {"updated": name}

    @tool("Создать новую субличность.")
    def create(
        self,
        name: Annotated[str, "Имя субличности (латиница, snake_case, без пробелов)"],
        description: Annotated[str, "Короткое описание (1 строка)"],
        content: Annotated[str, "Начальное содержимое субличности"],
    ) -> dict:
        if os.path.exists(self._path(name)):
            return {"error": f"Субличность '{name}' уже существует. Для обновления используй personality_update."}
        self._write(name, description, content)
        logging.info("[PersonalityProvider] создана: %s", name)
        return {"created": name}
