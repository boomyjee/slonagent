"""entity_resolver.py — каноническое разрешение сущностей.

Аналог Hindsight EntityResolver, адаптированный под SQLite.

Стратегия (в порядке убывания приоритета):
  1. Exact match    — COLLATE NOCASE (уже в схеме DDL)
  2. Alias cache    — entity_aliases таблица (fuzzy-resolved ранее)
  3. Substring      — "Ivan" ↔ "Ivan Petrov" и наоборот
  4. SequenceMatcher — нечёткое сходство ≥ SIMILARITY_THRESHOLD

При первом fuzzy-матче alias сохраняется в entity_aliases, чтобы
следующие обращения с тем же именем не перебирали всю таблицу.
"""
import logging
import sqlite3
import uuid
from difflib import SequenceMatcher
from typing import Optional

log = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.82   # ~аналог порога, используемого в Hindsight


def _name_score(a: str, b: str) -> float:
    """
    Комбинированный скор имён (0.0 – 1.0).
    Точное совпадение → 1.0
    Подстрока        → 0.92
    SequenceMatcher  → ratio (может быть < 0.82 → не засчитывается)
    """
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.92
    return SequenceMatcher(None, a, b).ratio()


class EntityResolver:
    """
    Разрешает имена сущностей в канонические entity_id.

    Кешируется in-memory по полному lowercase имени;
    fuzzy-матчи дополнительно сохраняются в entity_aliases.
    """

    def __init__(self, conn: sqlite3.Connection, threshold: float = SIMILARITY_THRESHOLD):
        self.conn = conn
        self.threshold = threshold
        # { lowercase_name -> entity_id }
        self._cache: dict[str, str] = self._load_aliases()

    def _load_aliases(self) -> dict[str, str]:
        """Загружает все известные алиасы из БД в кеш."""
        rows = self.conn.execute("SELECT alias, entity_id FROM entity_aliases").fetchall()
        return {r["alias"]: r["entity_id"] for r in rows}

    def _save_alias(self, alias: str, entity_id: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO entity_aliases (alias, entity_id) VALUES (?, ?)",
            (alias, entity_id),
        )
        self.conn.commit()
        self._cache[alias] = entity_id

    def resolve(
        self,
        name: str,
        nearby_entities: Optional[list[str]] = None,
    ) -> Optional[str]:
        """
        Возвращает entity_id для имени, или None если не найдено.
        Не создаёт новую запись.

        nearby_entities — другие сущности из того же факта.
        Используются для disambiguation через co-occurrence (как у Hindsight):
          score = name_similarity × 0.5
                + co_occurrence_overlap × 0.3
        """
        key = name.strip().lower()
        if not key:
            return None

        # 1. In-memory cache
        if key in self._cache:
            return self._cache[key]

        # 2. Exact match (COLLATE NOCASE)
        row = self.conn.execute(
            "SELECT entity_id FROM entities WHERE name = ?", (name.strip(),)
        ).fetchone()
        if row:
            self._cache[key] = row["entity_id"]
            return row["entity_id"]

        # 3. Fuzzy + co-occurrence scoring
        all_entities = self.conn.execute(
            "SELECT entity_id, name FROM entities"
        ).fetchall()

        if not all_entities:
            return None

        # Предзагружаем co-occurrence если есть nearby_entities
        cooc_map: dict[str, set[str]] = {}
        nearby_ids: set[str] = set()
        if nearby_entities:
            # Резолвим nearby без рекурсии (только exact/cache)
            for nb in nearby_entities:
                nb_key = nb.strip().lower()
                nb_id = self._cache.get(nb_key)
                if nb_id is None:
                    nb_row = self.conn.execute(
                        "SELECT entity_id FROM entities WHERE name = ?", (nb.strip(),)
                    ).fetchone()
                    if nb_row:
                        nb_id = nb_row["entity_id"]
                if nb_id:
                    nearby_ids.add(nb_id)

            if nearby_ids:
                from src.memory.providers.fact.storage import get_cooccurrence_map
                all_ids = [e["entity_id"] for e in all_entities]
                cooc_map = get_cooccurrence_map(self.conn, all_ids)

        best_id: Optional[str] = None
        best_score = 0.0

        for ent in all_entities:
            # name similarity (0–0.5, как у Hindsight)
            name_sim = _name_score(key, ent["name"].lower())
            score = name_sim * 0.5

            # co-occurrence overlap (0–0.3, как у Hindsight)
            if nearby_ids and cooc_map:
                co_entities = cooc_map.get(ent["entity_id"], set())
                overlap = len(nearby_ids & co_entities)
                if nearby_ids:
                    score += (overlap / len(nearby_ids)) * 0.3

            if score > best_score:
                best_score = score
                best_id = ent["entity_id"]

        # Порог на комбинированный score (0.5 × 0.82 = 0.41 для pure name match)
        combined_threshold = self.threshold * 0.5
        if best_score >= combined_threshold and best_id:
            log.debug(
                "[entity_resolver] fuzzy %r → %r (score=%.2f)",
                name, best_id, best_score,
            )
            self._save_alias(key, best_id)
            return best_id

        return None

    def resolve_or_create(
        self,
        name: str,
        nearby_entities: Optional[list[str]] = None,
    ) -> str:
        """
        Возвращает существующий entity_id или создаёт новую сущность.
        Основная точка входа при upsert из retain pipeline.
        """
        entity_id = self.resolve(name, nearby_entities=nearby_entities)
        if entity_id is not None:
            return entity_id

        entity_id = str(uuid.uuid4())
        canonical = name.strip()
        self.conn.execute(
            "INSERT OR IGNORE INTO entities (entity_id, name) VALUES (?, ?)",
            (entity_id, canonical),
        )
        self.conn.commit()

        key = canonical.lower()
        self._cache[key] = entity_id
        log.debug("[entity_resolver] new entity %r → %s", canonical, entity_id)
        return entity_id

    def resolve_batch(self, names: list[str]) -> dict[str, str]:
        """
        Разрешает список имён с учётом взаимного co-occurrence.
        Каждое имя видит остальные как nearby_entities.
        """
        return {
            name: self.resolve_or_create(name, nearby_entities=[n for n in names if n != name])
            for name in names if name.strip()
        }
