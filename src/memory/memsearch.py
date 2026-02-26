import asyncio
import logging
import os
from pathlib import Path
from typing import Annotated

from agent import tool
from memory import Memory as AppMemory
from src.memory.base import BaseProvider

log = logging.getLogger(__name__)


def _turns_to_md(turns: list) -> str:
    """Собирает markdown из списка ходов диалога."""
    lines = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role", "")
        text = " ".join(
            p.get("text", "") for p in turn.get("parts", [])
            if isinstance(p, dict) and "text" in p
        ).strip()
        if text:
            label = "Пользователь" if role == "user" else "Ассистент"
            lines.append(f"**{label}:** {text}")
    return "\n\n".join(lines)


class MemSearchProvider(BaseProvider):
    """
    Провайдер памяти на базе memsearch.

    - Markdown-файлы — единственный источник правды
    - Milvus Lite (локальный .db файл, никакого сервера)
    - Hybrid search: dense vector (Gemini) + BM25
    - _consolidate(): пишет диалог в daily .md → индексирует
    - get_context_prompt(): гибридный поиск по запросу пользователя
    - Тул memsearch_write для явной записи заметок агентом
    """

    def __init__(
        self,
        milvus_uri: str = "http://localhost:19530",
        milvus_token: str | None = None,
        consolidate_tokens: int = 2_000,
        top_k: int = 5,
    ):
        super().__init__(consolidate_tokens=consolidate_tokens)
        self._milvus_uri = milvus_uri
        self._milvus_token = milvus_token
        self._top_k = top_k
        self._memory_dir = Path(AppMemory.memory_dir) / "memsearch"
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._mem = None  # ленивая инициализация

    def _get_mem(self):
        if self._mem is None:
            # gRPC подхватывает HTTP_PROXY и пытается идти через него на localhost
            for key in ("no_proxy", "NO_PROXY"):
                existing = os.environ.get(key, "")
                additions = "localhost,127.0.0.1"
                if additions not in existing:
                    os.environ[key] = f"{existing},{additions}".lstrip(",")
            from memsearch import MemSearch
            self._mem = MemSearch(
                paths=[str(self._memory_dir)],
                embedding_provider="google",
                milvus_uri=self._milvus_uri,
                milvus_token=self._milvus_token,
                collection="slonagent",
            )
            self._patch_windows_paths(self._mem)
        return self._mem

    @staticmethod
    def _patch_windows_paths(mem):
        """
        Milvus filter expressions не поддерживают бэкслэши (воспринимает как escape).
        Патчим store чтобы Windows-пути хранились и искались с прямыми слэшами.
        """
        import os as _os
        if _os.name != "nt":
            return

        def fwd(s: str) -> str:
            return s.replace("\\", "/")

        store = mem._store

        _orig_upsert = store.upsert
        def _upsert(chunks):
            for c in chunks:
                if "source" in c:
                    c["source"] = fwd(c["source"])
            return _orig_upsert(chunks)
        store.upsert = _upsert

        _orig_hbs = store.hashes_by_source
        store.hashes_by_source = lambda src: _orig_hbs(fwd(src))

        _orig_dbs = store.delete_by_source
        store.delete_by_source = lambda src: _orig_dbs(fwd(src))

        # index() сравнивает active_sources со stored sources — нужно нормализовать оба
        from memsearch.scanner import scan_paths as _scan
        _orig_index = mem.index

        async def _index(*, force=False):
            files = list(_scan(mem._paths))
            active = {fwd(str(f.path)) for f in files}
            total = 0
            for f in files:
                total += await mem._index_file(f, force=force)
            for src in store.indexed_sources():
                if src not in active:
                    store.delete_by_source(src)
            return total

        mem.index = _index

    def _new_file(self) -> Path:
        from datetime import datetime
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")[:23]
        return self._memory_dir / f"{ts}.md"

    def _write_md(self, path: Path, content: str):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content + "\n")

    # ── consolidate ───────────────────────────────────────────────────────────

    async def _consolidate(self, pending: list):
        md = _turns_to_md(pending)
        if not md:
            return
        try:
            self._write_md(self._new_file(), md)
            n = await self._get_mem().index()
            log.info("[MemSearchProvider] indexed %d chunks", n)
        except Exception as e:
            log.warning("[MemSearchProvider] consolidate failed: %s", e)

    def _search_sync(self, query: str, top_k: int) -> list:
        """Поиск в отдельном потоке — там нет running event loop."""
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(
                asyncio.run, self._get_mem().search(query, top_k=top_k)
            ).result(timeout=10)

    # ── context ───────────────────────────────────────────────────────────────

    def get_context_prompt(self, user_text: str = "") -> str:
        if not user_text:
            return ""
        try:
            results = self._search_sync(user_text, self._top_k)
        except Exception as e:
            log.warning("[MemSearchProvider] search failed: %s", e)
            return ""

        if not results:
            return ""

        lines = []
        for r in results:
            heading = r.get("heading", "")
            content = r.get("content", "").strip()
            score = r.get("score", 0)
            prefix = f"[{heading}] " if heading else ""
            lines.append(f"- {prefix}{content[:300]} (score={score:.2f})")

        memories = "\n".join(lines)
        return (
            "<memsearch_memories>\n"
            "Релевантные воспоминания из прошлых разговоров:\n"
            f"{memories}\n"
            "</memsearch_memories>\n\n"
            "Записывай важное в память: memsearch_write"
        )

    # ── tools ─────────────────────────────────────────────────────────────────

    @tool("Записать заметку в долгосрочную память. Используй для фактов, решений, предпочтений пользователя.")
    async def memsearch_write(
        self,
        content: Annotated[str, "Текст заметки в markdown"],
    ) -> dict:
        try:
            self._write_md(self._new_file(), content)
            n = await self._get_mem().index()
            return {"ok": True, "indexed_chunks": n}
        except Exception as e:
            log.warning("[MemSearchProvider] write failed: %s", e)
            return {"error": str(e)}

    @tool("Поиск по долгосрочной памяти.")
    async def memsearch_search(
        self,
        query: Annotated[str, "Поисковый запрос"],
        top_k: Annotated[int, "Количество результатов"] = 5,
    ) -> dict:
        try:
            results = await asyncio.to_thread(self._search_sync, query, top_k)
            return {
                "results": [
                    {
                        "content": r.get("content", "")[:500],
                        "heading": r.get("heading", ""),
                        "source": os.path.basename(r.get("source", "")),
                        "score": round(r.get("score", 0), 3),
                    }
                    for r in results
                ]
            }
        except Exception as e:
            log.warning("[MemSearchProvider] search failed: %s", e)
            return {"error": str(e)}
