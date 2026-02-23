from agent import Skill, tool
from typing import Annotated
import os
import sys
import logging
import traceback


class LightRAGSkill(Skill):
    """База знаний на LightRAG: граф сущностей + семантический поиск.

    Подходит для книг, статей, документов. Строит граф персонажей/событий/локаций,
    что позволяет отвечать на вопросы о связях и общих темах, а не только о фактах.
    """

    def __init__(self):
        super().__init__()
        self._rags: dict = {}

        root = os.path.dirname(os.path.abspath(sys.modules["__main__"].__file__))
        self._base_dir = os.path.join(root, "memory", "workspace", "lightrag")
        os.makedirs(self._base_dir, exist_ok=True)

    async def _get_rag(self, kb: str):
        if kb not in self._rags:
            self._rags[kb] = await self._build_rag(kb)
        return self._rags[kb]

    async def _build_rag(self, kb: str):
        from lightrag import LightRAG
        from lightrag.llm.openai import openai_complete_if_cache, openai_embed
        from lightrag.utils import EmbeddingFunc
        import numpy as np

        api_key = os.environ["GEMINI_API_KEY"]
        base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
        llm_model = os.environ.get("GEMINI_MEMORY_MODEL", "gemini-2.0-flash")

        working_dir = os.path.join(self._base_dir, kb)
        os.makedirs(working_dir, exist_ok=True)

        async def llm_func(prompt, system_prompt=None, history_messages=None, **kwargs):
            return await openai_complete_if_cache(
                llm_model,
                prompt,
                system_prompt=system_prompt,
                history_messages=history_messages or [],
                api_key=api_key,
                base_url=base_url,
                **kwargs,
            )

        async def embed_func(texts: list) -> np.ndarray:
            return await openai_embed(
                texts,
                model="text-embedding-004",
                api_key=api_key,
                base_url=base_url,
            )

        rag = LightRAG(
            working_dir=working_dir,
            llm_model_func=llm_func,
            embedding_func=EmbeddingFunc(
                embedding_dim=768,
                max_token_size=8192,
                func=embed_func,
            ),
        )
        await rag.initialize_storages()
        return rag

    def get_context_prompt(self) -> str:
        lines = [
            "## База знаний (LightRAG)",
            "Ты можешь загружать книги, статьи и документы в базу знаний и задавать по ним вопросы.",
            "LightRAG строит граф сущностей (персонажи, события, локации, темы) поверх текста,",
            "что позволяет отвечать не только на фактические вопросы, но и на вопросы о связях,",
            "структуре нарратива и общих паттернах.",
            "",
            "Инструменты: ingest_file, ingest_text — загрузка; query — вопрос к базе; kb_list — список баз.",
            "Режимы query: hybrid (рекомендуется), local (факты/персонажи), global (темы/структура), naive (быстро).",
        ]

        if os.path.exists(self._base_dir):
            bases = [
                d for d in os.listdir(self._base_dir)
                if os.path.isdir(os.path.join(self._base_dir, d))
            ]
            if bases:
                lines.append(f"\nЗагруженные базы знаний: {', '.join(bases)}")
            else:
                lines.append("\nБаз знаний пока нет. Загрузи текст через ingest_file или ingest_text.")

        return "\n".join(lines)

    @tool(
        "Загрузить текст в базу знаний. "
        "Подходит для книг, статей, любых документов. "
        "Текст автоматически разбивается на чанки, строится граф сущностей. "
        "Для большого текста (книга) процесс занимает несколько минут."
    )
    async def ingest_text(
        self,
        text: Annotated[str, "Текст для загрузки"],
        kb: Annotated[str, "Имя базы знаний (например 'dostoevsky', 'my_book')"] = "default",
    ) -> dict:
        try:
            rag = await self._get_rag(kb)
            await rag.ainsert(text)
            return {"status": "ok", "kb": kb, "chars": len(text)}
        except Exception as e:
            logging.exception("[lightrag] ingest_text error")
            return {"error": str(e)}

    def _resolve_path(self, path: str) -> str:
        """Резолвит контейнерный путь (/workspace/..., /mnt/c/...) в хост-путь.
        Если путь уже хостовый — возвращает как есть."""
        from exec import ExecSkill
        exec_skill = next(
            (s for s in self.agent.skills if isinstance(s, ExecSkill)),
            None,
        ) if self.agent else None

        if exec_skill:
            resolved = exec_skill.resolve_path(path)
            if resolved:
                return resolved

        return path

    @tool(
        "Загрузить файл (.txt, .md) в базу знаний. "
        "Принимает как хостовые пути (C:\\\\books\\\\book.txt), "
        "так и пути контейнера (/workspace/book.txt, /mnt/c/books/book.txt)."
    )
    async def ingest_file(
        self,
        path: Annotated[str, "Путь к файлу (хостовый или контейнерный)"],
        kb: Annotated[str, "Имя базы знаний"] = "default",
    ) -> dict:
        host_path = self._resolve_path(path)
        if not os.path.exists(host_path):
            return {"error": f"Файл не найден: {host_path} (исходный путь: {path})"}
        try:
            with open(host_path, encoding="utf-8") as f:
                text = f.read()
            rag = await self._get_rag(kb)
            await rag.ainsert(text)
            return {"status": "ok", "kb": kb, "path": host_path, "chars": len(text)}
        except Exception as e:
            logging.exception("[lightrag] ingest_file error")
            return {"error": str(e)}

    @tool(
        "Задать вопрос базе знаний. Режимы:\n"
        "- hybrid (рекомендуется): граф + семантика, лучший баланс\n"
        "- local: конкретные факты, персонажи, сцены\n"
        "- global: общие темы, паттерны, структура по всему тексту\n"
        "- naive: простой RAG без графа (быстрее, хуже)"
    )
    async def query(
        self,
        question: Annotated[str, "Вопрос на естественном языке"],
        kb: Annotated[str, "Имя базы знаний"] = "default",
        mode: Annotated[str, "Режим: hybrid / local / global / naive"] = "hybrid",
    ) -> dict:
        from lightrag import QueryParam
        try:
            rag = await self._get_rag(kb)
            answer = await rag.aquery(question, param=QueryParam(mode=mode))
            return {"answer": answer, "kb": kb, "mode": mode}
        except Exception as e:
            logging.exception("[lightrag] query error")
            return {"error": str(e)}

    @tool("Показать список всех баз знаний.")
    def kb_list(self) -> dict:
        if not os.path.exists(self._base_dir):
            return {"bases": []}
        bases = [
            d for d in os.listdir(self._base_dir)
            if os.path.isdir(os.path.join(self._base_dir, d))
        ]
        return {"bases": bases}
