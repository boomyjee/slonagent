import re
from pathlib import Path

_ENTRY_RE = re.compile(r'^- ID:([^:\s]+:[^:\s]+):\s*(.*)', re.MULTILINE)


class Glossary:
    def __init__(self, path: Path):
        self.path = path

    def _read_parts(self) -> tuple[str, str]:
        """Returns (description, body) split by '---'."""
        if not self.path.exists():
            return "", ""
        text = self.path.read_text(encoding="utf-8")
        parts = text.split("---", 1)
        desc = parts[0].strip()
        body = parts[1].strip() if len(parts) > 1 else ""
        return desc, body

    def _write(self, desc: str, body: str):
        self.path.write_text(f"{desc}\n---\n{body}\n", encoding="utf-8")

    def read_body(self) -> str:
        _, body = self._read_parts()
        return body

    def read_dict(self) -> dict[str, str]:
        _, body = self._read_parts()
        return {k: v.strip() for k, v in _ENTRY_RE.findall(body)}

    def add(self, id_key: str, description: str):
        desc, body = self._read_parts()
        body = body.rstrip() + f"\n- ID:{id_key}: {description}"
        self._write(desc, body)

    def update(self, id_key: str, new_description: str):
        desc, body = self._read_parts()
        new_body, n = re.subn(
            rf'^(- ID:{re.escape(id_key)}:).*',
            rf'\1 {new_description}',
            body,
            flags=re.MULTILINE,
        )
        if n == 0:
            self.add(id_key, new_description)
        else:
            self._write(desc, new_body)
